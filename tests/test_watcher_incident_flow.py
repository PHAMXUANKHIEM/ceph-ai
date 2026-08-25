from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.main as watcher_main
from shared import db as db_module
from shared.db import Base
from shared.models import Action, ActionClassification, ActionStatus, Cluster, Incident, IncidentStatus

HEALTH_WARN_PAYLOAD = {
    "status": "HEALTH_WARN",
    "checks": {
        "MON_CLOCK_SKEW": {
            "severity": "HEALTH_WARN",
            "summary": {"message": "clock skew detected", "count": 1},
            "detail": [{"message": "mon.khiempx-mon2 clock skew 0.1s"}],
        }
    },
}

HEALTH_OK_PAYLOAD = {"status": "HEALTH_OK", "checks": {}}

# Two simultaneous checks — exercises the multi-fault path: one Incident row
# per check, all envelopes batch-published in a single asyncio.run() call.
MULTI_CHECK_PAYLOAD = {
    "status": "HEALTH_ERR",
    "checks": {
        "MON_CLOCK_SKEW": {
            "severity": "HEALTH_WARN",
            "summary": {"message": "clock skew detected", "count": 1},
            "detail": [{"message": "mon.khiempx-mon2 clock skew 0.1s"}],
        },
        "OSD_DOWN": {
            "severity": "HEALTH_ERR",
            "summary": {"message": "1 osds down", "count": 1},
            "detail": [{"message": "osd.3 (root=default,host=khiempx-data-b2) is down"}],
        },
    },
}


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    yield engine


def test_no_incident_created_on_recovery_to_health_ok(isolated_db, monkeypatch):
    published = []
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async(published))
    monkeypatch.setattr(
        watcher_main.collector, "collect_relevant_logs", lambda code, detail, **_kw: (["x"], "log")
    )

    watcher_main.build_and_publish_incident("HEALTH_WARN", HEALTH_OK_PAYLOAD)

    with db_module.SessionLocal() as session:
        assert session.query(Incident).count() == 0
    assert published == []


def _seed_incident(ceph_code: str, status: str) -> str:
    from datetime import datetime

    with db_module.SessionLocal() as session:
        incident = Incident(ceph_code=ceph_code, status=status, detected_at=datetime.utcnow())
        session.add(incident)
        session.commit()
        session.refresh(incident)
        return incident.id


def test_full_recovery_to_health_ok_resolves_stuck_open_incidents(isolated_db):
    # Regression: a real Ceph problem going away (e.g. the router-credentials
    # outage that had left Incidents permanently FAILED, or Worker draining
    # a backlog and writing PENDING_APPROVAL/FAILED long after Watcher last
    # saw a transition) must not leave the Dashboard's aggregate cluster
    # status stuck at ERR forever — Watcher never had a path to close an old
    # Incident back out before this. Calls _resolve_recovered_incidents
    # directly — this is now called every poll from run(), not from
    # build_and_publish_incident (see that function's docstring for why).
    failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)
    pending_id = _seed_incident("PG_DEGRADED", IncidentStatus.PENDING_APPROVAL.value)

    watcher_main._resolve_recovered_incidents(set())  # HEALTH_OK -> no checks reported

    with db_module.SessionLocal() as session:
        assert session.get(Incident, failed_id).status == IncidentStatus.RESOLVED.value
        assert session.get(Incident, pending_id).status == IncidentStatus.RESOLVED.value


def test_partial_recovery_only_resolves_the_code_that_disappeared(isolated_db):
    # A different, still-active check must not have its own Incident
    # touched just because an UNRELATED code recovered.
    recovered_id = _seed_incident("MON_CLOCK_SKEW", IncidentStatus.FAILED.value)
    still_active_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    # Only OSD_DOWN is still being reported — MON_CLOCK_SKEW disappeared.
    watcher_main._resolve_recovered_incidents({"OSD_DOWN"})

    with db_module.SessionLocal() as session:
        assert session.get(Incident, recovered_id).status == IncidentStatus.RESOLVED.value
        assert session.get(Incident, still_active_id).status == IncidentStatus.FAILED.value


def test_reconcile_rejects_only_pending_actions_of_terminal_incidents(isolated_db):
    terminal_id = _seed_incident("OLD_WARNING", IncidentStatus.RESOLVED.value)
    open_id = _seed_incident("ACTIVE_WARNING", IncidentStatus.PENDING_APPROVAL.value)
    with db_module.SessionLocal() as session:
        stale = Action(
            incident_id=terminal_id,
            action_id="investigate_manually",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
        )
        completed = Action(
            incident_id=terminal_id,
            action_id="restart_osd_daemon",
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.EXECUTED.value,
        )
        active = Action(
            incident_id=open_id,
            action_id="investigate_manually",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
        )
        session.add_all([stale, completed, active])
        session.commit()
        ids = stale.id, completed.id, active.id

    assert watcher_main._reconcile_terminal_actions() == 1

    with db_module.SessionLocal() as session:
        assert session.get(Action, ids[0]).status == ActionStatus.REJECTED.value
        assert session.get(Action, ids[1]).status == ActionStatus.EXECUTED.value
        assert session.get(Action, ids[2]).status == ActionStatus.PENDING_APPROVAL.value


def test_reconcile_closes_incidents_and_actions_of_inactive_cluster(isolated_db):
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="disabled", ceph_mon_nodes="10.0.0.2", ssh_user="root",
            ssh_key_path="/tmp/key", is_active=False,
        )
        session.add(cluster)
        session.flush()
        incident = Incident(
            cluster_id=cluster.id, ceph_code="POOL_APP_NOT_ENABLED",
            status=IncidentStatus.PENDING_APPROVAL.value, detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id, action_id="investigate_manually",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
        )
        session.add(action)
        session.commit()
        incident_id, action_id = incident.id, action.id

    assert watcher_main._reconcile_terminal_actions() == 1
    with db_module.SessionLocal() as session:
        assert session.get(Incident, incident_id).status == IncidentStatus.RESOLVED.value
        assert session.get(Action, action_id).status == ActionStatus.REJECTED.value


def test_generic_recovery_notifies_once_but_leaves_verifying_to_verifier(
    isolated_db, monkeypatch
):
    notifications = []
    monkeypatch.setattr(
        watcher_main.telegram_alerts,
        "send_incident_verified_alert",
        lambda code, **kwargs: notifications.append(code),
    )
    first = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)
    second = _seed_incident("OSD_DOWN", IncidentStatus.PENDING_APPROVAL.value)
    verifying = _seed_incident("BLUESTORE_SLOW_OP_ALERT", IncidentStatus.VERIFYING.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, first).status == IncidentStatus.RESOLVED.value
        assert session.get(Incident, second).status == IncidentStatus.RESOLVED.value
        assert session.get(Incident, verifying).status == IncidentStatus.VERIFYING.value
    assert notifications == ["OSD_DOWN"]


def test_cluster_recovery_copies_telegram_values_before_session_closes(
    isolated_db, monkeypatch
):
    notifications = []
    monkeypatch.setattr(
        watcher_main.telegram_alerts,
        "send_incident_verified_alert",
        lambda code, **kwargs: notifications.append((code, kwargs)),
    )
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="CS-LAB", ceph_mon_nodes="10.0.0.1", ssh_user="root",
            ssh_key_path="/key", telegram_bot_token="token",
            telegram_chat_id="chat", telegram_enabled=True, is_default=True,
        )
        session.add(cluster); session.flush()
        session.add(Incident(
            cluster_id=cluster.id, ceph_code="OSD_DOWN",
            status=IncidentStatus.FAILED.value, detected_at=datetime.utcnow(),
        ))
        cluster_id = cluster.id
        session.commit()

    watcher_main._resolve_recovered_incidents(set(), cluster_id=cluster_id)

    assert notifications == [("OSD_DOWN", {
        "cluster_name": "CS-LAB", "bot_token": "token", "chat_id": "chat",
        "enabled": True,
    })]


def test_chat_request_incident_is_never_auto_resolved_by_recovery(isolated_db):
    # 2026-07-23 regression: a chat-confirmed action's synthetic Incident
    # (ceph_code="CHAT_REQUEST") never matches any real `ceph health
    # detail` check code, so without the guard in
    # _resolve_recovered_incidents, it would ALWAYS look "recovered" on
    # Watcher's very next poll — silently overwriting a real FAILED outcome
    # with RESOLVED, regardless of what current_codes actually is.
    failed_chat_id = _seed_incident("CHAT_REQUEST", IncidentStatus.FAILED.value)
    real_failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, failed_chat_id).status == IncidentStatus.FAILED.value
        assert session.get(Incident, real_failed_id).status == IncidentStatus.RESOLVED.value


def test_cluster_upgrade_incident_is_never_auto_resolved_by_recovery(isolated_db):
    # 2026-07-23 regression (found live): dashboard/routes/upgrade.py's
    # synthetic Incident (ceph_code="CLUSTER_UPGRADE") never matches any
    # real `ceph health detail` check code either — same bug class as
    # CHAT_REQUEST above. Without this guard, a real upgrade Action that had
    # just failed (e.g. a stale Worker process not yet loaded with the
    # upgrade_ceph_cluster command) got silently overwritten from FAILED to
    # RESOLVED on Watcher's very next poll (~watcher_poll_interval_seconds
    # later), making the failure invisible on the Upgrade page.
    failed_upgrade_id = _seed_incident("CLUSTER_UPGRADE", IncidentStatus.FAILED.value)
    pending_upgrade_id = _seed_incident("CLUSTER_UPGRADE", IncidentStatus.PENDING_APPROVAL.value)
    real_failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, failed_upgrade_id).status == IncidentStatus.FAILED.value
        assert (
            session.get(Incident, pending_upgrade_id).status == IncidentStatus.PENDING_APPROVAL.value
        )
        assert session.get(Incident, real_failed_id).status == IncidentStatus.RESOLVED.value


def test_volume_saturated_incident_is_never_auto_resolved_by_recovery(isolated_db):
    # 2026-07-28: same bug class as CHAT_REQUEST/CLUSTER_UPGRADE above —
    # watcher/volume_monitor.py's synthetic Incidents (ceph_code prefixed
    # "VOLUME_SATURATED:") never match any real `ceph health detail` check
    # code either. That module owns this ceph_code family's own create/
    # resolve lifecycle (its own rolling-window saturated-set); without this
    # guard, _resolve_recovered_incidents would incorrectly "recover" every
    # open volume Incident on every single poll regardless of whether the
    # volume is actually still saturated.
    failed_volume_id = _seed_incident("VOLUME_SATURATED:vms/disk-1", IncidentStatus.FAILED.value)
    pending_volume_id = _seed_incident(
        "VOLUME_SATURATED:vms/disk-2", IncidentStatus.PENDING_APPROVAL.value
    )
    real_failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, failed_volume_id).status == IncidentStatus.FAILED.value
        assert (
            session.get(Incident, pending_volume_id).status == IncidentStatus.PENDING_APPROVAL.value
        )
        assert session.get(Incident, real_failed_id).status == IncidentStatus.RESOLVED.value


def test_log_anomaly_incident_is_owned_by_log_recovery_gate(isolated_db):
    pending_id = _seed_incident("LOG_ANOMALY:16883d76f840", IncidentStatus.PENDING_APPROVAL.value)
    real_failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, pending_id).status == IncidentStatus.PENDING_APPROVAL.value
        assert session.get(Incident, real_failed_id).status == IncidentStatus.RESOLVED.value


def test_node_resource_high_incident_is_never_auto_resolved_by_recovery(isolated_db):
    # 2026-08-05: same bug class as CHAT_REQUEST/CLUSTER_UPGRADE/
    # VOLUME_SATURATED above — watcher/node_health_monitor.py's synthetic
    # Incidents (ceph_code prefixed "NODE_RESOURCE_HIGH:") never match any
    # real `ceph health detail` check code either. That module owns this
    # ceph_code family's own create/resolve lifecycle (its own
    # consecutive-high-scans streak per host); without this guard,
    # _resolve_recovered_incidents would incorrectly "recover" every open
    # node-resource Incident on every single poll regardless of whether the
    # node is actually still overloaded.
    failed_node_id = _seed_incident("NODE_RESOURCE_HIGH:10.0.0.5", IncidentStatus.FAILED.value)
    pending_node_id = _seed_incident("NODE_RESOURCE_HIGH:10.0.0.6", IncidentStatus.PENDING_APPROVAL.value)
    real_failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, failed_node_id).status == IncidentStatus.FAILED.value
        assert session.get(Incident, pending_node_id).status == IncidentStatus.PENDING_APPROVAL.value
        assert session.get(Incident, real_failed_id).status == IncidentStatus.RESOLVED.value


def test_crush_skew_incidents_are_never_auto_resolved_by_recovery(isolated_db):
    # 2026-08-07 (Epic 12, Story 12.2, AD-32 — CRITICAL, marked as this
    # story's mandatory acceptance condition): same bug class as
    # CHAT_REQUEST/CLUSTER_UPGRADE/VOLUME_SATURATED/NODE_RESOURCE_HIGH
    # above — watcher/crush_skew_monitor.py's synthetic Incidents (ceph_code
    # prefixed "CRUSH_SKEW_USE:"/"CRUSH_SKEW_PG:") never match any real
    # `ceph health detail` check code either. Without this guard, a genuine
    # CRUSH Skew Incident would self-resolve on the very next poll
    # (typically within seconds — far faster than
    # settings.crush_scan_interval_seconds), hiding it from the operator
    # before it can ever be seen at PENDING_APPROVAL.
    pending_use_id = _seed_incident("CRUSH_SKEW_USE:3", IncidentStatus.PENDING_APPROVAL.value)
    pending_pg_id = _seed_incident("CRUSH_SKEW_PG:hostA", IncidentStatus.PENDING_APPROVAL.value)
    real_failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, pending_use_id).status == IncidentStatus.PENDING_APPROVAL.value
        assert session.get(Incident, pending_pg_id).status == IncidentStatus.PENDING_APPROVAL.value
        assert session.get(Incident, real_failed_id).status == IncidentStatus.RESOLVED.value


def test_database_size_incident_is_never_auto_resolved_by_recovery(isolated_db):
    # 2026-08-10: same bug class/guard as CRUSH_SKEW_USE/PG above --
    # watcher/database_capacity_monitor.py's synthetic Incident (ceph_code
    # "DATABASE_SIZE_HIGH", no dynamic suffix -- there is only ever one
    # database) never matches a real `ceph health detail` check code
    # either. Without this guard, it would self-resolve on the very next
    # poll, hiding it from the operator before it can ever be seen at
    # PENDING_APPROVAL.
    pending_id = _seed_incident("DATABASE_SIZE_HIGH", IncidentStatus.PENDING_APPROVAL.value)
    real_failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, pending_id).status == IncidentStatus.PENDING_APPROVAL.value
        assert session.get(Incident, real_failed_id).status == IncidentStatus.RESOLVED.value


def test_already_terminal_incidents_are_never_touched_by_recovery(isolated_db):
    resolved_id = _seed_incident("OSD_DOWN", IncidentStatus.RESOLVED.value)
    auto_fixed_id = _seed_incident("MON_CLOCK_SKEW", IncidentStatus.AUTO_FIXED.value)
    rejected_id = _seed_incident("PG_DEGRADED", IncidentStatus.REJECTED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, resolved_id).status == IncidentStatus.RESOLVED.value
        assert session.get(Incident, auto_fixed_id).status == IncidentStatus.AUTO_FIXED.value
        assert session.get(Incident, rejected_id).status == IncidentStatus.REJECTED.value




def test_incident_created_and_published_on_transition_to_warn(isolated_db, monkeypatch):
    published = []
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async(published))
    monkeypatch.setattr(
        watcher_main.collector,
        "collect_relevant_logs",
        lambda code, detail, **_kw: (["10.20.1.249"], "mon2 log excerpt"),
    )

    watcher_main.build_and_publish_incident(None, HEALTH_WARN_PAYLOAD)

    with db_module.SessionLocal() as session:
        rows = session.query(Incident).all()
        assert len(rows) == 1
        assert rows[0].ceph_code == "MON_CLOCK_SKEW"
        assert rows[0].status == IncidentStatus.NEW.value
        assert rows[0].log_excerpt == "mon2 log excerpt"
        assert rows[0].severity == "HEALTH_WARN"
        db_incident_id = rows[0].id

    assert len(published) == 1
    envelope = published[0]
    assert envelope["incident_id"] == db_incident_id  # AC #3: DB row <-> message linkage
    assert envelope["ceph_code"] == "MON_CLOCK_SKEW"
    assert envelope["nodes"] == ["10.20.1.249"]
    assert envelope["log_excerpt"] == "mon2 log excerpt"


def test_capacity_incident_freezes_structured_metric_evidence(isolated_db, monkeypatch):
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async([]))
    monkeypatch.setattr(
        watcher_main.collector, "collect_relevant_logs", lambda *a, **k: ([], "nearfull")
    )
    snapshot = '{"source":"ceph_capacity_snapshot","cluster":{"used_percent":91.2}}'
    monkeypatch.setattr(
        watcher_main.capacity_evidence, "collect_capacity_evidence", lambda *a, **k: snapshot
    )
    watcher_main.build_and_publish_incident(None, {
        "status": "HEALTH_WARN",
        "checks": {"OSD_NEARFULL": {"severity": "HEALTH_WARN"}},
    })
    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="OSD_NEARFULL").one()
        assert incident.signal_evidence_json == snapshot


def test_failed_incident_does_not_permanently_block_a_fresh_remediation_attempt(
    isolated_db, monkeypatch
):
    _seed_incident("BLUESTORE_SLOW_OP_ALERT", IncidentStatus.FAILED.value)
    published = []
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async(published))
    monkeypatch.setattr(
        watcher_main.collector,
        "collect_relevant_logs",
        lambda *a, **k: (["10.20.1.83"], "osd.4 slow"),
    )
    monkeypatch.setattr(
        watcher_main.capacity_evidence, "collect_capacity_evidence", lambda *a, **k: None
    )

    watcher_main.build_and_publish_incident(None, {
        "status": "HEALTH_WARN",
        "checks": {
            "BLUESTORE_SLOW_OP_ALERT": {
                "severity": "HEALTH_WARN",
                "detail": [{"message": "osd.4 observed slow operations in BlueStore"}],
            }
        },
    })

    assert len(published) == 1
    with db_module.SessionLocal() as session:
        rows = session.query(Incident).filter_by(ceph_code="BLUESTORE_SLOW_OP_ALERT").all()
        assert len(rows) == 2
        assert sorted(row.status for row in rows) == [
            IncidentStatus.FAILED.value, IncidentStatus.NEW.value,
        ]


def test_recurrent_osd_down_retries_after_executed_action_verification_window(
    isolated_db, monkeypatch
):
    incident_id = _seed_incident("OSD_DOWN", IncidentStatus.VERIFYING.value)
    with db_module.SessionLocal() as session:
        session.add(Action(
            incident_id=incident_id, action_id="restart_osd_daemon",
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.AUTO_EXECUTED.value,
            executed_at=datetime.utcnow() - timedelta(seconds=31),
        ))
        session.commit()
    published = []
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async(published))
    monkeypatch.setattr(
        watcher_main.collector, "collect_relevant_logs",
        lambda *a, **k: (["10.20.1.83"], "osd.0 down"),
    )
    monkeypatch.setattr(
        watcher_main.capacity_evidence, "collect_capacity_evidence", lambda *a, **k: None,
    )

    watcher_main.build_and_publish_incident(None, {
        "status": "HEALTH_WARN",
        "checks": {"OSD_DOWN": {
            "severity": "HEALTH_WARN",
            "detail": [{"message": "osd.0 is down"}],
        }},
    })

    assert len(published) == 1
    with db_module.SessionLocal() as session:
        assert session.query(Incident).filter_by(ceph_code="OSD_DOWN").count() == 2


def test_incident_creation_sends_telegram_before_ai_diagnosis(isolated_db, monkeypatch):
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async([]))
    monkeypatch.setattr(
        watcher_main.collector,
        "collect_relevant_logs",
        lambda code, detail, **_kw: ([], "mon2 log excerpt"),
    )
    calls = []
    monkeypatch.setattr(
        watcher_main.telegram_alerts,
        "send_incident_alert",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    watcher_main.build_and_publish_incident(None, HEALTH_WARN_PAYLOAD)

    assert calls == [(('MON_CLOCK_SKEW', 'HEALTH_WARN', 'mon2 log excerpt'), {})]


def test_no_telegram_alert_sent_on_recovery_to_health_ok(isolated_db, monkeypatch):
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async([]))
    calls = []
    monkeypatch.setattr(
        watcher_main.telegram_alerts,
        "send_incident_alert",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    watcher_main.build_and_publish_incident("HEALTH_WARN", HEALTH_OK_PAYLOAD)

    assert calls == []


def test_open_incident_is_reminded_hourly_until_resolved(isolated_db, monkeypatch):
    now = datetime(2026, 8, 14, 12, 0, 0)
    with db_module.SessionLocal() as session:
        due = Incident(
            ceph_code="OSD_DOWN",
            status=IncidentStatus.FAILED.value,
            severity="HEALTH_ERR",
            log_excerpt="osd.2 down",
            diagnosis_text="OSD.2 đã dừng do tiến trình bị lỗi.",
            detected_at=now - timedelta(hours=2),
            created_at=now - timedelta(hours=2),
        )
        resolved = Incident(
            ceph_code="MON_DOWN",
            status=IncidentStatus.RESOLVED.value,
            severity="HEALTH_ERR",
            detected_at=now - timedelta(hours=2),
            created_at=now - timedelta(hours=2),
        )
        session.add_all([due, resolved])
        session.flush()
        session.add(Action(
            incident_id=due.id,
            action_id="restart_osd_daemon",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale="Khởi động lại daemon OSD.2 để phục hồi dịch vụ.",
        ))
        session.commit()
        due_id = due.id

    calls = []
    monkeypatch.setattr(
        watcher_main.telegram_alerts,
        "send_incident_alert",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        watcher_main.settings, "telegram_incident_reminder_interval_seconds", 3600
    )

    assert watcher_main.send_due_incident_reminders(now) == 1
    assert calls[0][0][:3] == ("OSD_DOWN", "HEALTH_ERR", "osd.2 down")
    assert calls[0][1]["reminder"] is True
    assert calls[0][1]["diagnosis_text"] == "OSD.2 đã dừng do tiến trình bị lỗi."
    assert calls[0][1]["rationale"] == "Khởi động lại daemon OSD.2 để phục hồi dịch vụ."
    assert watcher_main.send_due_incident_reminders(now + timedelta(minutes=59)) == 0
    assert watcher_main.send_due_incident_reminders(now + timedelta(hours=1)) == 1

    with db_module.SessionLocal() as session:
        assert session.get(Incident, due_id).telegram_reminded_at == now + timedelta(hours=1)


def test_reminders_exclude_upgrade_and_collapse_duplicate_cluster_health_rows(
    isolated_db, monkeypatch
):
    now = datetime(2026, 8, 21, 12, 0, 0)
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="backup",
            ceph_mon_nodes="10.0.0.1",
            ssh_user="root",
            ssh_key_path="/tmp/test-key",
            is_default=False,
        )
        session.add(cluster)
        session.flush()
        session.add_all(
            [
                Incident(
                    cluster_id=cluster.id,
                    ceph_code="POOL_APP_NOT_ENABLED",
                    status=IncidentStatus.FAILED.value,
                    log_excerpt="old duplicate",
                    detected_at=now - timedelta(hours=3),
                    created_at=now - timedelta(hours=3),
                ),
                Incident(
                    cluster_id=cluster.id,
                    ceph_code="POOL_APP_NOT_ENABLED",
                    status=IncidentStatus.FAILED.value,
                    log_excerpt="newest evidence",
                    detected_at=now - timedelta(hours=2),
                    created_at=now - timedelta(hours=2),
                ),
                Incident(
                    cluster_id=cluster.id,
                    ceph_code="CLUSTER_UPGRADE",
                    status=IncidentStatus.PENDING_APPROVAL.value,
                    log_excerpt="upgrade proposal",
                    detected_at=now - timedelta(hours=2),
                    created_at=now - timedelta(hours=2),
                ),
            ]
        )
        session.commit()

    calls = []
    monkeypatch.setattr(
        watcher_main.telegram_alerts,
        "send_incident_alert",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        watcher_main.settings, "telegram_incident_reminder_interval_seconds", 3600
    )

    assert watcher_main.send_due_incident_reminders(now) == 1
    assert calls[0][0][:3] == (
        "POOL_APP_NOT_ENABLED",
        None,
        "newest evidence",
    )


def test_reminders_skip_stale_incidents_from_inactive_cluster(isolated_db, monkeypatch):
    now = datetime(2026, 8, 21, 12, 0, 0)
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="disabled-backup", ceph_mon_nodes="10.0.0.1",
            ssh_user="root", ssh_key_path="/tmp/test-key",
            is_default=False, is_active=False,
        )
        session.add(cluster)
        session.flush()
        session.add(Incident(
            cluster_id=cluster.id,
            ceph_code="POOL_APP_NOT_ENABLED",
            status=IncidentStatus.FAILED.value,
            detected_at=now - timedelta(days=10),
            created_at=now - timedelta(days=10),
        ))
        session.commit()
    calls = []
    monkeypatch.setattr(
        watcher_main.telegram_alerts, "send_incident_alert",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert watcher_main.send_due_incident_reminders(now) == 0
    assert calls == []


def test_observed_cluster_does_not_create_duplicate_open_incident(isolated_db, monkeypatch):
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="backup",
            ceph_mon_nodes="10.0.0.1",
            ssh_user="root",
            ssh_key_path="/tmp/test-key",
            is_default=False,
        )
        session.add(cluster)
        session.flush()
        session.add(
            Incident(
                cluster_id=cluster.id,
                ceph_code="POOL_APP_NOT_ENABLED",
                status=IncidentStatus.FAILED.value,
                detected_at=datetime.utcnow(),
            )
        )
        session.commit()
        cluster_id = cluster.id

    with db_module.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id)
        monkeypatch.setattr(
            watcher_main.collector,
            "collect_relevant_logs",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not recollect")),
        )
        watcher_main._build_and_publish_incident_for_observed_cluster(
            cluster,
            {
                "status": "HEALTH_WARN",
                "checks": {
                    "POOL_APP_NOT_ENABLED": {
                        "severity": "HEALTH_WARN",
                        "detail": [],
                    }
                },
            },
        )

    with db_module.SessionLocal() as session:
        assert (
            session.query(Incident)
            .filter_by(cluster_id=cluster_id, ceph_code="POOL_APP_NOT_ENABLED")
            .count()
            == 1
        )


def test_multiple_simultaneous_checks_create_one_incident_each_and_publish_all(
    isolated_db, monkeypatch
):
    published = []
    collected_codes = []

    def fake_collect(code, detail, **_kw):
        collected_codes.append(code)
        return ([f"node-for-{code}"], f"log for {code}")

    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async(published))
    monkeypatch.setattr(watcher_main.collector, "collect_relevant_logs", fake_collect)

    watcher_main.build_and_publish_incident("HEALTH_WARN", MULTI_CHECK_PAYLOAD)

    with db_module.SessionLocal() as session:
        rows = session.query(Incident).all()
        assert len(rows) == 2
        db_codes = {row.ceph_code for row in rows}
        assert db_codes == {"MON_CLOCK_SKEW", "OSD_DOWN"}
        assert all(row.status == IncidentStatus.NEW.value for row in rows)
        severity_by_code = {row.ceph_code: row.severity for row in rows}
        assert severity_by_code == {"MON_CLOCK_SKEW": "HEALTH_WARN", "OSD_DOWN": "HEALTH_ERR"}

    # One collect_relevant_logs call per check, and both checks collected.
    assert set(collected_codes) == {"MON_CLOCK_SKEW", "OSD_DOWN"}

    # Batch-published exactly once per incident, in a single asyncio.run() —
    # not zero, not merged into one message.
    assert len(published) == 2
    published_codes = {envelope["ceph_code"] for envelope in published}
    assert published_codes == {"MON_CLOCK_SKEW", "OSD_DOWN"}
    for envelope in published:
        assert envelope["nodes"] == [f"node-for-{envelope['ceph_code']}"]
        assert envelope["log_excerpt"] == f"log for {envelope['ceph_code']}"


def _record_async(sink: list):
    async def _fake_publish(envelope: dict) -> None:
        sink.append(envelope)

    return _fake_publish
