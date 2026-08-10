from datetime import datetime

import dashboard.routes.patch as patch_route
import dashboard.routes.upgrade as upgrade_route
import dashboard.telegram_approval_bot as telegram_bot
from shared import db as db_module
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    AuditEntry,
    Cluster,
    Incident,
    IncidentStatus,
    NodeUpgradeGate,
    NodeUpgradeGateState,
)


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _pending_action(incident_id: str = "inc-1") -> str:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id,
                ceph_code="OSD_DOWN",
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
            )
        )
        action = Action(
            incident_id=incident_id,
            action_id="restart_osd_daemon",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale="looks like a stuck OSD",
            proposed_command="docker restart ceph-osd-B",
            target_nodes='["10.20.1.83"]',
        )
        session.add(action)
        session.commit()
        return action.id


def test_index_shows_pending_action_card(dashboard_client):
    # 2026-07-23 restore: this card was removed in a prior session on the
    # (wrong) assumption that Chat-with-AI's own confirm click was itself
    # sufficient for a RISKY proposal. It isn't —
    # dashboard/routes/chat.py::confirm_chat_action routes a RISKY-classified
    # chat proposal (e.g. restart_osd_daemon, still `risky:` in
    # action_policy.yaml) through this SAME PENDING_APPROVAL state, and
    # without this card there was no UI path left to ever call
    # POST /actions/{id}/approve|reject — the action just sat there forever,
    # looking confirmed in the chat transcript but never actually executed.
    _pending_action()
    _login(dashboard_client)

    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Chờ duyệt" in response.text
    assert "restart_osd_daemon" in response.text
    assert "looks like a stuck OSD" in response.text
    assert "docker restart ceph-osd-B" in response.text


def test_approve_action_sets_approved_and_audits_operator_as_actor(dashboard_client):
    action_id = _pending_action("inc-approve")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.APPROVED.value
        incident = session.get(Incident, "inc-approve")
        assert incident.status == IncidentStatus.APPROVED.value
        entries = session.query(AuditEntry).filter_by(incident_id="inc-approve").all()
        assert len(entries) == 1
        assert entries[0].actor == "admin"
        assert entries[0].event_type == "risky_action_approved"


def test_index_still_shows_pending_card_for_uncovered_cluster_even_with_global_telegram_configured(
    dashboard_client, monkeypatch
):
    """2026-08-10 (multi-tenant remediation Phase 2) regression guard: before
    this phase, configuring the 3 global Telegram channels hid the "Chờ
    duyệt" card entirely — safe back then, since every RISKY action was
    always default-cluster-covered by them. Now a non-default cluster can
    have its OWN channel, which NARROWS coverage instead of the global ones
    always covering everything — an uncovered action must still show here,
    or it would be silently unapprovable anywhere (the exact stranding bug
    this codebase already fixed once, see test_index_shows_pending_action_
    card's own docstring)."""
    monkeypatch.setattr(telegram_bot.settings, "telegram_incident_bot_token", "global-token", raising=False)
    monkeypatch.setattr(telegram_bot.settings, "telegram_incident_chat_id", "-1", raising=False)
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="cluster-b",
            ceph_mon_nodes="10.30.1.10",
            ssh_user="root",
            ssh_key_path="/root/.ssh/key",
            ceph_exec_mode="docker",
            is_default=False,
            is_active=True,
            # No telegram_bot_token/chat_id -- no channel of its own.
        )
        session.add(cluster)
        session.commit()
        session.refresh(cluster)
        cluster_id = cluster.id
        session.add(
            Incident(
                id="inc-cluster-b",
                ceph_code="OSD_DOWN",
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
                cluster_id=cluster_id,
            )
        )
        session.add(
            Action(
                incident_id="inc-cluster-b",
                action_id="restart_osd_daemon",
                classification=ActionClassification.RISKY.value,
                status=ActionStatus.PENDING_APPROVAL.value,
                rationale="stuck OSD on cluster-b",
                target_nodes='["10.30.1.20"]',
            )
        )
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Chờ duyệt" in response.text
    assert "stuck OSD on cluster-b" in response.text
    assert "chưa có kênh Telegram cho cụm này" in response.text


def test_reject_action_sets_rejected(dashboard_client):
    action_id = _pending_action("inc-reject")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{action_id}/reject", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.REJECTED.value
        incident = session.get(Incident, "inc-reject")
        assert incident.status == IncidentStatus.REJECTED.value


def test_approve_action_requires_login(dashboard_client):
    action_id = _pending_action()

    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_approve_unknown_action_returns_404(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/actions/does-not-exist/approve")

    assert response.status_code == 404


def _pending_action_with_no_command(incident_id: str = "inc-no-cmd", action_id: str = "investigate_manually") -> str:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id,
                ceph_code="POOL_APP_NOT_ENABLED",
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
            )
        )
        action = Action(
            incident_id=incident_id,
            action_id=action_id,
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale="no automated fix exists for this",
            proposed_command=None,
            target_nodes='["10.20.1.83"]',
        )
        session.add(action)
        session.commit()
        return action.id


def test_approve_action_with_no_command_closes_out_without_worker_execution(dashboard_client):
    # 2026-07-23 regression: approving investigate_manually (or
    # pg_repair_force — neither has an automated Command) used to flip
    # status=APPROVED, which the Worker would then always fail to execute,
    # marking the Incident FAILED and falsely reporting the whole cluster
    # status as ERR. It must now close out directly, no Worker involvement.
    action_id = _pending_action_with_no_command()
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.EXECUTED.value
        incident = session.get(Incident, "inc-no-cmd")
        assert incident.status == IncidentStatus.RESOLVED.value
        entries = session.query(AuditEntry).filter_by(incident_id="inc-no-cmd").all()
        assert len(entries) == 1
        assert entries[0].event_type == "risky_action_acknowledged_no_command"


def test_approve_action_with_no_command_pg_repair_force_also_closes_out(dashboard_client):
    action_id = _pending_action_with_no_command("inc-pg-repair", "pg_repair_force")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.EXECUTED.value


def test_approve_volume_perf_sweep_sets_approved_not_executed(dashboard_client):
    # Regression, 2026-07-29 (verified live via a real user report): before
    # worker/executor/commands.py registered a preview builder for
    # volume_perf_sweep, has_command("volume_perf_sweep") was False —
    # approve_action took the SAME "no command, nothing to execute" branch
    # as investigate_manually/pg_repair_force above, silently marking the
    # Action EXECUTED with the sweep never actually run. Symptom the
    # operator saw: clicking "Duyệt" just redirected back to "/" with
    # nothing else happening — no error, no progress, no sweep.
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id="inc-perf-sweep",
                ceph_code="VOLUME_PERF_SWEEP",
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
            )
        )
        action = Action(
            incident_id="inc-perf-sweep",
            action_id="volume_perf_sweep",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
            target_nodes='["10.20.1.112"]',
            action_params='{"pool": "vms", "mon_ip": "10.20.1.112"}',
        )
        session.add(action)
        session.commit()
        action_id = action.id

    _login(dashboard_client)
    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_id)
        assert action.status == ActionStatus.APPROVED.value
        incident = session.get(Incident, "inc-perf-sweep")
        assert incident.status == IncidentStatus.APPROVED.value


def test_approve_already_approved_action_is_a_no_op(dashboard_client):
    action_id = _pending_action("inc-double")
    _login(dashboard_client)
    dashboard_client.post(f"/actions/{action_id}/approve")

    # Second click (double-submit) — must not raise or duplicate audit rows.
    response = dashboard_client.post(f"/actions/{action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        entries = session.query(AuditEntry).filter_by(incident_id="inc-double").all()
        assert len(entries) == 1  # not 2


# --- Approving an unrelated Action is blocked while a cluster upgrade is
# proposed/approved (2026-07-23) -------------------------------------------


def _pending_upgrade_action(status: str, action_id: str = upgrade_route.CLUSTER_UPGRADE_ACTION_ID) -> str:
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code=upgrade_route.CLUSTER_UPGRADE_CEPH_CODE,
            status=status,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification=ActionClassification.RISKY.value,
            status=status,
        )
        session.add(action)
        session.commit()
        return action.id


def test_approve_other_action_is_blocked_while_upgrade_is_pending_approval(dashboard_client):
    _pending_upgrade_action(ActionStatus.PENDING_APPROVAL.value)
    other_action_id = _pending_action("inc-other-1")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/approve")

    assert response.status_code == 409
    with db_module.SessionLocal() as session:
        action = session.get(Action, other_action_id)
        assert action.status == ActionStatus.PENDING_APPROVAL.value  # untouched


def test_approve_other_action_is_blocked_while_upgrade_is_approved(dashboard_client):
    _pending_upgrade_action(ActionStatus.APPROVED.value)
    other_action_id = _pending_action("inc-other-2")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/approve")

    assert response.status_code == 409


def test_approve_other_action_still_works_when_no_upgrade_active(dashboard_client):
    other_action_id = _pending_action("inc-other-3")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        assert session.get(Action, other_action_id).status == ActionStatus.APPROVED.value


def test_approving_the_upgrade_action_itself_is_never_blocked_by_its_own_gate(dashboard_client):
    upgrade_action_id = _pending_upgrade_action(ActionStatus.PENDING_APPROVAL.value)
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{upgrade_action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        assert session.get(Action, upgrade_action_id).status == ActionStatus.APPROVED.value


# --- Same mutual exclusion, symmetric for patch_install (2026-07-24) --------
#
# A live patch install is just as disruptive to the cluster as a cluster
# upgrade — dashboard/routes/actions.py::approve_action blocks approving
# some OTHER action (including an upgrade) while one is in-flight, both
# ways, the same way it already does for CLUSTER_UPGRADE_ACTION_IDS.


def _pending_patch_action(status: str, action_id: str = patch_route.PATCH_INSTALL_ACTION_ID) -> str:
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code=patch_route.CLUSTER_PATCH_CEPH_CODE,
            status=status,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification=ActionClassification.RISKY.value,
            status=status,
        )
        session.add(action)
        session.commit()
        return action.id


def test_approve_other_action_is_blocked_while_patch_install_is_pending_approval(dashboard_client):
    _pending_patch_action(ActionStatus.PENDING_APPROVAL.value)
    other_action_id = _pending_action("inc-other-patch-1")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/approve")

    assert response.status_code == 409
    with db_module.SessionLocal() as session:
        action = session.get(Action, other_action_id)
        assert action.status == ActionStatus.PENDING_APPROVAL.value  # untouched


def test_approve_other_action_is_blocked_while_patch_install_is_approved(dashboard_client):
    _pending_patch_action(ActionStatus.APPROVED.value)
    other_action_id = _pending_action("inc-other-patch-2")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/approve")

    assert response.status_code == 409


def test_approving_patch_install_itself_is_never_blocked_by_its_own_gate(dashboard_client):
    patch_action_id = _pending_patch_action(ActionStatus.PENDING_APPROVAL.value)
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{patch_action_id}/approve", follow_redirects=False)

    assert response.status_code == 303


def test_approve_upgrade_is_blocked_while_patch_install_in_flight(dashboard_client):
    _pending_patch_action(ActionStatus.PENDING_APPROVAL.value)
    upgrade_action_id = _pending_upgrade_action(ActionStatus.PENDING_APPROVAL.value)
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{upgrade_action_id}/approve")

    assert response.status_code == 409


def test_approve_patch_install_is_blocked_while_upgrade_in_flight(dashboard_client):
    _pending_upgrade_action(ActionStatus.PENDING_APPROVAL.value)
    patch_action_id = _pending_patch_action(ActionStatus.PENDING_APPROVAL.value)
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{patch_action_id}/approve")

    assert response.status_code == 409


def test_approving_patch_build_and_stage_is_blocked_while_patch_install_in_flight(dashboard_client):
    """patch_build_and_stage is NOT exempt from the patch_install gate
    (unlike patch_install approving itself) — it never touches the live
    Ceph cluster, but this app's own propose-time guard
    (_reject_duplicate_patch_proposal) already prevents this scenario from
    arising via the UI; this test just confirms the actions.py backstop
    would also catch it if it somehow did."""
    _pending_patch_action(ActionStatus.PENDING_APPROVAL.value, action_id=patch_route.PATCH_INSTALL_ACTION_ID)
    build_action_id = _pending_patch_action(
        ActionStatus.PENDING_APPROVAL.value, action_id=patch_route.PATCH_BUILD_ACTION_ID
    )
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{build_action_id}/approve")

    assert response.status_code == 409


def test_reject_other_action_is_never_blocked_by_an_active_upgrade(dashboard_client):
    # Rejecting never executes anything — no reason to gate it the way
    # approve is gated.
    _pending_upgrade_action(ActionStatus.PENDING_APPROVAL.value)
    other_action_id = _pending_action("inc-other-4")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/reject", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        assert session.get(Action, other_action_id).status == ActionStatus.REJECTED.value


def test_approve_other_action_is_blocked_when_upgrade_physically_running(dashboard_client, monkeypatch):
    # No PENDING_APPROVAL/APPROVED upgrade Action at all (e.g. already
    # EXECUTED — "start command sent") but cephadm reports the upgrade is
    # still actually running — the live-check layer must catch this too.
    monkeypatch.setattr(upgrade_route, "is_cluster_upgrade_physically_running", lambda: True)
    other_action_id = _pending_action("inc-other-5")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/approve")

    assert response.status_code == 409


def test_approve_gate_also_covers_both_package_based_upgrade_action_ids(dashboard_client):
    # The gate must recognize ALL 3 cluster-upgrade action_ids, not just the
    # original cephadm one — a pending package-based proposal must block
    # approving some OTHER risky action exactly the same way.
    for action_id in (
        "upgrade_ceph_cluster_package_download",
        "upgrade_ceph_cluster_package_local",
    ):
        with db_module.SessionLocal() as session:
            for row in session.query(Action).all():
                session.delete(row)
            for row in session.query(Incident).all():
                session.delete(row)
            session.commit()

        _pending_upgrade_action(ActionStatus.PENDING_APPROVAL.value, action_id=action_id)
        other_action_id = _pending_action(f"inc-other-{action_id}")
        _login(dashboard_client)

        response = dashboard_client.post(f"/actions/{other_action_id}/approve")

        assert response.status_code == 409, f"expected block for {action_id}"


def test_index_shows_disabled_approve_button_for_other_action_while_upgrade_pending(dashboard_client):
    _pending_upgrade_action(ActionStatus.PENDING_APPROVAL.value)
    _pending_action("inc-other-6")
    _login(dashboard_client)

    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert 'class="btn btn-approve" disabled' in response.text


# --- Story 11.3 (AD-19): approving an unrelated Action is blocked while a
# NodeUpgradeGate is non-terminal — exemption is ROW-specific, not
# action_id-family-wide (Reviewer Gate CRITICAL finding #2) -----------------


def _pending_gate_action(
    state: str, incident_id: str = "inc-gate-1", action_id: str = "node_os_gate_prepare"
) -> tuple[str, str]:
    """Returns (action_id, gate_id) — the Action IS `prepare_action_id` on
    the created NodeUpgradeGate row, mirroring how dashboard/routes/
    upgrade.py's real Prepare route will build these (Story 11.3, Task 8)."""
    with db_module.SessionLocal() as session:
        incident = Incident(id=incident_id, ceph_code="NODE_OS_GATE", detected_at=datetime.utcnow())
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
        )
        session.add(action)
        session.flush()
        gate = NodeUpgradeGate(
            host="10.20.1.83",
            target_version="18.2.4",
            state=state,
            prepare_action_id=action.id,
        )
        session.add(gate)
        session.commit()
        return action.id, gate.id


def test_approve_other_action_is_blocked_while_node_upgrade_gate_is_preparing(dashboard_client):
    _pending_gate_action(NodeUpgradeGateState.PREPARING.value)
    other_action_id = _pending_action("inc-other-gate-1")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/approve")

    assert response.status_code == 409
    with db_module.SessionLocal() as session:
        assert session.get(Action, other_action_id).status == ActionStatus.PENDING_APPROVAL.value


def test_approve_other_action_is_blocked_while_node_upgrade_gate_is_prepared(dashboard_client):
    _pending_gate_action(NodeUpgradeGateState.PREPARED.value)
    other_action_id = _pending_action("inc-other-gate-2")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/approve")

    assert response.status_code == 409


def test_approve_other_action_still_works_when_no_node_upgrade_gate_active(dashboard_client):
    other_action_id = _pending_action("inc-other-gate-3")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/approve", follow_redirects=False)

    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        assert session.get(Action, other_action_id).status == ActionStatus.APPROVED.value


def test_approve_other_action_works_once_node_upgrade_gate_reaches_done(dashboard_client):
    _pending_gate_action(NodeUpgradeGateState.DONE.value)
    other_action_id = _pending_action("inc-other-gate-4")
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{other_action_id}/approve", follow_redirects=False)

    assert response.status_code == 303


def test_approving_the_gates_own_prepare_action_is_never_blocked_by_its_own_gate(dashboard_client):
    prepare_action_id, _gate_id = _pending_gate_action(NodeUpgradeGateState.PREPARING.value)
    _login(dashboard_client)

    response = dashboard_client.post(f"/actions/{prepare_action_id}/approve", follow_redirects=False)

    # Row-specific self-exemption: this action IS the gate's own
    # prepare_action_id, so it must not be blocked by its own non-terminal
    # state. (No real command is registered for node_os_gate_prepare until
    # Task 7 lands, so this may resolve as ACKNOWLEDGED/EXECUTED rather
    # than APPROVED — either way, the key assertion is "not 409".)
    assert response.status_code != 409
