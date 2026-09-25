from shared import db as db_module
from shared.models import Cluster


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"})
    assert response.status_code in {200, 303}


def test_runtime_decision_screen_and_cluster_mode_are_operator_visible(
    dashboard_client, default_cluster_id,
):
    _login(dashboard_client)

    page = dashboard_client.get("/remediation-runtime")
    assert page.status_code == 200
    assert "Remediation Runtime Decisions" in page.text
    assert "Không hiển thị secret hoặc command" in page.text

    changed = dashboard_client.post(
        f"/remediation-runtime/clusters/{default_cluster_id}/mode",
        data={"mode": "APPROVAL_REQUIRED", "reason": "staging safety review"},
        follow_redirects=False,
    )
    assert changed.status_code == 303

    with db_module.SessionLocal() as session:
        cluster = session.get(Cluster, default_cluster_id)
        assert cluster.autopilot_mode == "APPROVAL_REQUIRED"

    api = dashboard_client.get(f"/api/remediation-runtime/decisions?cluster={default_cluster_id}")
    assert api.status_code == 200
    assert api.json()["decisions"] == []
