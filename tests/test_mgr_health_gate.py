"""Reusing `ceph health detail` while the mgr's metrics show no change (09/10/2026)."""

import pytest

from watcher import mgr_health_gate as gate_module

PAGE = """# HELP ceph_health_status Cluster health status
ceph_health_status 1.0
ceph_health_detail{name="LARGE_OMAP_OBJECTS",severity="HEALTH_WARN"} 1.0
ceph_health_detail{name="AUTH_INSECURE_KEYS_ALLOWED",severity="HEALTH_WARN"} 1.0
ceph_health_detail{name="OSD_DOWN",severity="HEALTH_WARN"} 0.0
ceph_mon_quorum_status{ceph_daemon="mon.a"} 1.0
"""
HEALTH = {"status": "HEALTH_WARN", "checks": {"LARGE_OMAP_OBJECTS": {}, "AUTH_INSECURE_KEYS_ALLOWED": {"muted": True}}}


def test_only_active_checks_count_and_an_empty_standby_page_is_no_data():
    parsed = gate_module.parse_health(PAGE)

    assert parsed.status == 1
    assert parsed.checks == {"LARGE_OMAP_OBJECTS", "AUTH_INSECURE_KEYS_ALLOWED"}  # OSD_DOWN is 0: past
    assert gate_module.parse_health("") is None  # a standby mgr answers 200 with no body


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _gate(monkeypatch, mode, pages, clock):
    monkeypatch.setattr(gate_module.settings, "ceph_health_mgr_gate", mode)
    monkeypatch.setattr(gate_module.settings, "ceph_health_mgr_gate_max_age_seconds", 60)
    fetched = iter(pages)
    return gate_module.MgrHealthGate(discover=lambda: "http://mgr:9283/metrics", fetch=lambda url: next(fetched),
                                     clock=clock)


def test_on_reuses_health_while_nothing_moved_and_re_reads_when_old(monkeypatch):
    clock, reads = _Clock(), []
    gate = _gate(monkeypatch, "on", [PAGE] * 4, clock)

    def full():
        reads.append(1)
        return dict(HEALTH)

    gate.read(full)
    clock.now += 15
    assert gate.read(full) == HEALTH and len(reads) == 1  # skipped
    clock.now += 50  # last full read is now 65 s old
    gate.read(full)
    assert len(reads) == 2 and gate.metrics["skipped"] == 1


def test_a_new_check_forces_a_full_read(monkeypatch):
    clock, reads = _Clock(), []
    changed = PAGE.replace('name="OSD_DOWN",severity="HEALTH_WARN"} 0.0', 'name="OSD_DOWN",severity="HEALTH_WARN"} 1.0')
    gate = _gate(monkeypatch, "on", [PAGE, changed], clock)

    gate.read(lambda: reads.append(1) or dict(HEALTH))
    clock.now += 15
    gate.read(lambda: reads.append(1) or dict(HEALTH))

    assert len(reads) == 2


@pytest.mark.parametrize("page", ["", "<html>standby</html>"])
def test_no_mgr_data_never_skips(monkeypatch, page):
    clock, reads = _Clock(), []
    gate = _gate(monkeypatch, "on", [PAGE, page], clock)

    gate.read(lambda: reads.append(1) or dict(HEALTH))
    clock.now += 15
    gate.read(lambda: reads.append(1) or dict(HEALTH))

    assert len(reads) == 2 and gate.metrics["mgr_unavailable"] == 1


def test_a_fetch_error_rediscovers_the_active_mgr(monkeypatch):
    monkeypatch.setattr(gate_module.settings, "ceph_health_mgr_gate", "on")
    found = []

    def discover():
        found.append(1)
        return "http://mgr:9283/metrics"

    def broken(url):
        raise TimeoutError("mgr failed over")

    gate = gate_module.MgrHealthGate(discover=discover, fetch=broken, clock=_Clock())
    gate.read(lambda: dict(HEALTH))
    gate.read(lambda: dict(HEALTH))

    assert len(found) == 2  # looked up again after the failure


def test_shadow_always_reads_and_records_agreement(monkeypatch):
    clock, reads = _Clock(), []
    gate = _gate(monkeypatch, "shadow", [PAGE] * 2, clock)

    gate.read(lambda: reads.append(1) or dict(HEALTH))
    clock.now += 15
    gate.read(lambda: reads.append(1) or dict(HEALTH))

    assert len(reads) == 2
    assert gate.metrics["would_skip"] == 1 and gate.metrics["agree"] == 2 and gate.metrics["skipped"] == 0


def test_off_never_asks_the_mgr(monkeypatch):
    monkeypatch.setattr(gate_module.settings, "ceph_health_mgr_gate", "off")
    gate = gate_module.MgrHealthGate(discover=lambda: pytest.fail("must not look up the mgr"))

    assert gate.read(lambda: dict(HEALTH)) == HEALTH


def test_a_failed_full_read_still_raises(monkeypatch):
    from watcher.ceph_client import CephQueryError

    clock = _Clock()
    gate = _gate(monkeypatch, "on", [PAGE], clock)

    def down():
        raise CephQueryError("All MON nodes failed")

    with pytest.raises(CephQueryError):
        gate.read(down)
