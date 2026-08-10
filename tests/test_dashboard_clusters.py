from datetime import datetime

import dashboard.routes.clusters as clusters_route
from shared import db as db_module
from shared.models import Cluster, Incident


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _cluster_form_data(**overrides):
    data = {
        "name": "cluster-b",
        "ceph_mon_nodes": "10.30.1.10,10.30.1.11",
        "ceph_container_name": "ceph-mon",
        "ssh_user": "root",
        "ssh_key_path": "/root/.ssh/ceph_aiops_watcher",
        "ceph_exec_mode": "docker",
    }
    data.update(overrides)
    return data


def _stub_restart_watcher(monkeypatch):
    """create_cluster()/toggle_cluster_active() now call the REAL
    restart_watcher() (dashboard/routes/settings.py) on success — that
    spawns an actual `python -m watcher.main` OS subprocess via
    subprocess.Popen (see _start_process()), same as every other route
    that touches Worker/Watcher config. Every test below that reaches a
    successful create/toggle MUST stub this out, same convention
    test_dashboard_settings.py already uses for restart_worker — verified
    live, 2026-08-10: forgetting this once actually spawned a real Watcher
    process mid test-run, inheriting pytest's own process env (test fixture
    MON IPs) into a real background process still running after the test
    suite exited."""
    monkeypatch.setattr(clusters_route, "restart_watcher", lambda: {"restarted": True, "new_pid": 1, "error": None})


def test_unauthenticated_get_clusters_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/clusters", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_clusters_page_shows_default_cluster(dashboard_client, default_cluster_id):
    _login(dashboard_client)
    response = dashboard_client.get("/clusters")

    assert response.status_code == 200
    assert "mặc định" in response.text


def test_create_cluster_tests_connection_before_saving(dashboard_client, monkeypatch):
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _stub_restart_watcher(monkeypatch)

    _login(dashboard_client)
    response = dashboard_client.post("/clusters/create", data=_cluster_form_data())

    assert response.status_code == 200
    assert "Đã thêm cụm" in response.text
    with db_module.SessionLocal() as session:
        created = session.query(Cluster).filter_by(name="cluster-b").one()
        assert created.is_default is False
        assert created.is_active is True
        assert created.ceph_mon_nodes == "10.30.1.10,10.30.1.11"


def test_create_cluster_never_touches_the_default_clusters_sticky_mon_node(dashboard_client, monkeypatch):
    """Regression guard: watcher/ceph_client.py::query_cluster_health_with's
    update_sticky_fallback must be False for this call site (see clusters.py's
    own comment) — a successful test-connection here must never overwrite
    the DEFAULT cluster's sticky MON node used by its own log collection."""
    from watcher import ceph_client

    ceph_client.last_successful_mon_node = "10.20.1.150"  # the default cluster's real sticky value

    def fake_query(mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode, update_sticky_fallback=True):
        assert update_sticky_fallback is False
        return {"status": "HEALTH_OK"}

    monkeypatch.setattr(clusters_route, "query_cluster_health_with", fake_query)
    _stub_restart_watcher(monkeypatch)

    _login(dashboard_client)
    dashboard_client.post("/clusters/create", data=_cluster_form_data())

    assert ceph_client.last_successful_mon_node == "10.20.1.150"  # unchanged


def test_create_cluster_connection_failure_does_not_save(dashboard_client, monkeypatch):
    from watcher.ceph_client import CephQueryError

    def failing_query(*a, **kw):
        raise CephQueryError("no MON node reachable")

    monkeypatch.setattr(clusters_route, "query_cluster_health_with", failing_query)

    _login(dashboard_client)
    response = dashboard_client.post("/clusters/create", data=_cluster_form_data())

    assert response.status_code == 200
    assert "Không kết nối được tới cụm" in response.text
    with db_module.SessionLocal() as session:
        assert session.query(Cluster).filter_by(name="cluster-b").first() is None


def test_create_cluster_missing_required_field_skips_connection_test(dashboard_client, monkeypatch):
    called = []
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: called.append(1))

    _login(dashboard_client)
    response = dashboard_client.post("/clusters/create", data=_cluster_form_data(ceph_mon_nodes=""))

    assert response.status_code == 200
    assert called == []


def test_toggle_active_flips_an_additional_cluster(dashboard_client, monkeypatch):
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _stub_restart_watcher(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/clusters/create", data=_cluster_form_data())
    with db_module.SessionLocal() as session:
        cluster_id = session.query(Cluster).filter_by(name="cluster-b").one().id

    response = dashboard_client.post(f"/clusters/{cluster_id}/toggle-active")

    assert response.status_code == 200
    with db_module.SessionLocal() as session:
        assert session.get(Cluster, cluster_id).is_active is False


def test_toggle_active_rejects_the_default_cluster(dashboard_client, default_cluster_id):
    _login(dashboard_client)
    response = dashboard_client.post(f"/clusters/{default_cluster_id}/toggle-active")

    assert response.status_code == 200
    assert "Không thể vô hiệu hoá cụm mặc định" in response.text
    with db_module.SessionLocal() as session:
        assert session.get(Cluster, default_cluster_id).is_active is True


# --- Cluster switcher on the main Dashboard page ----------------------------


def test_index_filters_incidents_by_selected_cluster(dashboard_client, monkeypatch, default_cluster_id):
    monkeypatch.setattr(clusters_route, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK"})
    _stub_restart_watcher(monkeypatch)
    _login(dashboard_client)
    dashboard_client.post("/clusters/create", data=_cluster_form_data())
    with db_module.SessionLocal() as session:
        other_cluster_id = session.query(Cluster).filter_by(name="cluster-b").one().id
        session.add(
            Incident(
                ceph_code="DEFAULT_CLUSTER_ISSUE",
                status="NEW",
                detected_at=datetime.utcnow(),
                cluster_id=default_cluster_id,
            )
        )
        session.add(
            Incident(
                ceph_code="OTHER_CLUSTER_ISSUE",
                status="NEW",
                detected_at=datetime.utcnow(),
                cluster_id=other_cluster_id,
            )
        )
        session.commit()

    default_view = dashboard_client.get("/")
    other_view = dashboard_client.get(f"/?cluster={other_cluster_id}")

    assert "DEFAULT_CLUSTER_ISSUE" in default_view.text
    assert "OTHER_CLUSTER_ISSUE" not in default_view.text
    assert "OTHER_CLUSTER_ISSUE" in other_view.text
    assert "DEFAULT_CLUSTER_ISSUE" not in other_view.text


def test_index_falls_back_to_default_cluster_for_unknown_cluster_query_param(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/?cluster=does-not-exist")

    assert response.status_code == 200  # no 404/500 on a stale/bad bookmark


# 2026-08-10: user-reported bug — the Clusters nav link was missing from
# every template EXCEPT clusters.html/crush_map.html/index.html, because
# this app has no shared nav partial (every template hand-copies its own
# topbar, same root cause test_dashboard_users.py's own
# test_nav_shows_users_link_for_admin_on_other_pages already documents for
# the Users link). Covers every admin-nav page that got the fix, not just
# a small sample, since the actual bug spanned all of them.
def test_nav_shows_clusters_link_for_admin_on_every_page(dashboard_client):
    _login(dashboard_client)

    for path in (
        "/",
        "/nodes",
        "/volumes",
        "/deploy-cluster",
        "/delete-cluster",
        "/convert-cluster",
        "/upgrade",
        "/patch",
        "/backups",
        "/restore-cluster",
        "/bucket-access-log",
        "/settings",
        "/telegram-alerts",
        "/telegram-alerts/help",
        "/users",
        "/crush-map",
        "/clusters",
    ):
        response = dashboard_client.get(path)
        assert 'href="/clusters"' in response.text, f"missing Clusters nav link on {path}"


def test_nav_hides_clusters_link_for_non_admin_on_other_pages(dashboard_client):
    from tests.test_dashboard_users import _create_user, _login_as

    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    for path in ("/", "/nodes", "/upgrade", "/settings"):
        response = dashboard_client.get(path)
        assert 'href="/clusters"' not in response.text, f"Clusters nav link leaked on {path}"
