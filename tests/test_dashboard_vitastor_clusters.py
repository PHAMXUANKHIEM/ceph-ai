from shared import db as db_module
from shared.models import VitastorCluster, VitastorOperation
import dashboard.routes.vitastor_clusters as route


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin", "product": "vitastor"})


def _form(**overrides):
    values = {
        "name": "vita-lab", "management_host": "10.0.0.20",
        "etcd_address": "10.0.0.10:2379/v3,10.0.0.11:2379/v3",
        "etcd_prefix": "/vitastor", "config_path": "", "ssh_user": "root",
        "ssh_key_path": "/root/.ssh/vitastor", "exec_mode": "none", "container_name": "",
    }
    values.update(overrides)
    return values


def test_admin_sees_independent_cluster_page(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/vitastor/clusters")
    assert response.status_code == 200
    assert 'action="/vitastor/clusters/create"' in response.text
    assert "không sử dụng MON" in response.text


def test_create_tests_connection_before_saving(dashboard_client, monkeypatch):
    calls = []
    monkeypatch.setattr(route, "query_status", lambda *args: calls.append(args) or {"cluster": {"osd": "3 / 3 up"}})
    _login(dashboard_client)

    response = dashboard_client.post("/vitastor/clusters/create", data=_form())

    assert "Đã kết nối và thêm cụm" in response.text
    assert calls and calls[0][0] == "10.0.0.20"
    with db_module.SessionLocal() as session:
        row = session.query(VitastorCluster).one()
        assert row.etcd_prefix == "/vitastor"
        assert "3 / 3 up" in row.last_status_json


def test_failed_connection_is_not_saved(dashboard_client, monkeypatch):
    def fail(*_args): raise route.VitastorConnectionError("timeout")
    monkeypatch.setattr(route, "query_status", fail)
    _login(dashboard_client)
    response = dashboard_client.post("/vitastor/clusters/create", data=_form())
    assert "Không kết nối được" in response.text
    with db_module.SessionLocal() as session:
        assert session.query(VitastorCluster).count() == 0


def test_missing_etcd_and_config_rejected_without_connection(dashboard_client, monkeypatch):
    monkeypatch.setattr(route, "query_status", lambda *_: (_ for _ in ()).throw(AssertionError("must not run")))
    _login(dashboard_client)
    response = dashboard_client.post("/vitastor/clusters/create", data=_form(etcd_address="", config_path=""))
    assert "Etcd address hoặc Config path" in response.text


def test_check_toggle_and_delete_cluster(dashboard_client, monkeypatch):
    monkeypatch.setattr(route, "query_status", lambda *_: {"cluster": {"osd": "3 / 3 up"}})
    _login(dashboard_client)
    dashboard_client.post("/vitastor/clusters/create", data=_form())
    with db_module.SessionLocal() as session:
        cluster_id = session.query(VitastorCluster).one().id

    assert "hoạt động" in dashboard_client.post(f"/vitastor/clusters/{cluster_id}/check").text
    dashboard_client.post(f"/vitastor/clusters/{cluster_id}/toggle-active")
    with db_module.SessionLocal() as session:
        assert session.get(VitastorCluster, cluster_id).is_active is False
    assert "Đã xoá kết nối cụm" in dashboard_client.post(f"/vitastor/clusters/{cluster_id}/delete").text


def test_cannot_disable_or_delete_cluster_with_in_flight_operation(dashboard_client, monkeypatch):
    monkeypatch.setattr(route, "query_status", lambda *_: {"cluster": {"osd": "3 / 3 up"}})
    _login(dashboard_client)
    dashboard_client.post("/vitastor/clusters/create", data=_form())
    with db_module.SessionLocal() as session:
        cluster = session.query(VitastorCluster).one()
        session.add(VitastorOperation(
            operation="upgrade", cluster_id=cluster.id, cluster_name=cluster.name,
            params_json="{}", plan_text="test", progress_json="[]", requested_by="admin",
        ))
        session.commit()
        cluster_id = cluster.id

    toggle = dashboard_client.post(f"/vitastor/clusters/{cluster_id}/toggle-active")
    delete = dashboard_client.post(f"/vitastor/clusters/{cluster_id}/delete")

    assert "Không thể vô hiệu hoá cụm" in toggle.text
    assert "Không thể xoá cụm" in delete.text
    with db_module.SessionLocal() as session:
        assert session.get(VitastorCluster, cluster_id) is not None
        assert session.get(VitastorCluster, cluster_id).is_active is True


def test_ceph_session_cannot_open_vitastor_clusters(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin", "product": "ceph"})
    response = dashboard_client.get("/vitastor/clusters", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
