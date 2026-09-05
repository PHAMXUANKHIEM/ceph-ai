"""Vitastor closed-loop remediation — policy, proposer, executor, watcher
reconciliation and the approve/reject/audit routes.

Mirrors the isolation guarantees of the Ceph pipeline tests: a Vitastor policy
decision is conservative by default, an action only ever runs on an
allowlisted host via a closed command builder, and the whole surface is gated
behind Vitastor login + Vitastor admin."""

import bcrypt
import pytest

from shared import db
from shared.models import (
    VitastorActionStatus,
    VitastorAuditEntry,
    VitastorCluster,
    VitastorRemediationAction,
    VitastorUser,
)
from vitastor import remediation


# --- Pure policy / builder / proposer --------------------------------------

def test_classify_is_conservative_by_default():
    assert remediation.classify_action("resync_time").value == "SAFE"
    assert remediation.classify_action("start_osd_service").value == "RISKY"
    assert remediation.classify_action("restart_mon_service").value == "RISKY"
    # An action_id nobody recognises is RISKY, never SAFE (AD-5).
    assert remediation.classify_action("rm_rf_everything").value == "RISKY"


def test_build_command_uses_closed_builders():
    assert remediation.build_command("start_osd_service", {"osd_id": "3"}) == "systemctl start vitastor-osd@3"
    assert remediation.build_command("restart_osd_service", {"osd_id": "7"}) == "systemctl restart vitastor-osd@7"
    assert remediation.build_command("restart_mon_service", {}) == "systemctl restart vitastor-mon"
    assert remediation.build_command("restart_etcd_service", {}) == "systemctl restart vitastor-etcd"
    # No-op action runs nothing.
    assert remediation.build_command("investigate_manually", {}) is None


def test_build_command_rejects_bad_osd_id_and_unknown_action():
    with pytest.raises(remediation.VitastorRemediationError):
        remediation.build_command("start_osd_service", {"osd_id": "3; rm -rf /"})
    with pytest.raises(remediation.VitastorRemediationError):
        remediation.build_command("start_osd_service", {})
    with pytest.raises(remediation.VitastorRemediationError):
        remediation.build_command("totally_unknown", {})


def test_propose_from_status_flags_down_osds_only():
    datasets = {"osds": [
        {"type": "osd", "name": 1, "parent": "node-a", "up": True},
        {"type": "osd", "name": 2, "parent": "node-b", "up": False},
        {"type": "host", "name": "node-b", "up": False},           # not an osd row
        {"type": "osd", "name": "", "parent": "node-c", "up": False},   # no id -> skip
        {"type": "osd", "name": 4, "parent": "", "up": False},      # no host -> skip
    ]}
    proposals = remediation.propose_from_status(datasets, {})
    assert len(proposals) == 1
    only = proposals[0]
    assert only["action_id"] == "restart_osd_service"
    assert only["target_host"] == "node-b"
    assert only["action_params"] == {"osd_id": "2"}
    assert only["dedup_key"] == "restart_osd_service:node-b:2"


# --- Executor + host allowlist ---------------------------------------------

def test_run_remediation_enforces_host_allowlist(monkeypatch):
    calls = []
    monkeypatch.setattr(remediation, "_run", lambda host, user, key, cmd: calls.append((host, cmd)) or "started")
    output = remediation.run_remediation(
        "start_osd_service", {"osd_id": "3"}, "node-a", "root", "/key", {"node-a"},
    )
    assert output == "started"
    assert calls == [("node-a", "systemctl start vitastor-osd@3")]


def test_run_remediation_rejects_host_outside_cluster(monkeypatch):
    monkeypatch.setattr(remediation, "_run", lambda *a, **k: pytest.fail("must not SSH to a foreign host"))
    with pytest.raises(remediation.VitastorRemediationError):
        remediation.run_remediation("restart_mon_service", {}, "attacker-host", "root", "/key", {"node-a"})


def test_run_remediation_noop_action_runs_nothing(monkeypatch):
    monkeypatch.setattr(remediation, "_run", lambda *a, **k: pytest.fail("no-op must not SSH"))
    assert remediation.run_remediation("investigate_manually", {}, "node-a", "root", "/key", {"node-a"}) == ""


# --- Watcher reconciliation (DB) -------------------------------------------

def _seed_cluster(**overrides) -> VitastorCluster:
    values = dict(
        name="prod-vita", management_host="10.0.0.10", etcd_address="10.0.0.10:2379",
        etcd_prefix="/vitastor", config_path="", ssh_user="root", ssh_key_path="/key",
        exec_mode="none", container_name="", is_active=True, created_by="admin",
    )
    values.update(overrides)
    with db.SessionLocal() as session:
        cluster = VitastorCluster(**values)
        session.add(cluster)
        session.commit()
        session.refresh(cluster)
        session.expunge(cluster)
        return cluster


def test_reconcile_creates_pending_action_and_dedupes(dashboard_client):
    cluster = _seed_cluster()
    datasets = {"osds": [{"type": "osd", "name": 5, "parent": "node-a", "up": False}]}

    new_pending = remediation.reconcile_monitor_proposals(cluster, datasets, {})
    assert len(new_pending) == 1

    with db.SessionLocal() as session:
        rows = session.query(VitastorRemediationAction).filter_by(cluster_id=cluster.id).all()
        assert len(rows) == 1
        assert rows[0].status == VitastorActionStatus.PENDING_APPROVAL.value
        assert rows[0].classification == "RISKY"
        assert rows[0].proposed_command == "systemctl restart vitastor-osd@5"
        assert rows[0].dedup_key == "restart_osd_service:node-a:5"
        assert session.query(VitastorAuditEntry).filter_by(cluster_id=cluster.id, event_type="PROPOSED").count() == 1

    # Same fault next poll -> no second row while the first is still open.
    assert remediation.reconcile_monitor_proposals(cluster, datasets, {}) == []
    with db.SessionLocal() as session:
        assert session.query(VitastorRemediationAction).filter_by(cluster_id=cluster.id).count() == 1


def test_reconcile_holds_terminal_dedup_until_recovery_then_rearms(dashboard_client):
    cluster = _seed_cluster()
    down = {"osds": [{"type": "osd", "name": 5, "parent": "node-a", "up": False}]}
    remediation.reconcile_monitor_proposals(cluster, down, {})
    with db.SessionLocal() as session:
        row = session.query(VitastorRemediationAction).filter_by(cluster_id=cluster.id).one()
        row.status = VitastorActionStatus.EXECUTED.value
        session.commit()

    # A stale DOWN sample after execution must not create another approval.
    assert remediation.reconcile_monitor_proposals(cluster, down, {}) == []
    # One healthy sample releases the key; a later outage is a new incident.
    assert remediation.reconcile_monitor_proposals(cluster, {"osds": []}, {}) == []
    assert len(remediation.reconcile_monitor_proposals(cluster, down, {})) == 1
    with db.SessionLocal() as session:
        assert session.query(VitastorRemediationAction).filter_by(cluster_id=cluster.id).count() == 2


def test_reconcile_rejects_host_not_in_allowlist_on_execute(dashboard_client, monkeypatch):
    """A SAFE proposal auto-executes; if telemetry ever pointed it at a host
    outside the cluster the executor refuses and the row is FAILED, never run."""
    cluster = _seed_cluster()
    monkeypatch.setattr(remediation, "propose_from_status", lambda datasets, summary: [{
        "action_id": "resync_time", "target_host": "ghost-host", "action_params": {},
        "rationale": "clock skew", "dedup_key": "resync_time:ghost-host",
    }])
    monkeypatch.setattr(remediation, "_run", lambda *a, **k: pytest.fail("must not SSH to a foreign host"))

    remediation.reconcile_monitor_proposals(cluster, {"osds": []}, {})
    with db.SessionLocal() as session:
        row = session.query(VitastorRemediationAction).filter_by(cluster_id=cluster.id).one()
        assert row.status == VitastorActionStatus.FAILED.value
        assert "không thuộc cụm" in (row.error_message or "")


# --- Routes: list / approve / reject / audit -------------------------------

def _login_admin(client):
    client.post("/login", data={"username": "admin", "password": "admin", "product": "vitastor"})


def _seed_action(cluster_id, **overrides) -> str:
    values = dict(
        cluster_id=cluster_id, source="MONITOR", action_id="start_osd_service",
        classification="RISKY", status=VitastorActionStatus.PENDING_APPROVAL.value,
        target_host="node-a", action_params='{"osd_id": "5"}',
        proposed_command="systemctl start vitastor-osd@5", rationale="OSD 5 down",
        dedup_key="start_osd_service:node-a:5", requested_by="vitastor-monitor",
    )
    values.update(overrides)
    with db.SessionLocal() as session:
        row = VitastorRemediationAction(**values)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def test_list_actions_returns_pending_and_admin_flag(dashboard_client):
    cluster = _seed_cluster()
    _seed_action(cluster.id)
    _login_admin(dashboard_client)

    response = dashboard_client.get(f"/vitastor/api/actions?cluster_id={cluster.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["is_admin"] is True
    assert len(body["pending"]) == 1
    assert body["pending"][0]["command"] == "systemctl start vitastor-osd@5"


def test_approve_executes_and_audits(dashboard_client, monkeypatch):
    cluster = _seed_cluster()
    action_id = _seed_action(cluster.id)
    # Never touch a real host in a test — swap the executor for a fake.
    import dashboard.routes.vitastor_actions as routes
    monkeypatch.setattr(routes, "run_remediation", lambda *a, **k: "osd started")
    _login_admin(dashboard_client)

    response = dashboard_client.post(f"/vitastor/api/actions/{action_id}/approve")
    assert response.status_code == 200

    with db.SessionLocal() as session:
        row = session.get(VitastorRemediationAction, action_id)
        assert row.status == VitastorActionStatus.EXECUTED.value
        assert row.result_output == "osd started"
        assert row.approved_by == "admin"
        events = {e.event_type for e in session.query(VitastorAuditEntry).filter_by(action_pk=action_id).all()}
        assert {"APPROVED", "EXECUTING", "EXECUTED"} <= events


def test_approve_rejects_inactive_cluster(dashboard_client, monkeypatch):
    cluster = _seed_cluster(is_active=False)
    action_id = _seed_action(cluster.id)
    import dashboard.routes.vitastor_actions as routes
    monkeypatch.setattr(routes, "run_remediation", lambda *a, **k: pytest.fail("must not execute"))
    _login_admin(dashboard_client)

    response = dashboard_client.post(f"/vitastor/api/actions/{action_id}/approve")

    assert response.status_code == 409
    with db.SessionLocal() as session:
        assert session.get(VitastorRemediationAction, action_id).status == VitastorActionStatus.PENDING_APPROVAL.value


def test_duplicate_background_tasks_execute_an_approval_only_once(dashboard_client, monkeypatch):
    cluster = _seed_cluster()
    action_id = _seed_action(cluster.id, status=VitastorActionStatus.APPROVED.value)
    import dashboard.routes.vitastor_actions as routes
    calls = []
    monkeypatch.setattr(routes, "run_remediation", lambda *args, **kwargs: calls.append(args) or "ok")

    routes._execute_approved(action_id, "admin")
    routes._execute_approved(action_id, "admin")

    assert len(calls) == 1
    with db.SessionLocal() as session:
        assert session.query(VitastorAuditEntry).filter_by(action_pk=action_id, event_type="EXECUTING").count() == 1


def test_approved_action_on_inactive_cluster_never_ssh(dashboard_client, monkeypatch):
    cluster = _seed_cluster(is_active=False)
    action_id = _seed_action(cluster.id, status=VitastorActionStatus.APPROVED.value)
    import dashboard.routes.vitastor_actions as routes
    monkeypatch.setattr(routes, "run_remediation", lambda *a, **k: pytest.fail("must not execute"))

    routes._execute_approved(action_id, "admin")

    with db.SessionLocal() as session:
        row = session.get(VitastorRemediationAction, action_id)
        assert row.status == VitastorActionStatus.FAILED.value
        assert "vô hiệu hoá" in (row.error_message or "")


def test_reject_sets_status_and_audits(dashboard_client):
    cluster = _seed_cluster()
    action_id = _seed_action(cluster.id)
    _login_admin(dashboard_client)

    response = dashboard_client.post(f"/vitastor/api/actions/{action_id}/reject")
    assert response.status_code == 200

    with db.SessionLocal() as session:
        row = session.get(VitastorRemediationAction, action_id)
        assert row.status == VitastorActionStatus.REJECTED.value
        assert session.query(VitastorAuditEntry).filter_by(action_pk=action_id, event_type="REJECTED").count() == 1


def test_approve_is_rejected_when_not_pending(dashboard_client, monkeypatch):
    cluster = _seed_cluster()
    action_id = _seed_action(cluster.id, status=VitastorActionStatus.EXECUTED.value)
    import dashboard.routes.vitastor_actions as routes
    monkeypatch.setattr(routes, "run_remediation", lambda *a, **k: pytest.fail("must not execute a non-pending action"))
    _login_admin(dashboard_client)

    response = dashboard_client.post(f"/vitastor/api/actions/{action_id}/approve")
    assert response.status_code == 409


def test_non_admin_vitastor_user_cannot_approve(dashboard_client):
    cluster = _seed_cluster()
    action_id = _seed_action(cluster.id)
    password_hash = bcrypt.hashpw(b"operator", bcrypt.gensalt()).decode()
    with db.SessionLocal() as session:
        session.add(VitastorUser(
            username="viewer", password_hash=password_hash, is_admin=False,
            is_active=True, created_by="admin",
        ))
        session.commit()
    dashboard_client.post("/login", data={"username": "viewer", "password": "operator", "product": "vitastor"})

    response = dashboard_client.post(f"/vitastor/api/actions/{action_id}/approve")
    assert response.status_code == 403
    with db.SessionLocal() as session:
        assert session.get(VitastorRemediationAction, action_id).status == VitastorActionStatus.PENDING_APPROVAL.value


def test_ceph_session_cannot_reach_vitastor_actions(dashboard_client):
    cluster = _seed_cluster()
    # Logged in to the Ceph product, not Vitastor.
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get(
        f"/vitastor/api/actions?cluster_id={cluster.id}", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_audit_endpoint_lists_entries(dashboard_client):
    cluster = _seed_cluster()
    action_id = _seed_action(cluster.id)
    with db.SessionLocal() as session:
        remediation.record_audit(session, cluster.id, action_id, "PROPOSED", "vitastor-monitor", "OSD 5 down")
        session.commit()
    _login_admin(dashboard_client)

    response = dashboard_client.get(f"/vitastor/api/audit?cluster_id={cluster.id}")

    assert response.status_code == 200
    entries = response.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["event_type"] == "PROPOSED"
    assert entries[0]["actor"] == "vitastor-monitor"
