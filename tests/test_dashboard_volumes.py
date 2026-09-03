import json
from datetime import datetime, timedelta

import bcrypt

import dashboard.routes.volumes as volumes_route
from config.settings import settings
from shared import db as db_module
from shared.models import (
    Action,
    ActionStatus,
    BackupJob,
    Incident,
    IncidentStatus,
    Cluster,
    User,
    VolumeMetric,
    VolumePerfSweep,
)
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
    monkeypatch.setattr(volumes_route, "_rbd_pools_for_request", lambda request: ["vms", "backups"])
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_trash",
        lambda pool: [
            {"id": trash_id, "name": name, "deletion_time": "2020-01-01 00:00:00",
             "status": "expired", "size_bytes": 1, "used_size_bytes": 1}
            for trash_id, name in (("1234567890ab", "old-disk"), ("other-id", "other-disk"))
        ],
    )


def _configure_openstack_controller():
    with db_module.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).one()
        cluster.openstack_controller_nodes = "10.0.0.10"
        cluster.openstack_openrc_path = "/root/admin-openrc"
        session.commit()


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


def test_trash_is_top_level_page_not_pool_sidebar_item(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/trash")

    assert response.status_code == 200
    assert "<h2>Trash theo Pool</h2>" in response.text
    assert 'id="pool-selector"' not in response.text
    assert 'href="/volumes?view=trash"' not in response.text


def test_legacy_volumes_trash_url_redirects_to_top_level_trash(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volumes?view=trash", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/trash"


def test_trash_landing_shows_each_pool_count_and_total_size(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)

    def fake_query(pool):
        if pool == "vms":
            return [_fake_trash_entry(), _fake_trash_entry("id-2", "old-disk-2")]
        return []

    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_trash", fake_query)
    _login(dashboard_client)

    response = dashboard_client.get("/trash")

    assert response.status_code == 200
    assert 'href="/trash?pool=vms"' in response.text
    assert 'href="/trash?pool=backups"' in response.text
    assert "512.0 MiB" in response.text
    assert "2.0 GiB" in response.text
    assert "old-disk" not in response.text


def test_trash_landing_shows_purge_all_for_each_non_empty_pool(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "query_rbd_trash",
        lambda pool: [_fake_trash_entry(name=f"{pool}-deleted")],
    )
    _login(dashboard_client)

    response = dashboard_client.get("/trash")

    assert response.status_code == 200
    assert response.text.count("Xoá vĩnh viễn tất cả (1)</button>") == 2
    assert 'action="/volumes/vms/trash/purge-all"' in response.text
    assert 'action="/volumes/backups/trash/purge-all"' in response.text


def test_trash_pool_page_only_lists_selected_pools_entries(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "query_rbd_trash",
        lambda pool: [_fake_trash_entry(name=f"{pool}-deleted")],
    )
    _login(dashboard_client)

    response = dashboard_client.get("/trash?pool=vms")

    assert response.status_code == 200
    assert "vms-deleted" in response.text
    assert "backups-deleted" not in response.text


def test_volumes_page_with_no_pool_selects_nothing_and_shows_empty_state(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volumes")

    assert response.status_code == 200
    assert 'id="volumes-panel"' not in response.text
    assert "Chọn một pool để xem Volume" in response.text


def test_volumes_page_with_explicit_pool_selects_it(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volumes?pool=vms")

    assert response.status_code == 200
    assert 'id="volume-inventory-panel"' in response.text
    assert 'id="volumes-panel"' not in response.text
    assert 'class="card volume-inventory-card"' in response.text
    assert 'class="trash-pagination volume-inventory-pagination"' in response.text


def test_volume_performance_page_is_separate_from_volume_inventory(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volume-performance?pool=vms")

    assert response.status_code == 200
    assert 'id="volumes-panel"' in response.text
    assert 'id="vm-perf-panel"' in response.text
    assert 'id="volume-inventory-panel"' not in response.text
    assert 'data-pool="vms"' in response.text


def test_volumes_page_uses_vm_benchmark_instead_of_legacy_pool_sweep(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volume-performance?pool=vms")

    assert response.status_code == 200
    assert 'id="vm-perf-form"' in response.text
    assert 'id="perf-sweep-run-btn"' not in response.text


def test_volumes_page_hides_perf_sweep_button_for_non_admin(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_no_trash(monkeypatch)
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.get("/volumes?pool=vms")

    assert response.status_code == 200
    assert 'id="perf-sweep-run-btn"' not in response.text


def test_volumes_page_does_not_surface_legacy_perf_sweep_pending_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    propose = dashboard_client.post("/volumes/vms/perf-sweep/propose")
    action_id = propose.json()["action_id"]

    response = dashboard_client.get("/volume-performance?pool=vms")

    assert response.status_code == 200
    assert f'action="/actions/{action_id}/approve"' not in response.text
    assert f'action="/actions/{action_id}/reject"' not in response.text
    assert 'id="perf-sweep-run-btn"' not in response.text
    assert 'id="vm-perf-form"' in response.text


def test_volumes_page_does_not_show_legacy_perf_sweep_running_indicator(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    propose = dashboard_client.post("/volumes/vms/perf-sweep/propose")
    action_id = propose.json()["action_id"]
    with db_module.SessionLocal() as session:
        session.get(Action, action_id).status = ActionStatus.APPROVED.value
        session.commit()

    response = dashboard_client.get("/volume-performance?pool=vms")

    assert response.status_code == 200
    assert "Đang đo hiệu năng — xem tiến độ bên dưới" not in response.text
    assert 'id="vm-perf-form"' in response.text


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


def test_pool_discovery_uses_selected_non_default_cluster_credentials(monkeypatch):
    cluster = type("ClusterConfig", (), {
        "id": "cluster-b", "is_default": False,
        "ceph_mon_nodes": "10.0.0.21,10.0.0.22", "ssh_user": "ceph-b",
        "ssh_key_path": "/keys/b", "ceph_exec_mode": "cephadm",
        "ceph_container_name": "mon-b",
    })()
    request = type("Request", (), {
        "query_params": {}, "session": {"selected_cluster_id": "cluster-b"},
    })()
    monkeypatch.setattr(volumes_route, "_resolve_selected_cluster", lambda *_: ([cluster], cluster))
    calls = []

    def fake_query(*args):
        calls.append(args)
        return "10.0.0.21", [
            {"pool_name": "rbd-b", "application_metadata": {"rbd": {}}},
            {"pool_name": "cephfs-b", "application_metadata": {"cephfs": {}}},
        ]

    monkeypatch.setattr(volumes_route, "run_ceph_json_command_with", fake_query)

    assert volumes_route._rbd_pools_for_request(request) == ["rbd-b"]
    assert calls[0] == (
        ["10.0.0.21", "10.0.0.22"], "mon-b", "ceph-b", "/keys/b",
        "cephadm", "ceph osd pool ls detail",
    )


def test_default_cluster_live_pool_list_overrides_stale_config(monkeypatch):
    cluster = type("ClusterConfig", (), {
        "id": "cluster-a", "is_default": True,
        "ceph_mon_nodes": "10.0.0.11", "ssh_user": "ceph-a",
        "ssh_key_path": "/keys/a", "ceph_exec_mode": "none",
        "ceph_container_name": "",
    })()
    request = type("Request", (), {"query_params": {}, "session": {}})()
    monkeypatch.setattr(volumes_route, "_resolve_selected_cluster", lambda *_: ([cluster], cluster))
    monkeypatch.setattr(settings, "ceph_rbd_pools", "stale-pool")
    monkeypatch.setattr(volumes_route, "resolve_ssh_creds", lambda c: ("ceph-a", "/keys/a", "none", ""))
    monkeypatch.setattr(
        volumes_route,
        "run_ceph_json_command_with",
        lambda *args: ("10.0.0.11", [
            {"pool_name": "live-rbd", "application_metadata": {"rbd": {}}},
            {"pool_name": "not-rbd", "application_metadata": {}},
        ]),
    )

    assert volumes_route._rbd_pools_for_request(request) == ["live-rbd"]


def test_volumes_page_shows_cluster_switcher_for_multiple_clusters(dashboard_client, monkeypatch):
    default = type("ClusterConfig", (), {"id": "cluster-a", "name": "cluster-a", "is_default": True})()
    selected = type("ClusterConfig", (), {"id": "cluster-b", "name": "cluster-b", "is_default": False})()
    monkeypatch.setattr(volumes_route, "cluster_selection", lambda request: ([default, selected], selected))
    monkeypatch.setattr(volumes_route, "_rbd_pools_for_request", lambda request: ["rbd-b"])
    monkeypatch.setattr(volumes_route, "_cluster_for_request", lambda request: selected)
    monkeypatch.setattr(volumes_route, "cluster_connection", lambda cluster: ([], "", "", "", "none"))
    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_trash_with", lambda pool, *args: [])
    _login(dashboard_client)

    response = dashboard_client.get("/volumes?pool=rbd-b")

    assert response.status_code == 200
    assert 'aria-label="Chọn cluster"' in response.text
    assert '<option value="cluster-b" selected>cluster-b</option>' in response.text


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


# --- "Đo hiệu năng tối đa" load sweep (2026-07-29) -----------------------


def test_unauthenticated_propose_perf_sweep_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/volumes/vms/perf-sweep/propose", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_propose_perf_sweep_rejects_non_admin(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post("/volumes/vms/perf-sweep/propose")

    assert response.status_code == 403


def test_vm_perf_form_prompts_for_ip_key_and_suggested_disks(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/volume-performance?pool=vms")

    assert response.status_code == 200
    assert 'name="vm_ip"' in response.text
    assert 'name="ssh_key_path"' in response.text
    assert 'value="/dev/vdb"' in response.text
    assert 'value="/dev/vdc"' in response.text
    assert "READ-ONLY" in response.text
    assert "Mỗi mức tải được đo đúng 3 lần" in response.text
    assert 'id="perf-sweep-panel"' not in response.text


def test_propose_vm_perf_creates_risky_pending_action(dashboard_client):
    _configure_openstack_controller()
    _login(dashboard_client)

    response = dashboard_client.post(
        "/volumes/vm-perf/propose",
        json={
            "vm_ip": "10.20.1.50",
            "ssh_user": "ubuntu",
            "ssh_key_path": "/root/.ssh/vm-key",
            "device": "/dev/vdb",
        },
    )

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        assert action.action_id == "vm_perf_benchmark"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.classification == "RISKY"
        assert json.loads(action.target_nodes) == ["10.0.0.10"]
        params = json.loads(action.action_params)
        assert params["controller_ip"] == "10.0.0.10"
        assert params["ssh_user"] == "ubuntu"
        assert params["ssh_key_path"] == "/root/.ssh/vm-key"
        assert params["device"] == "/dev/vdb"
        assert "/root/.ssh/vm-key" not in action.proposed_command


def test_propose_vm_perf_rejects_bad_ip_device_or_missing_key(dashboard_client, tmp_path):
    _configure_openstack_controller()
    _login(dashboard_client)
    base = {
        "vm_ip": "not-an-ip",
        "ssh_user": "root",
        "ssh_key_path": str(tmp_path / "missing"),
        "device": "/dev/vdb;reboot",
    }
    assert dashboard_client.post("/volumes/vm-perf/propose", json=base).status_code == 400

    base["vm_ip"] = "10.20.1.50"
    assert dashboard_client.post("/volumes/vm-perf/propose", json=base).status_code == 400

    base["device"] = "/dev/vdb"
    base["ssh_key_path"] = "relative/missing-key"
    response = dashboard_client.post("/volumes/vm-perf/propose", json=base)
    assert response.status_code == 400
    assert "SSH key" in response.json()["detail"]


def test_propose_vm_perf_requires_configured_openstack_controller(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/volumes/vm-perf/propose",
        json={
            "vm_ip": "10.20.1.50",
            "ssh_user": "ubuntu",
            "ssh_key_path": "/root/.ssh/vm-key",
            "device": "/dev/vdb",
        },
    )
    assert response.status_code == 400
    assert "OpenStack Controller" in response.json()["detail"]


def test_propose_perf_sweep_rejects_pool_not_in_configured_list(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/unknown-pool/perf-sweep/propose")

    assert response.status_code == 404


def test_propose_perf_sweep_creates_pending_approval_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/vms/perf-sweep/propose")

    assert response.status_code == 201
    action_pk = response.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.action_id == "volume_perf_sweep"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.classification == "RISKY"
        # TEST_CEPH_MON_NODES/TEST_CEPH_OSD_NODES (conftest.py's autouse fixture).
        assert json.loads(action.target_nodes) == ["10.20.1.150"]
        params = json.loads(action.action_params)
        assert params["pool"] == "vms"
        assert params["mon_ip"] == "10.20.1.150"
        assert params["osd_ips"] == ["10.20.1.83", "10.20.1.78", "10.20.1.1"]
        assert params["requested_by"] == "admin"
        # 2026-07-29 regression: without a proposed_command, has_command()
        # returns False for this action_id and approving it silently
        # closes it out as EXECUTED without ever running anything (see
        # tests/test_dashboard_actions.py's own regression test for the
        # approve-side half of this).
        assert action.proposed_command
        assert "vms" in action.proposed_command
        assert "fio" in action.proposed_command

        incident = session.get(Incident, action.incident_id)
        assert incident.ceph_code == "VOLUME_PERF_SWEEP"
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value


def test_propose_perf_sweep_rejects_when_no_mon_nodes_configured(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/vms/perf-sweep/propose")

    assert response.status_code == 400


def test_propose_perf_sweep_rejects_second_proposal_while_one_pending(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    first = dashboard_client.post("/volumes/vms/perf-sweep/propose")
    assert first.status_code == 201

    second = dashboard_client.post("/volumes/vms/perf-sweep/propose")
    assert second.status_code == 409


def test_propose_perf_sweep_allows_new_proposal_for_a_different_pool(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    dashboard_client.post("/volumes/vms/perf-sweep/propose")

    response = dashboard_client.post("/volumes/backups/perf-sweep/propose")
    assert response.status_code == 201


def test_perf_sweep_progress_api_returns_null_status_with_no_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/volumes/vms/perf-sweep/progress")

    assert response.status_code == 200
    assert response.json() == {"status": None, "progress": []}


def test_perf_sweep_progress_api_formats_timestamps_as_vietnam_local_clock(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    propose = dashboard_client.post("/volumes/vms/perf-sweep/propose")
    action_pk = propose.json()["action_id"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        action.status = ActionStatus.APPROVED.value
        action.execution_progress = json.dumps(
            [
                {
                    "step": "prepare",
                    "status": "done",
                    "pct": 10,
                    "started_at": "2026-07-29T03:29:30",
                    "finished_at": "2026-07-29T03:29:45",
                },
                {"step": "sweep", "status": "running", "pct": 90, "hosts": [{"host": "iodepth=1", "status": "done"}]},
            ]
        )
        session.commit()

    response = dashboard_client.get("/api/volumes/vms/perf-sweep/progress").json()

    assert response["status"] == "APPROVED"
    done_step = response["progress"][0]
    assert done_step["started_at_display"] == "10:29:30"  # 03:29 UTC -> 10:29 ICT
    assert done_step["finished_at_display"] == "10:29:45"
    assert response["progress"][1]["hosts"][0]["status"] == "done"


def test_perf_sweep_latest_api_rejects_pool_not_in_configured_list(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/volumes/unknown-pool/perf-sweep/latest")

    assert response.status_code == 404


def test_perf_sweep_latest_api_returns_none_when_no_sweep_yet(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/api/volumes/vms/perf-sweep/latest")

    assert response.status_code == 200
    assert response.json() == {"pool": "vms", "sweep": None}


def test_perf_sweep_latest_api_returns_most_recent_done_sweep_with_knee(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        session.add(
            VolumePerfSweep(
                id="sweep-1",
                action_id="action-1",
                pool="vms",
                scratch_image="_ceph_aiops_perf_probe",
                requested_by="admin",
                status="DONE",
                steps_json=json.dumps([{"iodepth": 16, "iops": 16000, "latency_avg_ms": 1.0, "latency_p99_ms": 1.8}]),
                knee_iodepth=16,
                knee_iops=16000.0,
                knee_latency_avg_ms=1.0,
                knee_latency_p99_ms=1.8,
                qos_notes="Không có giới hạn QoS nào được đặt trên scratch image.",
                bottleneck_notes="ceph osd perf:\nosd.0 1 2",
                created_at=datetime.utcnow(),
                finished_at=datetime.utcnow(),
            )
        )
        session.commit()

    response = dashboard_client.get("/api/volumes/vms/perf-sweep/latest")

    assert response.status_code == 200
    body = response.json()["sweep"]
    assert body["status"] == "DONE"
    assert body["knee"] == {"iodepth": 16, "iops": 16000.0, "latency_avg_ms": 1.0, "latency_p99_ms": 1.8}
    assert body["qos_notes"].startswith("Không có giới hạn QoS")
    assert len(body["steps"]) == 1
    assert body["ai_conclusion"] is None
    assert body["ai_analyzed_at"] is None


def test_perf_sweep_latest_api_includes_ai_conclusion_when_present(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    conclusion = {
        "max_iops": 16000,
        "max_iops_basis": "saturation_knee",
        "confidence": "high",
        "conclusion_vi": "Hiệu năng tối đa khoảng 16000 IOPS.",
        "caveats_vi": "Nút thắt có thể ở OSD.",
    }
    with db_module.SessionLocal() as session:
        session.add(
            VolumePerfSweep(
                id="sweep-ai",
                action_id="action-1",
                pool="vms",
                scratch_image="_ceph_aiops_perf_probe",
                requested_by="admin",
                status="DONE",
                steps_json="[]",
                ai_conclusion=json.dumps(conclusion),
                ai_analyzed_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    response = dashboard_client.get("/api/volumes/vms/perf-sweep/latest")

    assert response.status_code == 200
    body = response.json()["sweep"]
    assert body["ai_conclusion"] == conclusion
    assert body["ai_analyzed_at"] is not None


def test_perf_sweep_latest_api_returns_failed_sweep_with_error_message(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        session.add(
            VolumePerfSweep(
                id="sweep-2",
                action_id="action-2",
                pool="vms",
                scratch_image="_ceph_aiops_perf_probe",
                requested_by="admin",
                status="FAILED",
                steps_json="[]",
                error_message="10.20.1.150: chưa cài fio",
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    response = dashboard_client.get("/api/volumes/vms/perf-sweep/latest")

    assert response.status_code == 200
    body = response.json()["sweep"]
    assert body["status"] == "FAILED"
    assert body["knee"] is None
    assert body["error_message"] == "10.20.1.150: chưa cài fio"


# --- "Phân tích bằng AI" (2026-07-29) -------------------------------------

_AI_CONCLUSION = {
    "max_iops": 16000,
    "max_iops_basis": "saturation_knee",
    "confidence": "high",
    "conclusion_vi": "Hiệu năng tối đa khoảng 16000 IOPS.",
    "caveats_vi": "Nút thắt có thể ở OSD.",
}


def test_analyze_perf_sweep_rejects_pool_not_in_configured_list(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/api/volumes/unknown-pool/perf-sweep/analyze")

    assert response.status_code == 404


def test_analyze_perf_sweep_rejects_when_no_done_sweep_exists(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post("/api/volumes/vms/perf-sweep/analyze")

    assert response.status_code == 400


def test_analyze_perf_sweep_rejects_when_latest_sweep_is_running(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        session.add(
            VolumePerfSweep(
                id="sweep-running",
                action_id="action-1",
                pool="vms",
                scratch_image="_ceph_aiops_perf_probe",
                requested_by="admin",
                status="RUNNING",
                steps_json="[]",
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    response = dashboard_client.post("/api/volumes/vms/perf-sweep/analyze")

    assert response.status_code == 400


def test_analyze_perf_sweep_persists_conclusion_and_returns_it(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        session.add(
            VolumePerfSweep(
                id="sweep-done",
                action_id="action-1",
                pool="vms",
                scratch_image="_ceph_aiops_perf_probe",
                requested_by="admin",
                status="DONE",
                steps_json=json.dumps([{"iodepth": 16, "iops": 16000, "latency_avg_ms": 1.0, "latency_p99_ms": 1.8}]),
                knee_iodepth=16,
                knee_iops=16000.0,
                knee_latency_avg_ms=1.0,
                knee_latency_p99_ms=1.8,
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    async def fake_analyze(sweep):
        assert sweep["pool"] == "vms"
        assert sweep["knee"]["iodepth"] == 16
        return _AI_CONCLUSION

    monkeypatch.setattr(volumes_route.volume_perf_analysis, "analyze_volume_perf_sweep", fake_analyze)

    response = dashboard_client.post("/api/volumes/vms/perf-sweep/analyze")

    assert response.status_code == 200
    assert response.json()["conclusion"] == _AI_CONCLUSION
    with db_module.SessionLocal() as session:
        row = session.get(VolumePerfSweep, "sweep-done")
        assert json.loads(row.ai_conclusion) == _AI_CONCLUSION
        assert row.ai_analyzed_at is not None


def test_analyze_perf_sweep_returns_502_when_analysis_fails(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    with db_module.SessionLocal() as session:
        session.add(
            VolumePerfSweep(
                id="sweep-done-2",
                action_id="action-1",
                pool="vms",
                scratch_image="_ceph_aiops_perf_probe",
                requested_by="admin",
                status="DONE",
                steps_json="[]",
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    async def broken_analyze(sweep):
        raise volumes_route.volume_perf_analysis.VolumePerfAnalysisError("Chưa cấu hình API AI")

    monkeypatch.setattr(volumes_route.volume_perf_analysis, "analyze_volume_perf_sweep", broken_analyze)

    response = dashboard_client.post("/api/volumes/vms/perf-sweep/analyze")

    assert response.status_code == 502
    with db_module.SessionLocal() as session:
        row = session.get(VolumePerfSweep, "sweep-done-2")
        assert row.ai_conclusion is None


# --- Trash (2026-07-28) -------------------------------------------------


def _fake_trash_entry(entry_id="1234567890ab", name="old-disk"):
    return {"id": entry_id, "name": name, "deletion_time": "2026-07-28 10:00:00", "status": "expired", "size_bytes": 1073741824, "used_size_bytes": 268435456}


def test_volumes_page_shows_trash_entries(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_trash", lambda pool: [_fake_trash_entry()]
    )
    _login(dashboard_client)

    response = dashboard_client.get("/trash?pool=vms")

    assert response.status_code == 200
    assert "old-disk" in response.text
    assert "1234567890ab" in response.text
    assert 'id="trash-id-filter"' in response.text
    assert 'id="trash-entry-list"' in response.text
    assert 'data-trash-id="1234567890ab"' in response.text
    assert 'id="trash-pagination"' in response.text
    assert "10 Trash mỗi trang" in response.text
    assert 'src="/static/trash.js' in response.text
    assert 'id="trash-purge-all-btn"' in response.text
    assert 'action="/volumes/vms/trash/purge-all"' in response.text
    assert "Xoá vĩnh viễn tất cả (1)" in response.text
    assert "XOÁ VĨNH VIỄN tất cả 1 volume đã hết TTL" in response.text


def test_trash_page_hides_purge_all_from_non_admin(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_trash", lambda pool: [_fake_trash_entry()]
    )
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.get("/trash?pool=vms")

    assert response.status_code == 200
    assert 'id="trash-purge-all-btn"' not in response.text
    assert 'action="/volumes/vms/trash/purge-all"' not in response.text


def test_trash_page_server_hides_entries_after_first_ten(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "query_rbd_trash",
        lambda pool: [_fake_trash_entry(f"id-{index}", f"disk-{index}") for index in range(12)],
    )
    _login(dashboard_client)

    response = dashboard_client.get("/trash?pool=vms")

    assert response.status_code == 200
    assert 'data-trash-id="id-9"' in response.text
    assert 'data-trash-id="id-10" hidden' in response.text
    assert "Trang 1 / 2" in response.text


def test_volumes_page_shows_empty_trash_hint(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_no_trash(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.get("/trash?pool=vms")

    assert response.status_code == 200
    assert "Trash của pool" in response.text
    assert "đang trống" in response.text


def test_volumes_page_shows_trash_error_without_crashing(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)

    def fake_query(pool):
        raise CephQueryError("all MON nodes failed")

    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_trash", fake_query)
    _login(dashboard_client)

    response = dashboard_client.get("/trash?pool=vms")

    assert response.status_code == 200
    assert "Một số pool không đọc được Trash" in response.text


def test_volumes_page_shows_xoa_button_when_no_pending_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_trash", lambda pool: [_fake_trash_entry()]
    )
    _login(dashboard_client)

    response = dashboard_client.get("/trash?pool=vms")

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

    response = dashboard_client.get("/trash?pool=vms")

    assert response.status_code == 200
    assert "Duyệt xoá" in response.text
    assert f'action="/actions/{action_id}/approve"' in response.text
    assert 'action="/volumes/vms/trash/1234567890ab/propose"' not in response.text


def test_unauthenticated_propose_trash_remove_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/volumes/vms/trash/1234567890ab/propose", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_propose_trash_remove_rejects_non_admin(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _create_user("trash-operator", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "trash-operator", "s3cret-pw")

    response = dashboard_client.post("/volumes/vms/trash/1234567890ab/propose")

    assert response.status_code == 403
    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id="rbd_trash_remove").count() == 0


def test_propose_trash_remove_creates_pending_approval_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/volumes/vms/trash/1234567890ab/propose", follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/trash?pool=vms"
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(action_id="rbd_trash_remove").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        # AI roadmap Pha 0.4 (2026-08-18): moved risky: -> destructive: —
        # permanently destroys data. Always required explicit approval
        # either way (unchanged above); stricter label only.
        assert action.classification == "DESTRUCTIVE"
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


def test_purge_all_trash_creates_pending_approval_action_without_direct_delete(
    dashboard_client, monkeypatch
):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "query_rbd_trash",
        lambda pool: [
            {"id": "id-1", "name": "disk-1", "deletion_time": "2020-01-01 00:00:00"},
            {"id": "id-2", "name": "disk-2", "deletion_time": "2020-01-01 00:00:00"},
        ],
    )
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/vms/trash/purge-all", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/trash?pool=vms"

    with db_module.SessionLocal() as session:
        action = (
            session.query(Action)
            .filter_by(action_id="rbd_trash_purge_all", status=ActionStatus.PENDING_APPROVAL.value)
            .one()
        )
        assert action.classification == "DESTRUCTIVE"
        params = json.loads(action.action_params)
        assert set(params["trash_ids"]) == {"id-1", "id-2"}
        assert json.loads(action.target_nodes) == ["10.20.1.150"]
        assert "--force" not in action.proposed_command
        assert action.proposed_command.splitlines() == ["rbd trash rm vms/id-1", "rbd trash rm vms/id-2"]

        incident = session.get(Incident, action.incident_id)
        assert incident.ceph_code == "RBD_TRASH_PURGE_ALL"
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        action_pk = action.id

    page = dashboard_client.get("/trash?pool=vms")
    assert page.status_code == 200
    assert "Đề xuất xoá tất cả đang chờ duyệt." in page.text
    assert f'action="/actions/{action_pk}/approve"' in page.text
    assert "Duyệt xoá tất cả" in page.text


def test_purge_all_trash_rejects_duplicate_in_flight_proposal(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "query_rbd_trash",
        lambda pool: [{"id": "id-1", "name": "disk-1", "deletion_time": "2020-01-01 00:00:00"}],
    )
    _login(dashboard_client)

    first = dashboard_client.post("/volumes/vms/trash/purge-all", follow_redirects=False)
    duplicate = dashboard_client.post("/volumes/vms/trash/purge-all", follow_redirects=False)

    assert first.status_code == 303
    assert duplicate.status_code == 409


def test_purge_all_trash_empty_trash_reports_nothing_to_delete(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_trash", lambda pool: [])
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/vms/trash/purge-all")

    assert response.status_code == 409
    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id="rbd_trash_purge_all").count() == 0


def test_trash_ttl_blocks_early_delete_and_purge_all(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(settings, "rbd_trash_retention_days", 30)
    fresh = {
        "id": "fresh-id", "name": "fresh-disk",
        "deletion_time": datetime.utcnow().isoformat(), "status": "normal",
        "size_bytes": 1, "used_size_bytes": 1,
    }
    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_trash", lambda pool: [fresh])
    _login(dashboard_client)

    page = dashboard_client.get("/trash?pool=vms")
    single = dashboard_client.post("/volumes/vms/trash/fresh-id/propose")
    bulk = dashboard_client.post("/volumes/vms/trash/purge-all")

    assert page.status_code == 200
    assert "Còn 30 ngày" in page.text
    assert "Chưa hết TTL" in page.text
    assert 'id="trash-purge-all-btn" disabled' in page.text
    assert 'title="Chưa có volume nào hết TTL"' in page.text
    assert 'action="/volumes/vms/trash/purge-all"' not in page.text
    assert 'action="/volumes/vms/trash/fresh-id/propose"' not in page.text
    assert single.status_code == 409
    assert bulk.status_code == 409


def test_purge_all_trash_shows_error_when_listing_fails(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)

    def broken(pool):
        raise CephQueryError("all MON nodes unreachable")

    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_trash", broken)
    _login(dashboard_client)

    response = dashboard_client.post("/volumes/vms/trash/purge-all")

    assert response.status_code == 502
    with db_module.SessionLocal() as session:
        assert session.query(Action).filter_by(action_id="rbd_trash_purge_all").count() == 0


def test_volume_inventory_api_searches_sorts_and_pages(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "query_rbd_inventory",
        lambda pool: [
            {"name": "web-02", "image_id": "2", "provisioned_size": 20, "used_size": 8, "snapshot_count": 0},
            {"name": "db-01", "image_id": "1", "provisioned_size": 50, "used_size": 40, "snapshot_count": 2},
            {"name": "web-01", "image_id": "3", "provisioned_size": 10, "used_size": 4, "snapshot_count": 1},
        ],
    )
    _login(dashboard_client)

    response = dashboard_client.get(
        "/api/volumes/vms/inventory?search=web&sort=provisioned_size&order=desc&page=1&page_size=1"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["pages"] == 2
    assert [item["name"] for item in body["items"]] == ["web-02"]
    assert body["cluster_id"]
    assert body["summary"] == {"image_count": 2, "provisioned_size": 30, "used_size": 12}


def test_volume_inventory_defaults_to_ten_rows_and_rejects_larger_pages(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_inventory", lambda pool: [
        {"name": f"volume-{index:02d}", "image_id": str(index), "provisioned_size": 10,
         "used_size": 1, "snapshot_count": 0}
        for index in range(12)
    ])
    _login(dashboard_client)

    first_page = dashboard_client.get("/api/volumes/vms/inventory")
    oversized = dashboard_client.get("/api/volumes/vms/inventory?page_size=11")

    assert first_page.status_code == 200
    assert first_page.json()["page_size"] == 10
    assert len(first_page.json()["items"]) == 10
    assert first_page.json()["pages"] == 2
    assert oversized.status_code == 422


def test_volume_inventory_api_uses_selected_secondary_cluster(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    with db_module.SessionLocal() as session:
        secondary = Cluster(
            name="secondary", ceph_mon_nodes="10.2.0.1", ceph_container_name="mon",
            ssh_user="ceph", ssh_key_path="/key", ceph_exec_mode="cephadm",
            is_default=False, is_active=True,
        )
        session.add(secondary)
        session.commit()
        cluster_id = secondary.id
    monkeypatch.setattr(volumes_route, "_rbd_pools_for_request", lambda request: ["vms"])
    calls = []
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_inventory_with",
        lambda *args: calls.append(args) or [],
    )
    _login(dashboard_client)

    response = dashboard_client.get(f"/api/volumes/vms/inventory?cluster={cluster_id}")

    assert response.status_code == 200
    assert response.json()["cluster_id"] == cluster_id
    assert calls[0][0] == "vms"
    assert calls[0][1] == ["10.2.0.1"]


def test_volume_inventory_detail_rejects_invalid_name_and_returns_dependencies(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(volumes_route, "discover_cinder_volume", lambda cluster, image: {"status": "not_cinder", "verified": False})
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_image_detail",
        lambda pool, image: {
            "pool": pool, "name": image, "image_id": "id-1", "size": 1024,
            "object_size": 4096, "object_count": 1, "format": 2,
            "features": ["layering"], "flags": [], "created_at": None,
            "parent": None, "snapshots": [{"name": "daily"}],
            "watchers": [{"client": "client.1"}], "children": ["vms/clone"],
            "locks": [{"locker_id": "client.1"}],
            "attachment_summary": {"attached": True, "watcher_count": 1,
                                   "lock_count": 1, "management_source": "unknown",
                                   "mutation_supported": False},
        },
    )
    _login(dashboard_client)

    invalid = dashboard_client.get("/api/volumes/vms/inventory/bad%2Fname")
    response = dashboard_client.get("/api/volumes/vms/inventory/vm-01")

    assert invalid.status_code in (400, 404)
    assert response.status_code == 200
    assert response.json()["snapshots"] == [{"name": "daily"}]
    assert response.json()["children"] == ["vms/clone"]
    assert response.json()["attachment_summary"]["mutation_supported"] is False


def test_volume_inventory_detail_marks_verified_cinder_consumer(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    volume_id = "12345678-1234-4123-8123-1234567890ab"
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_image_detail",
        lambda pool, image: {
            "pool": pool, "name": image, "watchers": [], "locks": [],
            "attachment_summary": {"attached": False, "watcher_count": 0,
                                   "lock_count": 0, "management_source": "unknown",
                                   "mutation_supported": False},
        },
    )
    monkeypatch.setattr(
        volumes_route, "discover_cinder_volume",
        lambda cluster, image: {
            "status": "managed", "verified": True, "volume_id": volume_id,
            "volume_status": "in-use", "multiattach": False,
            "attachments": [{"attachment_id": "attach-1", "instance_id": "vm-1"}],
        },
    )
    monkeypatch.setattr(
        volumes_route, "discover_cinder_snapshots",
        lambda cluster, volume_id: {
            "status": "ok", "count": 1,
            "items": [{"snapshot_id": "snap-1", "name": "daily", "status": "available"}],
        },
    )
    _login(dashboard_client)

    response = dashboard_client.get(f"/api/volumes/vms/inventory/volume-{volume_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cinder"]["volume_id"] == volume_id
    assert payload["attachment_summary"]["management_source"] == "openstack_cinder"
    assert payload["attachment_summary"]["consumer_count"] == 1
    assert payload["attachment_summary"]["mutation_supported"] is False
    assert payload["attachment_reconciliation"]["status"] == "mismatch"
    assert payload["cinder_snapshots"]["items"][0]["snapshot_id"] == "snap-1"


def test_cinder_attach_proposal_is_approval_gated_and_targets_controller(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _configure_openstack_controller()
    volume_id = "12345678-1234-4123-8123-1234567890ab"
    server_id = "abcdefab-1234-4123-8123-1234567890ab"

    async def fake_preflight(cluster, pool, image):
        return {}, {
            "status": "managed", "verified": True, "volume_id": volume_id,
            "volume_status": "available", "attachments": [],
        }, {"status": "healthy", "safe": True}

    monkeypatch.setattr(volumes_route, "_cinder_attachment_preflight", fake_preflight)
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/api/volumes/vms/inventory/volume-{volume_id}/attach",
        headers={"Idempotency-Key": "attach-test-1234"}, json={"server_id": server_id},
    )

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(action_id="cinder_attach_volume").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert json.loads(action.target_nodes) == ["10.0.0.10"]
        params = json.loads(action.action_params)
        assert params["volume_id"] == volume_id
        assert params["server_id"] == server_id
        assert "openstack server add volume" in action.proposed_command


def test_cinder_detach_proposal_requires_matching_attachment(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _configure_openstack_controller()
    volume_id = "12345678-1234-4123-8123-1234567890ab"
    server_id = "abcdefab-1234-4123-8123-1234567890ab"

    async def fake_preflight(cluster, pool, image):
        return {}, {
            "status": "managed", "verified": True, "volume_id": volume_id,
            "volume_status": "in-use",
            "attachments": [{"attachment_id": "attach-1", "instance_id": server_id}],
        }, {"status": "healthy", "safe": True}

    monkeypatch.setattr(volumes_route, "_cinder_attachment_preflight", fake_preflight)
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/api/volumes/vms/inventory/volume-{volume_id}/detach",
        headers={"Idempotency-Key": "detach-test-123"}, json={"server_id": server_id},
    )

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(action_id="cinder_detach_volume").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert "openstack server remove volume" in action.proposed_command


def test_cinder_multiattach_requires_capability_and_new_server(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _configure_openstack_controller()
    volume_id = "12345678-1234-4123-8123-1234567890ab"
    existing_server = "abcdefab-1234-4123-8123-1234567890ab"
    new_server = "fedcbafe-1234-4123-8123-1234567890ab"

    async def fake_preflight(cluster, pool, image):
        return {}, {
            "status": "managed", "verified": True, "volume_id": volume_id,
            "volume_status": "in-use", "multiattach": True,
            "attachments": [{"attachment_id": "attach-1", "instance_id": existing_server}],
        }, {"status": "healthy", "safe": True}

    monkeypatch.setattr(volumes_route, "_cinder_attachment_preflight", fake_preflight)
    _login(dashboard_client)
    duplicate = dashboard_client.post(
        f"/api/volumes/vms/inventory/volume-{volume_id}/attach",
        json={"server_id": existing_server},
    )
    response = dashboard_client.post(
        f"/api/volumes/vms/inventory/volume-{volume_id}/attach",
        json={"server_id": new_server},
    )

    assert duplicate.status_code == 409
    assert "đã attach" in duplicate.json()["detail"]
    assert response.status_code == 201


def test_cinder_attach_rejects_in_use_exclusive_volume(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _configure_openstack_controller()
    volume_id = "12345678-1234-4123-8123-1234567890ab"

    async def fake_preflight(cluster, pool, image):
        return {}, {
            "status": "managed", "verified": True, "volume_id": volume_id,
            "volume_status": "in-use", "multiattach": False,
            "attachments": [{"instance_id": "abcdefab-1234-4123-8123-1234567890ab"}],
        }, {"status": "healthy", "safe": True}

    monkeypatch.setattr(volumes_route, "_cinder_attachment_preflight", fake_preflight)
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/api/volumes/vms/inventory/volume-{volume_id}/attach",
        json={"server_id": "fedcbafe-1234-4123-8123-1234567890ab"},
    )

    assert response.status_code == 409
    assert "multiattach=true" in response.json()["detail"]


def test_cinder_snapshot_create_is_approval_gated_and_forces_attached_volume(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _configure_openstack_controller()
    volume_id = "12345678-1234-4123-8123-1234567890ab"

    async def fake_preflight(cluster, pool, image):
        return {}, {
            "status": "managed", "verified": True, "volume_id": volume_id,
            "volume_status": "in-use", "attachments": [{"instance_id": "vm-1"}],
        }, {"status": "healthy", "safe": True}

    monkeypatch.setattr(volumes_route, "_cinder_attachment_preflight", fake_preflight)
    monkeypatch.setattr(
        volumes_route, "discover_cinder_snapshots",
        lambda cluster, cinder_volume_id: {"status": "ok", "items": [], "count": 0},
    )
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/api/volumes/vms/inventory/volume-{volume_id}/snapshots",
        headers={"Idempotency-Key": "snapshot-test-1"},
        json={"snapshot_name": "daily-01"},
    )

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(action_id="cinder_create_snapshot").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        params = json.loads(action.action_params)
        assert params["snapshot_name"] == "daily-01"
        assert params["force"] is True
        assert "snapshot create" in action.proposed_command
        assert "--force" in action.proposed_command


def test_volume_inventory_api_is_read_only_for_non_admin_and_surfaces_backend_error(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _create_user("viewer", "viewer-password", is_admin=False)
    _login_as(dashboard_client, "viewer", "viewer-password")
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_inventory",
        lambda pool: (_ for _ in ()).throw(CephQueryError("MON timeout")),
    )

    response = dashboard_client.get("/api/volumes/vms/inventory")

    assert response.status_code == 502
    assert "MON timeout" in response.json()["detail"]


def test_volume_pool_overview_api_returns_durability_and_capacity(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_pool_overview",
        lambda pool: {
            "pool": pool, "pool_id": 3, "type": "replicated", "replica_size": 3,
            "min_size": 2, "pg_num": 32, "pgp_num": 32, "crush_rule": 0,
            "erasure_code_profile": None, "rbd_enabled": True, "bytes_used": 2048,
            "max_available": 8192, "percent_used": 20.0, "objects": 5,
            "health": "warning", "near_full": True,
            "health_checks": [{"code": "OSD_NEARFULL", "severity": "HEALTH_WARN", "summary": "1 osd nearfull"}],
        },
    )
    _login(dashboard_client)

    response = dashboard_client.get("/api/volumes/vms/inventory-overview")

    assert response.status_code == 200
    assert response.json()["replica_size"] == 3
    assert response.json()["rbd_enabled"] is True
    assert response.json()["bytes_used"] == 2048
    assert response.json()["near_full"] is True


def test_volume_inventory_rejects_inactive_cluster_without_default_fallback(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    with db_module.SessionLocal() as session:
        inactive = Cluster(
            name="inactive", ceph_mon_nodes="10.9.0.1", ceph_container_name="",
            ssh_user="root", ssh_key_path="/key", ceph_exec_mode="none",
            is_default=False, is_active=False,
        )
        session.add(inactive)
        session.commit()
        cluster_id = inactive.id
    default_calls = []
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_inventory",
        lambda pool: default_calls.append(pool) or [],
    )
    _login(dashboard_client)

    response = dashboard_client.get(f"/api/volumes/vms/inventory?cluster={cluster_id}")

    assert response.status_code == 409
    assert "không fallback" in response.json()["detail"]
    assert default_calls == []


def _stub_volume_mutation_preflight(monkeypatch, *, current_size=10 * 1024 ** 3, max_available=100 * 1024 ** 3):
    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_inventory", lambda pool: [])
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_image_detail",
        lambda pool, image: {"pool": pool, "name": image, "size": current_size},
    )
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_pool_overview",
        lambda pool: {"pool": pool, "max_available": max_available},
    )


def test_propose_create_volume_creates_risky_cluster_scoped_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/api/volumes/vms/inventory/create", json={"image": "vm-new", "size_gib": 20}
    )

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        assert action.action_id == "rbd_create_volume"
        assert action.classification == "RISKY"
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert json.loads(action.action_params) == {"pool_name": "vms", "image": "vm-new", "size_mib": 20480}
        incident = session.get(Incident, action.incident_id)
        assert incident.cluster_id is not None
        assert incident.ceph_code == "RBD_VOLUME_CREATE"


def test_propose_create_volume_rejects_existing_or_over_capacity(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch, max_available=5 * 1024 ** 3)
    _login(dashboard_client)

    too_large = dashboard_client.post(
        "/api/volumes/vms/inventory/create", json={"image": "vm-new", "size_gib": 10}
    )
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_inventory",
        lambda pool: [{"name": "exists", "image_id": "1", "provisioned_size": 1, "used_size": 1, "snapshot_count": 0}],
    )
    exists = dashboard_client.post(
        "/api/volumes/vms/inventory/create", json={"image": "exists", "size_gib": 1}
    )

    assert too_large.status_code == 409
    assert exists.status_code == 409


def test_propose_create_volume_rejects_duplicate_in_flight_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch)
    _login(dashboard_client)

    first = dashboard_client.post(
        "/api/volumes/vms/inventory/create", json={"image": "vm-new", "size_gib": 10}
    )
    duplicate = dashboard_client.post(
        "/api/volumes/vms/inventory/create", json={"image": "vm-new", "size_gib": 10}
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    with db_module.SessionLocal() as session:
        actions = session.query(Action).filter(Action.action_id == "rbd_create_volume").all()
        assert len(actions) == 1


def test_create_volume_idempotency_key_replays_same_action_before_preflight(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch)
    _login(dashboard_client)
    headers = {"Idempotency-Key": "create-vm-new-001"}

    first = dashboard_client.post(
        "/api/volumes/vms/inventory/create",
        json={"image": "vm-new", "size_gib": 10}, headers=headers,
    )
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_inventory",
        lambda pool: (_ for _ in ()).throw(AssertionError("replay must skip Ceph preflight")),
    )
    replay = dashboard_client.post(
        "/api/volumes/vms/inventory/create",
        json={"image": "vm-new", "size_gib": 10}, headers=headers,
    )

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == {
        "action_id": first.json()["action_id"], "status": "PENDING_APPROVAL", "replayed": True,
    }


def test_idempotency_key_rejects_different_intent_and_invalid_key(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch)
    _login(dashboard_client)
    headers = {"Idempotency-Key": "create-vm-new-002"}
    first = dashboard_client.post(
        "/api/volumes/vms/inventory/create",
        json={"image": "vm-new", "size_gib": 10}, headers=headers,
    )

    conflict = dashboard_client.post(
        "/api/volumes/vms/inventory/create",
        json={"image": "vm-new", "size_gib": 11}, headers=headers,
    )
    invalid = dashboard_client.post(
        "/api/volumes/vms/inventory/create",
        json={"image": "other", "size_gib": 1}, headers={"Idempotency-Key": "short"},
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert invalid.status_code == 400


def test_propose_resize_volume_is_expand_only_and_creates_risky_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch, current_size=10 * 1024 ** 3)
    _login(dashboard_client)

    shrink = dashboard_client.post(
        "/api/volumes/vms/inventory/vm-01/resize", json={"size_gib": 9}
    )
    expand = dashboard_client.post(
        "/api/volumes/vms/inventory/vm-01/resize", json={"size_gib": 20}
    )

    assert shrink.status_code == 409
    assert expand.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.get(Action, expand.json()["action_id"])
        assert action.action_id == "rbd_resize_volume"
        assert action.classification == "RISKY"
        assert "--allow-shrink" not in action.proposed_command


def test_propose_rename_volume_creates_risky_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/api/volumes/vms/inventory/vm-01/rename", json={"new_image": "vm-renamed"}
    )

    assert response.status_code == 201
    with db_module.SessionLocal() as session:
        action = session.get(Action, response.json()["action_id"])
        assert action.action_id == "rbd_rename_volume"
        assert action.classification == "RISKY"
        assert json.loads(action.action_params) == {
            "pool_name": "vms", "image": "vm-01", "new_image": "vm-renamed"
        }
        assert action.proposed_command.endswith("rbd info vms/vm-renamed --format json")


def test_propose_rename_volume_rejects_existing_destination_or_watcher(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_inventory", lambda pool: [{"name": "exists"}],
    )
    _login(dashboard_client)

    existing = dashboard_client.post(
        "/api/volumes/vms/inventory/vm-01/rename", json={"new_image": "exists"}
    )
    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_inventory", lambda pool: [])
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_image_detail",
        lambda pool, image: {"pool": pool, "name": image, "size": 1, "watchers": [{"client": "client.1"}]},
    )
    attached = dashboard_client.post(
        "/api/volumes/vms/inventory/vm-01/rename", json={"new_image": "vm-renamed"}
    )

    assert existing.status_code == 409
    assert attached.status_code == 409


def test_propose_rename_volume_rejects_destination_reserved_by_pending_create(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch)
    _login(dashboard_client)

    create = dashboard_client.post(
        "/api/volumes/vms/inventory/create", json={"image": "reserved", "size_gib": 10}
    )
    rename = dashboard_client.post(
        "/api/volumes/vms/inventory/vm-01/rename", json={"new_image": "reserved"}
    )

    assert create.status_code == 201
    assert rename.status_code == 409


def test_propose_trash_move_blocks_dependencies_and_creates_risky_action(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch)
    _login(dashboard_client)

    proposed = dashboard_client.post("/api/volumes/vms/inventory/vm-01/trash", json={})
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_image_detail",
        lambda pool, image: {"watchers": [{"client": "client.1"}], "snapshots": [], "children": []},
    )
    blocked = dashboard_client.post("/api/volumes/vms/inventory/vm-busy/trash", json={})

    assert proposed.status_code == 201
    assert blocked.status_code == 409
    with db_module.SessionLocal() as session:
        action = session.get(Action, proposed.json()["action_id"])
        assert action.action_id == "rbd_trash_move_volume"
        assert action.classification == "RISKY"


def test_propose_trash_move_blocks_running_backup(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch)
    with db_module.SessionLocal() as session:
        session.add(BackupJob(run_id="run-1", pool="vms", image="vm-01", job_type="full", status="RUNNING"))
        session.commit()
    _login(dashboard_client)

    response = dashboard_client.post("/api/volumes/vms/inventory/vm-01/trash", json={})

    assert response.status_code == 409
    assert "backup" in response.json()["detail"]


def test_propose_trash_restore_validates_entry_and_destination(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _stub_volume_mutation_preflight(monkeypatch)
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_trash",
        lambda pool: [{"id": "123abc", "name": "vm-old"}],
    )
    _login(dashboard_client)

    proposed = dashboard_client.post(
        "/api/volumes/vms/trash/123abc/restore", json={"image": "vm-restored"}
    )
    missing = dashboard_client.post(
        "/api/volumes/vms/trash/missing/restore", json={"image": "other"}
    )
    monkeypatch.setattr(
        volumes_route.ceph_client, "query_rbd_inventory", lambda pool: [{"name": "exists"}],
    )
    conflict = dashboard_client.post(
        "/api/volumes/vms/trash/123abc/restore", json={"image": "exists"}
    )

    assert proposed.status_code == 201
    assert missing.status_code == 404
    assert conflict.status_code == 409
    with db_module.SessionLocal() as session:
        action = session.get(Action, proposed.json()["action_id"])
        assert action.action_id == "rbd_trash_restore_volume"
        assert json.loads(action.action_params) == {
            "pool_name": "vms", "image": "vm-restored", "trash_id": "123abc"
        }
