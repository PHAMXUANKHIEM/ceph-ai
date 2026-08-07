from datetime import datetime

import dashboard.ws as ws_module
from shared import db as db_module
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


# --- Story 5.2: _snapshot() must also fingerprint WatcherHeartbeat ---------


def test_snapshot_changes_when_heartbeat_recorded_even_without_incident_change(
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

    assert before != after


def test_authenticated_websocket_receives_change_notification_on_heartbeat_update(
    dashboard_client, monkeypatch, default_cluster_id
):
    monkeypatch.setattr(ws_module, "POLL_INTERVAL_SECONDS", 0.05)
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    with dashboard_client.websocket_connect("/ws/incidents") as websocket:
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

        message = websocket.receive_json()
        assert message == {"event": "incidents_changed"}
