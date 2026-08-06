"""Story 11.5 — end-to-end integration test spanning Epic 11 Stories 11.1-11.5:
a node blocked at the Gate, walked through Prepare -> Confirm -> Node Recovery
via mocked SSH, then proven that the cluster-upgrade proposal flow (Story 7.2,
unmodified by Epic 11) resumes normally afterward and does NOT auto-create an
Action. See this story's own file for why this lives in its own module rather
than being appended to test_dashboard_upgrade.py/test_cluster_deploy.py.

Two nodes are configured, not one: `_phase_gate_remove_mon`/
`_phase_gate_restore_config_and_keyring`/`_phase_gate_rejoin_mon` all require
an OTHER configured mon to run FROM (`_any_configured_mon_host(exclude=host)`
raises if none remain, exactly Story 11.3's "never allow a 1-mon cluster to
gate its own only mon" safety invariant) — only NODE_A (combined MON+OSD) goes
through the Gate/Prepare/Confirm/Recovery arc; NODE_B (MON-only) stays up
throughout and plays "the other mon" for every quorum-touching command.
"""

import json
from datetime import datetime

from tests.test_dashboard_upgrade import (
    _login,
    _seed_gate_lock,
    _set_package_deploy,
    _stub_os_release,
    _stub_package_command_preview,
)
from shared import db as db_module
from shared.models import Action, ActionStatus, Incident, NodeUpgradeGate, NodeUpgradeGateLock, NodeUpgradeGateState
from shared.kill_switch import set_kill_switch
from shared.node_upgrade_gate import LOCK_ID
from worker.executor import cluster_deploy as cluster_deploy_module
from worker.llm import router_client as router_client_module

NODE_A = "10.20.1.83"  # gated node: combined MON+OSD
NODE_B = "10.20.1.150"  # stays up the whole time: the "other mon"
NODE_A_HOSTNAME = "node83.lab"
NODE_B_HOSTNAME = "node150.lab"


def _configured_nodes():
    return [
        {"host": NODE_A, "roles": ["MON", "OSD"]},
        {"host": NODE_B, "roles": ["MON"]},
    ]


def _make_full_flow_dispatch_execute():
    """Returns `(dispatch, commands_run)`: a fresh, stateful fake
    `execute_command` covering every SSH call BOTH node_os_gate_prepare's
    and node_os_gate_recover's phase lists issue for NODE_A, happy-path
    only, plus the list it records every `(host, command)` pair into (code
    review fix: lets the test assert WHICH commands actually fired, not
    just that the DB ended up in the right final state). Mirrors the
    per-story dispatch patterns test_cluster_deploy.py already established
    for each phase list individually (Story 11.3's prepare-phase tests,
    Story 11.4's _recover_dispatch_execute) — combined here since this test
    drives both arcs through the real HTTP routes in one flow.

    `ceph quorum_status` needs to be STATEFUL (own counter, same pattern
    test_cluster_deploy.py::test_remove_mon_happy_path_confirms_quorum_count
    already uses): Prepare's remove_mon calls it twice (before=2 mons,
    after=1 mon, checking the delta), then Confirm's rejoin_mon poll calls
    it again expecting NODE_A back in the list (2 mons). Code review fix:
    bounded to exactly 3 calls — a 4th+ call raises, rather than silently
    reusing the "rejoined" answer for a call the phase list was never
    expected to make.

    Code review fix: any command NOT matched by one of the branches below
    raises `AssertionError` instead of silently answering `""` — same
    fail-loud convention `tests/test_cluster_deploy.py`'s own fake_execute
    helpers already use, so an unanticipated command from a future phase-
    list change fails this test immediately instead of being swallowed."""
    quorum_calls = {"n": 0}
    commands_run: list[tuple[str, str]] = []

    def dispatch(host, command):
        commands_run.append((host, command))
        if command == "ceph-volume lvm list":
            return (
                "====== osd.0 =======\n\n"
                "  [block]       /dev/ceph-abc/osd-block-def\n\n"
                "      block device              /dev/ceph-abc/osd-block-def\n"
                "      cluster fsid              11111111-1111-1111-1111-111111111111\n"
                "      osd fsid                  22222222-2222-2222-2222-222222222222\n"
                "      osd id                    0\n"
            )
        if command == "ceph osd dump --format json":
            return json.dumps({"flags": "sortbitwise"})
        if command.startswith("ceph osd set") or command.startswith("ceph osd unset"):
            return ""
        if command.startswith("hostname"):
            return NODE_A_HOSTNAME if host == NODE_A else NODE_B_HOSTNAME
        if command.startswith("ceph mon rm"):
            return ""
        if command == "ceph quorum_status --format json":
            quorum_calls["n"] += 1
            n = quorum_calls["n"]
            if n == 1:
                names = [NODE_A_HOSTNAME, NODE_B_HOSTNAME]  # remove_mon's "before" snapshot
            elif n == 2:
                names = [NODE_B_HOSTNAME]  # remove_mon's "after" snapshot — NODE_A gone
            elif n == 3:
                names = [NODE_A_HOSTNAME, NODE_B_HOSTNAME]  # rejoin_mon's poll — NODE_A back
            else:
                raise AssertionError(
                    f"unexpected 4th+ call to ceph quorum_status (n={n}) — the happy-path flow "
                    f"this test drives should only ever call it 3 times (remove_mon before/after, "
                    f"rejoin_mon's poll)"
                )
            return json.dumps({"quorum_names": names})
        if "pvscan" in command:
            return "CEPH_AIOPS_PV_OK\nCEPH_AIOPS_LV_ALL_ACTIVE"
        if "getenforce" in command:
            return ""
        if "/etc/hosts" in command or "chrony" in command:
            return ""
        if "download.ceph.com" in command or "rhel_ver" in command or "install" in command:
            return ""
        if command.startswith("base64 "):
            return "ZmFrZS1ieXRlcw=="  # base64("fake-bytes")
        if "base64 -d" in command:
            return ""
        if command == "ceph -s":
            return "ok"
        if "bootstrap-osd" in command:
            return ""
        if command == "ceph-volume lvm activate --all" or "ceph-osd@" in command:
            return ""
        if command == "ceph osd tree --format json":
            return json.dumps({"nodes": [{"id": "0", "type": "osd", "status": "up"}]})
        if "ceph auth get mon." in command or "ceph mon getmap" in command:
            return ""
        if command.startswith("sed -i"):
            return ""
        if "mkfs" in command or "systemctl enable --now ceph-mon" in command:
            return ""
        raise AssertionError(f"unmocked command from {host!r}: {command!r}")

    return dispatch, commands_run


def test_full_epic11_flow_from_blocked_gate_to_resumed_cluster_upgrade(dashboard_client, monkeypatch):
    target_version = "19.2.0"  # squid — min_el_version far above el7

    # --- Step 1: blocked at the Gate ------------------------------------
    _set_package_deploy(monkeypatch, mon_nodes=f"{NODE_A},{NODE_B}", osd_nodes=NODE_A)
    monkeypatch.setattr(cluster_deploy_module, "configured_nodes", _configured_nodes)
    _stub_os_release(
        monkeypatch,
        {NODE_A: {"ID": "centos", "VERSION_ID": "7"}, NODE_B: {"ID": "rocky", "VERSION_ID": "9"}},
    )
    _login(dashboard_client)

    response = dashboard_client.post(
        "/upgrade/propose-package-download", data={"target_version": target_version}
    )
    assert response.status_code == 200
    assert "Chưa thể nâng cấp" in response.text
    with db_module.SessionLocal() as session:
        assert session.query(Incident).count() == 0
        assert session.query(Action).count() == 0

    # --- Step 2: Prepare NODE_A ------------------------------------------
    # AD-4: _execute_approved_action re-checks the kill-switch fresh before
    # every phase — the test DB has no seeded SystemFlag row (no migration
    # run against it), so it fails CLOSED (blocking) by default; must be
    # explicitly turned off for this test's execution steps to proceed.
    with db_module.SessionLocal() as session:
        set_kill_switch(session, False)
        session.commit()
    _seed_gate_lock(active_gate_id=None)
    dispatch, commands_run = _make_full_flow_dispatch_execute()
    monkeypatch.setattr(cluster_deploy_module, "execute_command", dispatch)
    monkeypatch.setattr(
        cluster_deploy_module.backup_metadata,
        "latest_successful_metadata_job",
        lambda: type("_J", (), {"created_at": datetime.utcnow()})(),
    )

    response = dashboard_client.post(
        "/upgrade/gate/prepare",
        data={"host": NODE_A, "target_version": target_version},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    # The route only APPROVES the Action (same as every other
    # cluster_deploy_action_ids member) — actual phase execution normally
    # happens on the Worker process's separate poll loop
    # (_process_approved_actions_once). Drive that step directly, same
    # precedent tests/test_router_client.py already establishes for its own
    # approve-then-execute tests.
    with db_module.SessionLocal() as session:
        gate = session.query(NodeUpgradeGate).filter_by(host=NODE_A).one()
        assert gate.state == NodeUpgradeGateState.PREPARING.value
        prepare_action_id = gate.prepare_action_id
        gate_id = gate.id

    router_client_module._execute_approved_action(prepare_action_id)

    with db_module.SessionLocal() as session:
        gate = session.get(NodeUpgradeGate, gate_id)
        assert gate.state == NodeUpgradeGateState.PREPARED.value, gate.state
        # AD-21: the CAS lock stays HELD across the Prepare->Confirm gap
        # (the simulated OS reinstall) — unique to an end-to-end test that
        # actually spans that gap, unlike the individual route-level tests.
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id == gate_id

    # Code review fix: prove the safety-critical commands actually fired
    # against the right host, not just that the DB ended up in the right
    # final state — a phase-sequencing bug would otherwise be invisible.
    assert (NODE_B, "ceph mon rm node83.lab") in commands_run
    # _any_configured_mon_host() (no exclude) picks the FIRST configured
    # mon — NODE_A in this fixture's ordering — for the maintenance-flags
    # phase; only NODE_B is guaranteed to be "the other mon" for phases
    # that explicitly exclude NODE_A (mon removal/rejoin/config-restore).
    assert any(c.startswith("ceph osd set") for _h, c in commands_run)
    assert (NODE_A, "ceph-volume lvm list") in commands_run

    # --- Step 3: operator "reinstalls the OS" -> Confirm ------------------
    _stub_os_release(
        monkeypatch,
        {NODE_A: {"ID": "rocky", "VERSION_ID": "9"}, NODE_B: {"ID": "rocky", "VERSION_ID": "9"}},
    )

    response = dashboard_client.post(
        "/upgrade/gate/confirm", data={"host": NODE_A}, follow_redirects=False
    )
    assert response.status_code == 303, response.text
    with db_module.SessionLocal() as session:
        gate = session.get(NodeUpgradeGate, gate_id)
        assert gate.state == NodeUpgradeGateState.RECOVERING.value
        confirm_action_id = gate.confirm_action_id

    router_client_module._execute_approved_action(confirm_action_id)

    with db_module.SessionLocal() as session:
        gate = session.get(NodeUpgradeGate, gate_id)
        assert gate.state == NodeUpgradeGateState.DONE.value, gate.state
        assert session.get(NodeUpgradeGateLock, LOCK_ID).active_gate_id is None

    assert (NODE_A, "ceph-volume lvm activate --all") in commands_run
    assert any(h == NODE_A and c == "systemctl enable --now ceph-osd@0" for h, c in commands_run)
    assert any(h == NODE_A and "mkfs" in c for h, c in commands_run)  # mon rejoin's mkfs+start

    # --- Step 4: the actual point of this story --------------------------
    # No PACKAGE_DOWNLOAD Action exists yet — Recovery completing alone
    # must not have auto-proposed anything (AC #1: operator vẫn phải chủ
    # động đề xuất lại).
    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id="upgrade_ceph_cluster_package_download").count() == 0

    _stub_package_command_preview(monkeypatch)

    response = dashboard_client.post(
        "/upgrade/propose-package-download", data={"target_version": target_version}
    )
    # propose_package_download_upgrade's success path always returns
    # RedirectResponse(..., status_code=303); dashboard_client's default
    # follow_redirects=True means the OBSERVED status is always the final
    # one after following that redirect (200), never 303 itself.
    assert response.status_code == 200
    assert "Chưa thể nâng cấp" not in response.text  # no longer blocked

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(action_id="upgrade_ceph_cluster_package_download").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        incident = session.get(Incident, action.incident_id)
        assert incident is not None


def test_worker_package_upgrade_dispatch_has_no_epic11_references():
    # Automated version of Task 1's manual grep — a future change can't
    # silently reintroduce a dependency here without this test noticing.
    import worker.llm.router_client as router_client_module

    with open(router_client_module.__file__, encoding="utf-8") as f:
        source = f.read()
    for needle in ("NodeUpgradeGate", "node_os_gate_", "node_upgrade_gate"):
        assert needle not in source, f"unexpected Epic 11 reference in router_client.py: {needle!r}"
