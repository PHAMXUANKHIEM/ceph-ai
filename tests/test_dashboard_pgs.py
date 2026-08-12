import dashboard.routes.pgs as pgs_route
from config.settings import settings
from shared import db as db_module
from shared.models import Cluster
from watcher.ceph_client import CephQueryError


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _configure_pools(monkeypatch):
    monkeypatch.setattr(settings, "ceph_rbd_pools", "vms,backups")


def test_unauthenticated_pgs_page_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/pgs", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_pgs_page_lists_pools_without_live_query(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    _login(dashboard_client)
    response = dashboard_client.get("/pgs")
    assert response.status_code == 200
    assert "vms" in response.text
    assert "backups" in response.text
    assert "Chọn một pool để xem Placement Groups" in response.text


def test_pgs_page_returns_normalized_rows_for_selected_pool(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        pgs_route.ceph_client,
        "run_ceph_json_command",
        lambda command: (
            "mon1",
            {
                "pg_stats": [
                    {
                        "pgid": "1.a",
                        "state": "active+clean",
                        "up": [0, 1, 2],
                        "acting": [0, 1, 2],
                        "stat_sum": {"num_objects": 42, "num_bytes": 4096},
                        "last_scrub_stamp": "2026-08-11T01:02:03Z",
                    }
                ]
            },
        ),
    )
    _login(dashboard_client)
    response = dashboard_client.get("/pgs?pool=vms")
    assert response.status_code == 200
    assert "1.a" in response.text
    assert "active+clean" in response.text
    assert "[0, 1, 2]" in response.text
    assert "42" in response.text


def test_pgs_page_rejects_unknown_pool_without_running_command(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)
    monkeypatch.setattr(
        pgs_route.ceph_client,
        "run_ceph_json_command",
        lambda _command: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    _login(dashboard_client)
    response = dashboard_client.get("/pgs?pool=unknown")
    assert response.status_code == 404


def test_pgs_page_shows_ceph_query_error(dashboard_client, monkeypatch):
    _configure_pools(monkeypatch)

    def fail(_command):
        raise CephQueryError("all MON nodes unavailable")

    monkeypatch.setattr(pgs_route.ceph_client, "run_ceph_json_command", fail)
    _login(dashboard_client)
    response = dashboard_client.get("/pgs?pool=vms")
    assert response.status_code == 200
    assert "all MON nodes unavailable" in response.text


def test_pgs_page_uses_selected_additional_cluster(dashboard_client, monkeypatch):
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="cluster-b", ceph_mon_nodes="10.0.0.21", ceph_container_name="mon-b",
            ssh_user="ceph-b", ssh_key_path="/keys/b", ceph_exec_mode="cephadm",
            is_default=False, is_active=True,
        )
        session.add(cluster)
        session.commit()
        cluster_id = cluster.id

    calls = []

    def fake_query(*args):
        calls.append(args)
        if args[-1] == "ceph osd pool ls detail":
            return "10.0.0.21", [{"pool_name": "rbd-b", "application_metadata": {"rbd": {}}}]
        return "10.0.0.21", {"pg_stats": [{"pgid": "9.a", "state": "active+clean"}]}

    monkeypatch.setattr(pgs_route, "run_ceph_json_command_with", fake_query)
    _login(dashboard_client)
    dashboard_client.get(f"/?cluster={cluster_id}")
    response = dashboard_client.get("/pgs?pool=rbd-b")

    assert response.status_code == 200
    assert "9.a" in response.text
    assert calls[0] == (
        ["10.0.0.21"], "mon-b", "ceph-b", "/keys/b", "cephadm",
        "ceph osd pool ls detail",
    )
