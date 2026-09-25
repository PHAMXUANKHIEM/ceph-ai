import json
from datetime import datetime

import bcrypt

from shared import db
from shared.models import Action, AuditEntry, Incident, User


def _login(client, username="admin", password="admin"):
    return client.post("/login", data={"username": username, "password": password})


def test_block_storage_audit_is_cluster_scoped_and_redacted(
    dashboard_client, default_cluster_id,
):
    with db.SessionLocal() as session:
        incident = Incident(
            id="block-audit-incident", cluster_id=default_cluster_id,
            ceph_code="RBD_OPERATION", status="FAILED", detected_at=datetime(2026, 9, 25),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id, action_id="rbd_resize_volume", classification="RISKY",
            status="EXECUTED", proposed_command="rbd resize volumes/vm-a --size 20G",
            action_params=json.dumps({
                "pool_name": "volumes", "image": "vm-a", "password": "super-secret",
            }),
        )
        session.add(action)
        session.flush()
        session.add(AuditEntry(
            incident_id=incident.id, action_id=action.id,
            event_type="risky_action_executed", actor="operator", created_at=datetime(2026, 9, 25),
        ))
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/api/block-storage/audit?pool=volumes&image=vm-a")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["entries"][0]["action"] == "rbd_resize_volume"
    assert payload["entries"][0]["result"] == "succeeded"
    assert "super-secret" not in response.text
    assert "redacted" in response.text.lower()


def test_block_storage_audit_page_has_filters_and_empty_state(dashboard_client, default_cluster_id):
    _login(dashboard_client)

    response = dashboard_client.get(f"/block-storage/audit?cluster_id={default_cluster_id}")

    assert response.status_code == 200
    assert "Audit Block Storage" in response.text
    assert "Lọc audit Block Storage" in response.text
    assert "Chưa có audit Block Storage" in response.text


def test_block_storage_audit_requires_admin(dashboard_client):
    password = "operator-password"
    with db.SessionLocal() as session:
        session.add(User(
            username="block-audit-viewer", password_hash=bcrypt.hashpw(
                password.encode(), bcrypt.gensalt()
            ).decode(), is_admin=False, is_active=True, created_by="admin",
        ))
        session.commit()

    _login(dashboard_client, "block-audit-viewer", password)

    assert dashboard_client.get("/block-storage/audit").status_code == 403
    assert dashboard_client.get("/api/block-storage/audit").status_code == 403
