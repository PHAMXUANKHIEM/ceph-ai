from datetime import datetime

from dashboard.routes import storage_audit
from shared import db
from shared.models import BackupJob, Cluster, ObjectStorageAuditEntry


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "admin"})
    assert response.status_code in {200, 303}


def test_storage_audit_viewer_is_cluster_scoped_and_redacts_details(dashboard_client):
    now = datetime.utcnow()
    with db.SessionLocal() as session:
        default_cluster = session.query(Cluster).filter(Cluster.is_default.is_(True)).first()
        session.add(ObjectStorageAuditEntry(
            cluster_id=default_cluster.id,
            actor="admin",
            action="quota_set",
            target_type="bucket",
            target_id="safe-bucket",
            preview="secret=must-not-leak",
            result="succeeded",
            created_at=now,
        ))
        session.add(BackupJob(
            cluster_id=None,
            run_id="audit-test-run",
            pool="vms",
            image="disk-a",
            job_type="full",
            status="SUCCESS",
            created_at=now,
        ))
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/api/audit/storage?page_size=10")

    assert response.status_code == 200
    body = response.json()
    assert body["read_only"] is True
    assert {row["kind"] for row in body["items"]} >= {"object_storage", "backup"}
    assert "secret=must-not-leak" not in response.text
    assert all("preview" not in row for row in body["items"])


def test_storage_audit_viewer_supports_kind_filter_and_html_page(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/api/audit/storage?kind=not-a-kind")
    assert response.status_code == 200
    assert response.json()["kind"] == ""

    page = dashboard_client.get("/audit")
    assert page.status_code == 200
    assert "Audit Viewer" in page.text


def test_legacy_global_bucket_purge_is_fail_closed(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/api/object-storage/buckets/delete-all")

    assert response.status_code == 409
    assert "preview/execute" in response.json()["detail"]
