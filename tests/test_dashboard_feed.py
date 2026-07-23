from datetime import datetime, timedelta

from sqlalchemy.exc import OperationalError

from shared import db as db_module
from shared.models import AuditEntry, Incident, WatcherHeartbeat


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_index_shows_incident_from_db(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(
            Incident(ceph_code="OSD_DOWN", status="NEW", detected_at=datetime.utcnow())
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "OSD_DOWN" in response.text


def test_index_shows_diagnosis_text_as_the_error_reason(dashboard_client):
    # 2026-07-23: the Incident Feed was simplified to just "Mã lỗi" + "Lý do
    # lỗi" (Chat-with-AI now covers everything the removed status/severity/
    # approve-action columns used to show) — diagnosis_text is the "reason".
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                ceph_code="OSD_DOWN",
                status="NEW",
                detected_at=datetime.utcnow(),
                severity="HEALTH_ERR",
                diagnosis_text="OSD.3 bị crash do hết dung lượng đĩa.",
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "OSD.3 bị crash do hết dung lượng đĩa." in response.text


def test_index_shows_placeholder_when_incident_has_no_diagnosis_yet(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(Incident(ceph_code="OSD_DOWN", status="NEW", detected_at=datetime.utcnow()))
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Chưa có chẩn đoán." in response.text


def test_index_shows_empty_state_when_no_incidents(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Chưa có Incident" in response.text


def test_index_handles_db_error_gracefully(dashboard_client, monkeypatch):
    _login(dashboard_client)

    def _broken_session_local():
        raise OperationalError("SELECT 1", {}, Exception("db unreachable"))

    monkeypatch.setattr(db_module, "SessionLocal", _broken_session_local)
    response = dashboard_client.get("/")

    assert response.status_code == 503


def test_index_handles_non_db_error_gracefully_not_raw_500(dashboard_client, monkeypatch):
    # Review Story 5.2: compute_cluster_status()/is_heartbeat_stale() run
    # inside the same try as the DB fetch — a bug there (anything that
    # isn't a SQLAlchemyError) must still return a clean error response,
    # not leak a raw unhandled-exception 500/stack trace to the browser.
    import dashboard.routes.incidents as incidents_route

    def _broken_is_heartbeat_stale(_heartbeat):
        raise RuntimeError("boom")

    monkeypatch.setattr(incidents_route, "is_heartbeat_stale", _broken_is_heartbeat_stale)

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 500
    assert "boom" not in response.text


# --- Story 5.2: heartbeat display on the main page -------------------------


def test_index_shows_connection_lost_warning_when_never_polled(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Mất kết nối cụm Ceph" in response.text


def test_index_shows_connection_lost_warning_when_last_poll_failed(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(
            WatcherHeartbeat(
                id=1,
                success=False,
                mon_node=None,
                error_message="All MON nodes failed: timed out",
                polled_at=datetime.utcnow(),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Mất kết nối cụm Ceph" in response.text
    assert "All MON nodes failed: timed out" in response.text


def test_index_shows_connection_lost_warning_when_poll_too_old(dashboard_client, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "watcher_poll_interval_seconds", 15)
    with db_module.SessionLocal() as session:
        session.add(
            WatcherHeartbeat(
                id=1,
                success=True,
                mon_node="10.20.1.150",
                error_message=None,
                polled_at=datetime.utcnow() - timedelta(seconds=15 * 3 + 1),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Mất kết nối cụm Ceph" in response.text


def test_index_shows_healthy_connection_details_when_recent_and_successful(dashboard_client, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "watcher_poll_interval_seconds", 15)
    with db_module.SessionLocal() as session:
        session.add(
            WatcherHeartbeat(
                id=1,
                success=True,
                mon_node="10.20.1.150",
                error_message=None,
                polled_at=datetime.utcnow() - timedelta(seconds=2),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Mất kết nối cụm Ceph" not in response.text
    assert "10.20.1.150" in response.text


# --- Audit Trail preview on the Dashboard page ------------------------------


def test_index_shows_audit_trail_section_with_recent_entries(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(
            Incident(id="inc-1", ceph_code="OSD_DOWN", status="NEW", detected_at=datetime.utcnow())
        )
        session.add(
            AuditEntry(
                incident_id="inc-1",
                action_id=None,
                event_type="INCIDENT_DETECTED",
                actor="system",
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert '<h2>Audit Trail</h2>' in response.text
    assert 'id="audit-feed-preview"' in response.text
    assert "INCIDENT_DETECTED" in response.text
    assert 'href="/audit"' in response.text
    assert "Xem tất cả" in response.text


def test_index_shows_empty_state_when_no_audit_entries(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Chưa có hoạt động nào." in response.text


def test_index_audit_trail_preview_caps_at_20_most_recent_entries(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(
            Incident(id="inc-1", ceph_code="OSD_DOWN", status="NEW", detected_at=datetime.utcnow())
        )
        base = datetime.utcnow()
        for i in range(25):
            session.add(
                AuditEntry(
                    incident_id="inc-1",
                    action_id=None,
                    event_type=f"EVENT_{i}",
                    actor="system",
                    created_at=base + timedelta(seconds=i),
                )
            )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    # Newest 20 (EVENT_5..EVENT_24) shown, oldest 5 (EVENT_0..EVENT_4) not.
    for i in range(5, 25):
        assert f"EVENT_{i}" in response.text
    for i in range(0, 5):
        assert f"EVENT_{i}<" not in response.text
