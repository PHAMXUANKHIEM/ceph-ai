from datetime import datetime

import dashboard.routes.volumes as volumes_route
from config.settings import settings
from shared import db as db_module
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from watcher.ceph_client import CephQueryError


def _login(client):
    # dashboard_client fixture (conftest.py) pins these credentials.
    client.post("/login", data={"username": "admin", "password": "admin"})


def _configure_pools(monkeypatch):
    monkeypatch.setattr(settings, "ceph_rbd_pools", "vms,backups")


def _stub_no_trash(monkeypatch):
    # Avoids the route's real SSH call (query_rbd_trash) in tests that
    # don't care about its content — every dashboard-level test in this
    # file that renders /volumes?pool=... with a selected pool now also
    # queries trash, matching /nodes/test_dashboard_upgrade.py's own
    # "always mock live SSH calls explicitly" discipline (an earlier real
    # slowdown was found from a test that didn't).
    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_trash", lambda pool: [])


def test_unauthenticated_get_volumes_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/volumes", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_unauthenticated_iostat_api_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/api/volumes/vms/iostat", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_volumes_page_lists_configured_pools(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volumes")

    assert response.status_code == 200
    assert "vms" in response.text
    assert "backups" in response.text


def test_volumes_page_with_no_pool_selects_nothing_and_shows_empty_state(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volumes")

    assert response.status_code == 200
    assert 'id="volumes-panel"' not in response.text
    assert "Chọn một pool để xem hiệu năng Volume" in response.text


def test_volumes_page_with_explicit_pool_selects_it(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volumes?pool=vms")

    assert response.status_code == 200
    assert 'id="volumes-panel"' in response.text
    assert 'data-pool="vms"' in response.text


def test_volumes_page_rejects_pool_not_in_configured_list(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volumes?pool=unknown-pool")

    assert response.status_code == 404


def test_volumes_page_shows_hint_when_no_pools_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_rbd_pools", "")
    _login(dashboard_client)

    response = dashboard_client.get("/volumes")

    assert response.status_code == 200
    assert "CEPH_RBD_POOLS" in response.text


def test_iostat_api_returns_samples_for_configured_pool(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    fake_samples = [
        {"pool": "vms", "image": "disk-1", "iops": 120.0, "read_latency_ms": 1.0, "write_latency_ms": 2.0}
    ]
    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_iostat", lambda pool: fake_samples)

    response = dashboard_client.get("/api/volumes/vms/iostat")

    assert response.status_code == 200
    body = response.json()
    assert body["pool"] == "vms"
    assert body["images"] == [
        {
            "image": "disk-1",
            "iops": 120.0,
            "read_latency_ms": 1.0,
            "write_latency_ms": 2.0,
            "saturated": False,
        }
    ]


def test_iostat_api_rejects_pool_not_in_configured_list_without_querying(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    calls = []
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_iostat", lambda pool: calls.append(pool) or []
    )

    response = dashboard_client.get("/api/volumes/unknown-pool/iostat")

    assert response.status_code == 404
    assert calls == []  # whitelist check happens before any SSH attempt


def test_iostat_api_returns_502_when_query_fails(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    def fake_query(pool):
        raise CephQueryError("all MON nodes failed")

    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_iostat", fake_query)

    response = dashboard_client.get("/api/volumes/vms/iostat")

    assert response.status_code == 502


def test_iostat_api_marks_image_saturated_when_incident_open(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    fake_samples = [
        {"pool": "vms", "image": "disk-1", "iops": 95.0, "read_latency_ms": 10.0, "write_latency_ms": 8.0}
    ]
    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_iostat", lambda pool: fake_samples)

    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                ceph_code="VOLUME_SATURATED:vms/disk-1",
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
            )
        )
        session.commit()

    response = dashboard_client.get("/api/volumes/vms/iostat")

    assert response.status_code == 200
    assert response.json()["images"][0]["saturated"] is True


def test_iostat_api_does_not_mark_saturated_when_incident_already_resolved(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    fake_samples = [
        {"pool": "vms", "image": "disk-1", "iops": 95.0, "read_latency_ms": 10.0, "write_latency_ms": 8.0}
    ]
    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_iostat", lambda pool: fake_samples)

    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                ceph_code="VOLUME_SATURATED:vms/disk-1",
                status=IncidentStatus.RESOLVED.value,
                detected_at=datetime.utcnow(),
            )
        )
        session.commit()

    response = dashboard_client.get("/api/volumes/vms/iostat")

    assert response.status_code == 200
    assert response.json()["images"][0]["saturated"] is False


# --- Trash (2026-07-28) -------------------------------------------------


def _fake_trash_entry(entry_id="1234567890ab", name="old-disk"):
    return {"id": entry_id, "name": name, "deletion_time": "2026-07-28 10:00:00", "status": "expired"}


def test_volumes_page_shows_trash_entries(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_trash", lambda pool: [_fake_trash_entry()]
    )
    _login(dashboard_client)

    response = dashboard_client.get("/volumes?pool=vms")

    assert response.status_code == 200
    assert "old-disk" in response.text
    assert "1234567890ab" in response.text


def test_volumes_page_shows_empty_trash_hint(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volumes?pool=vms")

    assert response.status_code == 200
    assert "Trash của pool này đang trống" in response.text


def test_volumes_page_shows_trash_error_without_crashing(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)

    def fake_query(pool):
        raise CephQueryError("all MON nodes failed")

    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_trash", fake_query)
    _login(dashboard_client)

    response = dashboard_client.get("/volumes?pool=vms")

    assert response.status_code == 200
    assert "Không lấy được danh sách trash" in response.text


def test_volumes_page_shows_xoa_button_when_no_pending_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_trash", lambda pool: [_fake_trash_entry()]
    )
    _login(dashboard_client)

    response = dashboard_client.get("/volumes?pool=vms")

    assert response.status_code == 200
    assert 'action="/volumes/vms/trash/1234567890ab/propose"' in response.text
    assert "Chờ duyệt" not in response.text


def test_volumes_page_shows_pending_approval_state_instead_of_xoa_button(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_trash", lambda pool: [_fake_trash_entry()]
    )
    _login(dashboard_client)

    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code="RBD_TRASH_REMOVE",
            status=IncidentStatus.PENDING_APPROVAL.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="rbd_trash_remove",
            classification="RISKY",
            status=ActionStatus.PENDING_APPROVAL.value,
            action_params='{"pool_name": "vms", "trash_id": "1234567890ab"}',
        )
        session.add(action)
        session.commit()
        action_id = action.id

    response = dashboard_client.get("/volumes?pool=vms")

    assert response.status_code == 200
    assert "Chờ duyệt" in response.text
    assert f'action="/actions/{action_id}/approve"' in response.text
    assert 'action="/volumes/vms/trash/1234567890ab/propose"' not in response.text


def test_unauthenticated_propose_trash_remove_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/volumes/vms/trash/1234567890ab/propose", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_propose_trash_remove_creates_pending_approval_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/volumes/vms/trash/1234567890ab/propose", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/volumes?pool=vms"
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(action_id="rbd_trash_remove").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.classification == "RISKY"
        assert action.proposed_command == "rbd trash rm vms/1234567890ab"
        incident = session.get(Incident, action.incident_id)
        assert incident.ceph_code == "RBD_TRASH_REMOVE"
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value


def test_propose_trash_remove_rejects_pool_not_in_configured_list(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/unknown-pool/trash/1234567890ab/propose")

    assert response.status_code == 404


def test_propose_trash_remove_rejects_duplicate_in_flight_proposal(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    first = dashboard_client.post("/volumes/vms/trash/1234567890ab/propose")
    assert first.status_code == 200  # followed the redirect

    second = dashboard_client.post(
        "/volumes/vms/trash/1234567890ab/propose", follow_redirects=False
    )
    assert second.status_code == 409


def test_propose_trash_remove_allows_different_trash_id_after_existing_proposal(
    dashboard_client, monkeypatch
):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    dashboard_client.post("/volumes/vms/trash/1234567890ab/propose")

    response = dashboard_client.post(
        "/volumes/vms/trash/other-id/propose", follow_redirects=False
    )
    assert response.status_code == 303  # a different trash_id is not a duplicate
