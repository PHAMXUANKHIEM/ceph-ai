from datetime import datetime

import dashboard.ws as ws_module
from shared import db as db_module
from shared.cluster_snapshot import publish_snapshot
from shared.models import Incident, WatcherHeartbeat


def test_unauthenticated_websocket_is_rejected(dashboard_client):
    try:
        with dashboard_client.websocket_connect("/ws/incidents"):
            connected = True
    except Exception:
        connected = False
    assert not connected, "unauthenticated client should not be able to use the incidents websocket"


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

    with dashboard_client.websocket_connect(
        f"/ws/cluster-state?cluster_id={default_cluster_id}"
    ) as websocket:
        publish_snapshot(default_cluster_id, {"pools": [{"name": "rbd"}]})
        message = websocket.receive_json()

    assert message["event"] == "snapshot_changed"
    assert message["cluster_id"] == default_cluster_id
    assert message["sections"] == ["pools"]
    assert isinstance(message["generation"], int)


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
