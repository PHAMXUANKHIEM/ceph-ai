"""OS settings of the Ceph nodes, checked against the 09/10/2026 ceph2 lockup."""

from watcher import node_config_audit as audit

CEPH2_BEFORE = {
    "HOST": "rnd-khiempx-lab-ceph2", "MACHINE_ID": "67fafb1a07f74320850b77a083102175",
    "MEM_KB": "8127520", "SWAP_KB": "0", "LRU_GEN": "0x0007", "THP": "always", "MIN_FREE_KB": "67584",
    "JOURNAL": "volatile", "MAKECACHE": "enabled", "OSDS": "2", "NTP_SYNC": "yes",
}
CEPH2_AFTER = {**CEPH2_BEFORE, "LRU_GEN": "0x0000", "THP": "madvise", "MIN_FREE_KB": "262144",
               "JOURNAL": "persistent", "MAKECACHE": "disabled"}
TWO_GB = 2 * 1024**3


def _codes(findings):
    return sorted(finding.code for finding in findings)


def test_ceph2_before_the_fix_raises_every_reclaim_risk():
    findings = audit.evaluate({"10.3.53.69": CEPH2_BEFORE}, TWO_GB)

    assert _codes(findings) == ["DNF_MAKECACHE_ENABLED", "JOURNAL_VOLATILE", "MIN_FREE_LOW",
                                "RECLAIM_LOCKUP_RISK", "THP_ALWAYS"]
    assert all(finding.fix for finding in findings)


def test_ceph2_after_the_fix_is_clean():
    assert audit.evaluate({"10.3.53.69": CEPH2_AFTER}, TWO_GB) == []


def test_two_addresses_of_one_vm_are_one_node_but_clones_share_a_machine_id():
    ceph1 = {**CEPH2_AFTER, "HOST": "rnd-khiempx-lab-ceph1"}
    findings = audit.evaluate({"10.3.53.1": ceph1, "10.20.1.39": ceph1, "10.3.53.69": CEPH2_AFTER}, TWO_GB)

    assert _codes(findings) == ["DUPLICATE_MACHINE_ID"]
    assert findings[0].host == "rnd-khiempx-lab-ceph1, rnd-khiempx-lab-ceph2"


def test_memory_budget_counts_the_osds_on_the_node():
    crowded = {**CEPH2_AFTER, "OSDS": "3"}

    assert _codes(audit.evaluate({"h": crowded}, 4 * 1024**3)) == ["MEMORY_OVERCOMMIT"]
    assert audit.evaluate({"h": crowded}, None) == []  # unknown target: no guess


def test_the_facts_command_is_read_only():
    for verb in ("rm ", "echo n >", "systemctl disable", "sysctl -w", "reboot"):
        assert verb not in audit.FACTS_COMMAND


def test_telegram_hears_only_when_findings_change(monkeypatch):
    published = {}
    sent = []
    monkeypatch.setattr(audit, "collect", lambda nodes: {"10.3.53.69": CEPH2_BEFORE})
    monkeypatch.setattr(audit, "osd_memory_target", lambda: TWO_GB)
    import shared.cluster_snapshot as snapshots

    monkeypatch.setattr(snapshots, "publish_section_snapshot",
                        lambda cluster_id, section, data, **kw: published.update({section: data}))
    monkeypatch.setattr(snapshots, "read_section_snapshot",
                        lambda cluster_id, section, **kw: dict(published) or None)

    audit.run_audit("c1", ["10.3.53.69"], notify=sent.append)
    audit.run_audit("c1", ["10.3.53.69"], notify=sent.append)
    monkeypatch.setattr(audit, "collect", lambda nodes: {"10.3.53.69": CEPH2_AFTER})
    audit.run_audit("c1", ["10.3.53.69"], notify=sent.append)

    assert len(sent) == 2
    assert "kswapd" in sent[0] and sent[1].startswith("✅")


# --- clocks (09/10/2026: the Ceph AI host ran 37.7 s behind) ---------------------------------

def _timed(facts, node_epoch, local_epoch, **extra):
    return {**facts, "EPOCH": f"{node_epoch:.3f}", "_LOCAL_EPOCH": f"{local_epoch:.3f}", **extra}


def test_ceph_ai_host_running_behind_synced_nodes_is_reported():
    ceph1 = _timed({**CEPH2_AFTER, "HOST": "rnd-khiempx-lab-ceph1", "MACHINE_ID": "a"}, 1000.0 + 37.7, 1000.0)
    ceph2 = _timed({**CEPH2_AFTER, "MACHINE_ID": "b"}, 1010.0 + 37.8, 1010.0)

    findings = audit.evaluate({"10.3.53.1": ceph1, "10.3.53.69": ceph2}, TWO_GB)

    assert _codes(findings) == ["CEPH_AI_CLOCK_SKEW"]
    assert "chậm 37.8 s" in findings[0].message or "chậm 37.7 s" in findings[0].message


def test_round_trip_noise_and_unsynced_nodes_do_not_blame_the_ceph_ai_host():
    near = _timed({**CEPH2_AFTER, "MACHINE_ID": "a"}, 1000.4, 1000.0)
    unsynced = _timed({**CEPH2_AFTER, "HOST": "rnd-khiempx-lab-ceph3", "MACHINE_ID": "c"}, 1100.0, 1000.0,
                      NTP_SYNC="no")

    findings = audit.evaluate({"10.3.53.69": near, "10.3.54.118": unsynced}, TWO_GB)

    assert _codes(findings) == ["NTP_UNSYNCED"]  # the node is wrong, not this host
    assert findings[0].host == "rnd-khiempx-lab-ceph3"
