from datetime import datetime

import pytest

import dashboard.ws as ws_module
from shared import db as db_module
from shared.cluster_snapshot import publish_snapshot
from shared.models import Action, ActionClassification, ActionStatus, Incident, WatcherHeartbeat
from shared.models import User


def test_unauthenticated_websocket_is_rejected(dashboard_client):
    try:
        with dashboard_client.websocket_connect("/ws/incidents"):
            connected = True
    except Exception:
        connected = False
    assert not connected, "unauthenticated client should not be able to use the incidents websocket"


def test_revoked_database_user_websocket_is_rejected(dashboard_client):
    import bcrypt

    with db_module.SessionLocal() as session:
        session.add(User(
            username="ws-revoked",
            password_hash=bcrypt.hashpw(b"operator-password", bcrypt.gensalt()).decode(),
            is_admin=False,
            is_active=True,
            created_by="admin",
        ))
        session.commit()
    login = dashboard_client.post(
        "/login",
        data={"username": "ws-revoked", "password": "operator-password", "product": "ceph"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    with db_module.SessionLocal() as session:
        user = session.query(User).filter_by(username="ws-revoked").one()
        user.is_active = False
        user.session_version += 1
        session.commit()

    with pytest.raises(Exception):
        with dashboard_client.websocket_connect("/ws/incidents") as websocket:
            websocket.receive_json()


def test_authenticated_websocket_receives_change_notification(dashboard_client, monkeypatch):
    monkeypatch.setattr(ws_module, "POLL_INTERVAL_SECONDS", 0.05)
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    with dashboard_client.websocket_connect("/ws/incidents") as websocket:
        with db_module.SessionLocal() as session:
            session.add(
                Incident(ceph_code="OSD_DOWN", status="NEW", detected_at=datetime.utcnow())
            )
            session.commit()

        message = websocket.receive_json()
        assert message == {"event": "incidents_changed"}


# Heartbeats are intentionally not browser-refresh events -------------------


def test_snapshot_does_not_change_when_heartbeat_recorded_without_incident_change(
    dashboard_client, default_cluster_id
):
    # dashboard_client fixture already points db_module.SessionLocal at an
    # isolated in-memory DB — no Incident is touched here at all.
    before = ws_module._snapshot()

    with db_module.SessionLocal() as session:
        session.add(
            WatcherHeartbeat(
                cluster_id=default_cluster_id,
                success=True,
                mon_node="10.20.1.150",
                error_message=None,
                polled_at=datetime.utcnow(),
            )
        )
        session.commit()

    after = ws_module._snapshot()

    assert before == after


def test_authenticated_websocket_receives_snapshot_change_notification(
    dashboard_client, default_cluster_id, monkeypatch
):
    monkeypatch.setattr(ws_module, "POLL_INTERVAL_SECONDS", 0.05)
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    with dashboard_client.websocket_connect("/ws/incidents") as websocket:
        publish_snapshot(default_cluster_id, {"health": {"status": "HEALTH_WARN"}})
        message = websocket.receive_json()

    assert message == {
        "event": "snapshot_changed",
        "cluster_id": default_cluster_id,
        "sections": ["health"],
    }


def test_cluster_state_websocket_receives_scoped_event(
    dashboard_client, default_cluster_id, monkeypatch
):
    monkeypatch.setattr(ws_module, "POLL_INTERVAL_SECONDS", 0.05)
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    before = ws_module.get_metrics()

    with dashboard_client.websocket_connect(
        f"/ws/cluster-state?cluster_id={default_cluster_id}"
    ) as websocket:
        publish_snapshot(default_cluster_id, {"pools": [{"name": "rbd"}]})
        message = websocket.receive_json()

    assert message["event"] == "snapshot_changed"
    assert message["cluster_id"] == default_cluster_id
    assert message["sections"] == ["pools"]
    assert isinstance(message["generation"], int)
    after = ws_module.get_metrics()
    assert after["cluster_state_connections_total"] >= before["cluster_state_connections_total"] + 1
    assert after["cluster_state_messages_total"] >= before["cluster_state_messages_total"] + 1
    assert after["cluster_state_disconnects_total"] >= before["cluster_state_disconnects_total"] + 1


def test_action_state_event_is_published_after_database_commit(
    dashboard_client, default_cluster_id, monkeypatch
):
    from shared.cluster_events import read_latest_event

    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id=default_cluster_id,
            ceph_code="OSD_DOWN",
            status="NEW",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="restart_osd_daemon",
            classification=ActionClassification.SAFE.value,
            status="PENDING",
        )
        session.add(action)
        session.commit()
        action_id = action.id

        action.status = "EXECUTING"
        session.commit()

    event = read_latest_event(default_cluster_id)
    assert event["event"] == "action_state_changed"
    assert event["action_id"] == action_id
    assert event["action_status"] == "EXECUTING"
    assert event["action_state"] == "running"


def test_action_lifecycle_realtime_contract_reaches_postcheck_success(
    dashboard_client, default_cluster_id
):
    from shared.cluster_events import read_latest_event

    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id=default_cluster_id,
            ceph_code="OSD_DOWN",
            status="NEW",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="restart_osd_daemon",
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.PENDING.value,
        )
        session.add(action)
        session.commit()
        action_id = action.id

        assert read_latest_event(default_cluster_id)["action_state"] == "queued"

        action.status = ActionStatus.EXECUTING.value
        session.commit()
        assert read_latest_event(default_cluster_id)["action_state"] == "running"

        action.status = ActionStatus.AUTO_EXECUTED.value
        incident.status = "VERIFYING"
        session.commit()
        verifying = read_latest_event(default_cluster_id)
        assert verifying["event"] == "action_state_changed"
        assert verifying["action_id"] == action_id
        assert verifying["action_state"] == "verifying"

        session.refresh(incident)
        incident.status = "RESOLVED"
        session.commit()

    succeeded = read_latest_event(default_cluster_id)
    assert succeeded["event"] == "snapshot_changed"
    assert succeeded["action_id"] == action_id
    assert succeeded["action_status"] == ActionStatus.AUTO_EXECUTED.value
    assert succeeded["action_state"] == "succeeded"


def test_failed_postcheck_publishes_error_without_claiming_snapshot_success(
    dashboard_client, default_cluster_id
):
    from shared.cluster_events import read_latest_event

    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id=default_cluster_id,
            ceph_code="OSD_DOWN",
            status="VERIFYING",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="restart_osd_daemon",
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.AUTO_EXECUTED.value,
        )
        session.add(action)
        session.commit()
        action_id = action.id

        session.refresh(incident)
        incident.status = "FAILED"
        session.commit()

    event = read_latest_event(default_cluster_id)
    assert event["event"] == "snapshot_refresh_failed"
    assert event["action_id"] == action_id
    assert event["action_status"] == ActionStatus.AUTO_EXECUTED.value
    assert event["action_state"] == "failed"
    assert event["sections"] == ["health", "status", "nodes"]


def test_action_state_event_is_not_published_for_rolled_back_transition(
    dashboard_client, default_cluster_id
):
    from shared.cluster_events import read_latest_event

    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id=default_cluster_id,
            ceph_code="PG_DEGRADED",
            status="NEW",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="pg_repair_force",
            classification=ActionClassification.SAFE.value,
            status="PENDING",
        )
        session.add(action)
        session.commit()
        action_id = action.id

        action.status = "EXECUTING"
        session.flush()
        session.rollback()

    event = read_latest_event(default_cluster_id)
    assert event["event"] == "action_state_changed"
    assert event["action_id"] == action_id
    assert event["action_status"] == "PENDING"


def test_resolved_incident_publishes_snapshot_invalidation_after_commit(
    dashboard_client, default_cluster_id
):
    from shared import ceph_query_cache
    from shared.cluster_events import EVENT_NAMESPACE, read_latest_event

    ceph_query_cache.invalidate(EVENT_NAMESPACE, default_cluster_id)
    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id=default_cluster_id,
            ceph_code="POOL_FULL",
            status="NEW",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.commit()
        ceph_query_cache.invalidate(EVENT_NAMESPACE, default_cluster_id)

        incident.status = "RESOLVED"
        session.commit()

    event = read_latest_event(default_cluster_id)
    assert event["event"] == "snapshot_changed"
    assert event["cluster_id"] == default_cluster_id
    assert event["sections"] == ["health", "status", "pools"]
    assert "action_state" not in event


def test_manual_resolution_with_action_does_not_claim_postcheck_success(
    dashboard_client, default_cluster_id
):
    from shared import ceph_query_cache
    from shared.cluster_events import EVENT_NAMESPACE, read_latest_event

    ceph_query_cache.invalidate(EVENT_NAMESPACE, default_cluster_id)
    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id=default_cluster_id,
            ceph_code="POOL_FULL",
            status="NEW",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        session.add(
            Action(
                incident_id=incident.id,
                action_id="pool_scrub",
                classification=ActionClassification.SAFE.value,
                status=ActionStatus.EXECUTED.value,
            )
        )
        session.commit()
        ceph_query_cache.invalidate(EVENT_NAMESPACE, default_cluster_id)

        incident.status = "RESOLVED"
        session.commit()

    event = read_latest_event(default_cluster_id)
    assert event["event"] == "snapshot_changed"
    assert event["action_status"] == ActionStatus.EXECUTED.value
    assert event["action_state"] == "verifying"
    assert event["action_state"] != "succeeded"


def test_postcheck_snapshot_event_carries_bounded_action_metadata(
    dashboard_client, default_cluster_id
):
    from shared import ceph_query_cache
    from shared.cluster_events import EVENT_NAMESPACE, read_latest_event

    ceph_query_cache.invalidate(EVENT_NAMESPACE, default_cluster_id)
    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id=default_cluster_id,
            ceph_code="CRUSH_MAP_DRIFT",
            status="NEW",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="reweight_osd",
            classification=ActionClassification.RISKY.value,
            status="EXECUTING",
        )
        session.add(action)
        session.commit()
        action_pk = action.id
        ceph_query_cache.invalidate(EVENT_NAMESPACE, default_cluster_id)

        action.status = "EXECUTED"
        incident.status = "VERIFYING"
        session.commit()
        session.refresh(incident)
        incident.status = "RESOLVED"
        session.commit()

    event = read_latest_event(default_cluster_id)
    assert event["event"] == "snapshot_changed"
    assert event["action_id"] == action_pk
    assert event["action_status"] == "EXECUTED"
    assert event["action_state"] == "succeeded"
    assert event["sections"] == ["health", "status", "crush"]
    assert "proposed_command" not in event
    assert "target_nodes" not in event


@pytest.mark.parametrize(
    ("ceph_code", "expected_section"),
    [("CRUSH_MAP_DRIFT", "crush"), ("RGW_BUCKET_QUOTA", "status"), ("DEPLOY_FAILED", "nodes")],
)
def test_resolved_incident_maps_mutation_scope_to_snapshot_section(
    dashboard_client, default_cluster_id, ceph_code, expected_section
):
    from shared import ceph_query_cache
    from shared.cluster_events import EVENT_NAMESPACE, read_latest_event

    ceph_query_cache.invalidate(EVENT_NAMESPACE, default_cluster_id)
    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id=default_cluster_id,
            ceph_code=ceph_code,
            status="NEW",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.commit()
        ceph_query_cache.invalidate(EVENT_NAMESPACE, default_cluster_id)
        incident.status = "RESOLVED"
        session.commit()

    event = read_latest_event(default_cluster_id)
    assert event["event"] == "snapshot_changed"
    assert expected_section in event["sections"]


def test_poller_detects_changes_without_deserializing_snapshots(
    dashboard_client, default_cluster_id, monkeypatch
):
    """`_snapshot` chạy mỗi POLL_INTERVAL_SECONDS cho MỖI tab đang mở. Trước
    đây nó đọc trọn 6 snapshot chỉ để so 6 số `generation`, tức deserialize
    và deep-copy vài chục KB PG/pool/CRUSH mỗi lượt, trong một lock toàn cục.
    Nếu có ai đọc payload trở lại, test này sẽ nổ."""
    from shared import ceph_query_cache

    # Chặn ở TẦNG CACHE chứ không ở hàm read_*: mọi đường đọc payload đều
    # phải đi qua get_cached, còn fingerprint chỉ gọi stat().
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("poller không được deserialize payload snapshot")

    monkeypatch.setattr(ceph_query_cache, "get_cached", _forbidden)

    before = ws_module._snapshot()
    publish_snapshot(default_cluster_id, {"health": {"status": "HEALTH_WARN"}})
    after = ws_module._snapshot()

    assert before != after
