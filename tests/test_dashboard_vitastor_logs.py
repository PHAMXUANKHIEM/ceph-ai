from shared import db
from shared.models import VitastorCluster
import dashboard.routes.vitastor as route


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin", "product": "vitastor"})


def _cluster():
    with db.SessionLocal() as session:
        row = VitastorCluster(name="vita-logs", management_host="10.0.0.20", etcd_address="etcd:2379", etcd_prefix="/vitastor", config_path="", ssh_user="root", ssh_key_path="/key", exec_mode="none", container_name="", is_active=True, created_by="admin", last_status_json='{"deployment":{"nodes":[{"host":"10.0.0.21"}]}}')
        session.add(row); session.commit(); return row.id


def test_logs_page_has_presets_and_keyboard_filter(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/vitastor/logs")
    assert response.status_code == 200
    assert "Error / Failed / Fatal" in response.text
    assert 'id="vl-keyword"' in response.text
    assert "Tự làm mới 10 giây" in response.text


def test_logs_api_applies_preset_and_typed_keyword(dashboard_client, monkeypatch):
    cluster_id = _cluster(); _login(dashboard_client)
    calls = []
    monkeypatch.setattr(route, "query_logs", lambda *args, **kwargs: calls.append((args, kwargs)) or "one INFO ready\ntwo ERROR osd timeout\nthree ERROR mon stopped")
    response = dashboard_client.get(f"/vitastor/api/logs?cluster_id={cluster_id}&host=10.0.0.21&preset=errors&keyword=osd")
    assert response.status_code == 200
    assert response.json()["lines"] == ["two ERROR osd timeout"]
    assert response.json()["host"] == "10.0.0.21"
    assert calls[0][0][0] == "10.0.0.21"


def test_logs_api_rejects_unlisted_source_and_control_char(dashboard_client):
    _login(dashboard_client)
    assert dashboard_client.get("/vitastor/api/logs?source=shell").status_code == 400
    assert dashboard_client.get("/vitastor/api/logs?keyword=bad%0Avalue").status_code == 400


def test_logs_api_rejects_host_outside_cluster(dashboard_client):
    cluster_id = _cluster(); _login(dashboard_client)
    response = dashboard_client.get(f"/vitastor/api/logs?cluster_id={cluster_id}&host=attacker.example")
    assert response.status_code == 400
