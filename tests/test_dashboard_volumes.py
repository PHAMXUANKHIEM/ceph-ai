import json
from datetime import datetime, timedelta

import bcrypt

import dashboard.routes.volumes as volumes_route
from config.settings import settings
from shared import db as db_module
from shared.models import Action, ActionStatus, Incident, IncidentStatus, User, VolumeMetric
from watcher.ceph_client import CephQueryError


def _login(client):
    # dashboard_client fixture (conftest.py) pins these credentials.
    client.post("/login", data={"username": "admin", "password": "admin"})


def _create_user(username, password, *, is_admin=False):
    with db_module.SessionLocal() as session:
        session.add(
            User(
                username=username,
                password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                is_admin=is_admin,
                is_active=True,
                created_by="admin",
            )
        )
        session.commit()


def _login_as(client, username, password):
    client.post("/login", data={"username": username, "password": password})


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


def test_volumes_page_shows_hint_when_no_pools_configured_and_none_discovered(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_rbd_pools", "")
    # CEPH_RBD_POOLS blank now means "auto-discover" (watcher/ceph_client.py
    # ::configured_rbd_pools), not "disabled" — mock discovery itself
    # finding nothing rather than letting this test attempt a real SSH call.
    monkeypatch.setattr(volumes_route.ceph_client, "discover_rbd_pools", lambda: [])
    _login(dashboard_client)

    response = dashboard_client.get("/volumes")

    assert response.status_code == 200
    assert "CEPH_RBD_POOLS" in response.text


def test_volumes_page_lists_auto_discovered_pools_when_none_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_rbd_pools", "")
    monkeypatch.setattr(volumes_route.ceph_client, "discover_rbd_pools", lambda: ["backups", "vms"])
    _login(dashboard_client)

    response = dashboard_client.get("/volumes")

    assert response.status_code == 200
    assert "vms" in response.text
    assert "backups" in response.text


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


# --- Volume search + history chart (2026-07-29) -------------------------


def _add_metric(pool, image, *, iops, read_ms, write_ms, polled_at, saturated=False):
    with db_module.SessionLocal() as session:
        session.add(
            VolumeMetric(
                pool=pool,
                image=image,
                iops=iops,
                read_latency_ms=read_ms,
                write_latency_ms=write_ms,
                saturated=saturated,
                polled_at=polled_at,
            )
        )
        session.commit()


def test_known_images_api_rejects_pool_not_in_configured_list(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/volumes/unknown-pool/images")

    assert response.status_code == 404


def test_known_images_api_combines_history_and_live_iostat(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    _add_metric("vms", "disk-idle", iops=0, read_ms=0, write_ms=0, polled_at=datetime.utcnow())
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "query_rbd_iostat",
        lambda pool: [{"image": "disk-live", "iops": 5, "read_latency_ms": 1, "write_latency_ms": 1}],
    )

    response = dashboard_client.get("/api/volumes/vms/images")

    assert response.status_code == 200
    assert response.json()["images"] == ["disk-idle", "disk-live"]


def test_known_images_api_falls_back_to_history_when_live_query_fails(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    _add_metric("vms", "disk-idle", iops=0, read_ms=0, write_ms=0, polled_at=datetime.utcnow())

    def broken(pool):
        raise CephQueryError("all MON nodes unreachable")

    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_iostat", broken)

    response = dashboard_client.get("/api/volumes/vms/images")

    assert response.status_code == 200
    assert response.json()["images"] == ["disk-idle"]


def test_history_api_rejects_pool_not_in_configured_list(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/volumes/unknown-pool/disk-1/history")

    assert response.status_code == 404


def test_history_api_returns_samples_within_window_ordered_by_time(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    now = datetime.utcnow()
    _add_metric("vms", "disk-1", iops=100, read_ms=1, write_ms=1, polled_at=now - timedelta(hours=1))
    _add_metric("vms", "disk-1", iops=200, read_ms=2, write_ms=2, polled_at=now - timedelta(minutes=30))
    # Outside the default 6h window entirely — must not appear in samples.
    _add_metric("vms", "disk-1", iops=50, read_ms=0.5, write_ms=0.5, polled_at=now - timedelta(days=2))

    response = dashboard_client.get("/api/volumes/vms/disk-1/history")

    assert response.status_code == 200
    body = response.json()
    assert [s["iops"] for s in body["samples"]] == [100, 200]


def test_history_api_computes_peak_over_full_history_not_just_window(dashboard_client, monkeypatch):
    # The whole point of this endpoint per the operator's own request: the
    # all-time best a volume has done must still show up even if it fell
    # out of the plotted (bounded) time window long ago.
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    now = datetime.utcnow()
    _add_metric("vms", "disk-1", iops=900, read_ms=9, write_ms=9, polled_at=now - timedelta(days=10))
    _add_metric("vms", "disk-1", iops=100, read_ms=1, write_ms=1, polled_at=now - timedelta(minutes=5))

    response = dashboard_client.get("/api/volumes/vms/disk-1/history")

    assert response.status_code == 200
    body = response.json()
    assert body["peak"]["iops"]["value"] == 900
    assert [s["iops"] for s in body["samples"]] == [100]  # only the in-window sample plotted


def test_history_api_clamps_hours_to_max(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/volumes/vms/disk-1/history?hours=999999")

    assert response.status_code == 200
    assert response.json()["hours"] == volumes_route._MAX_HISTORY_HOURS


def test_history_api_marks_saturated_when_incident_open(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    _add_metric("vms", "disk-1", iops=100, read_ms=1, write_ms=1, polled_at=datetime.utcnow())
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                ceph_code="VOLUME_SATURATED:vms/disk-1",
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
            )
        )
        session.commit()

    response = dashboard_client.get("/api/volumes/vms/disk-1/history")

    assert response.status_code == 200
    assert response.json()["saturated"] is True


def test_history_api_returns_none_peak_when_volume_never_seen(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/volumes/vms/never-seen/history")

    assert response.status_code == 200
    body = response.json()
    assert body["samples"] == []
    assert body["peak"] == {"iops": None, "read_latency_ms": None, "write_latency_ms": None}


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


def test_propose_trash_remove_sets_target_nodes_to_a_single_mon_node(dashboard_client, monkeypatch):
    # 2026-07-28 regression test: this used to be target_nodes=[], which
    # worker/llm/router_client.py::_execute_approved_action treats as
    # missing/malformed and marks FAILED without ever attempting the
    # command — approving this Action always silently failed.
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    dashboard_client.post("/volumes/vms/trash/1234567890ab/propose")

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(action_id="rbd_trash_remove").one()
        target_nodes = json.loads(action.target_nodes)
        assert target_nodes == ["10.20.1.150"]  # TEST_CEPH_MON_NODES' first entry


# --- POST /volumes/{pool}/trash/purge-all ("Xoá tất cả trash", 2026-07-28) -


def test_unauthenticated_purge_all_trash_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/volumes/vms/trash/purge-all", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_purge_all_trash_rejects_non_admin(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post("/volumes/vms/trash/purge-all")

    assert response.status_code == 403


def test_purge_all_trash_rejects_pool_not_in_configured_list(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/unknown-pool/trash/purge-all")

    assert response.status_code == 404


def test_purge_all_trash_success_records_executed_action_and_audit_entry(
    dashboard_client, monkeypatch
):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "force_purge_rbd_trash",
        lambda pool: [
            {"id": "id-1", "name": "disk-1", "error": None},
            {"id": "id-2", "name": "disk-2", "error": None},
        ],
    )
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/vms/trash/purge-all")

    assert response.status_code == 200
    assert "Đã xoá 2/2 volume" in response.text

    with db_module.SessionLocal() as session:
        action = (
            session.query(Action)
            .filter_by(action_id="rbd_trash_remove", status=ActionStatus.EXECUTED.value)
            .one()
        )
        assert action.classification == "RISKY"
        params = json.loads(action.action_params)
        assert params["bulk"] is True
        assert params["force"] is True
        assert set(params["trash_ids"]) == {"id-1", "id-2"}

        incident = session.get(Incident, action.incident_id)
        assert incident.ceph_code == "RBD_TRASH_PURGE_ALL"
        assert incident.status == IncidentStatus.RESOLVED.value


def test_purge_all_trash_reports_partial_failure(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "force_purge_rbd_trash",
        lambda pool: [
            {"id": "id-1", "name": "disk-1", "error": None},
            {"id": "id-2", "name": "disk-2", "error": "still in use"},
        ],
    )
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/vms/trash/purge-all")

    assert response.status_code == 200
    assert "Đã xoá 1/2 volume" in response.text
    assert "still in use" in response.text


def test_purge_all_trash_empty_trash_reports_nothing_to_delete(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(volumes_route.ceph_client, "force_purge_rbd_trash", lambda pool: [])
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/vms/trash/purge-all")

    assert response.status_code == 200
    assert "đang trống" in response.text
    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id="rbd_trash_remove").count() == 0


def test_purge_all_trash_shows_error_when_listing_fails(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)

    def broken(pool):
        raise CephQueryError("all MON nodes unreachable")

    monkeypatch.setattr(volumes_route.ceph_client, "force_purge_rbd_trash", broken)
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/vms/trash/purge-all")

    assert response.status_code == 200
    assert "Không lấy được danh sách trash" in response.text
    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id="rbd_trash_remove").count() == 0
