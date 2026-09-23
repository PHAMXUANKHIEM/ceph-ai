"""Backup inventory filters must remain bounded and scoped to the selected cluster."""

import csv
import io
from datetime import datetime

from shared import db
from shared.models import BackupJob, Cluster


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_inventory_filters_by_utc_day_and_existing_fields(dashboard_client):
    _login(dashboard_client)
    with db.SessionLocal() as session:
        session.add_all([
            BackupJob(run_id="inventory-old", pool="vms", image="disk-a", job_type="full",
                      status="SUCCESS", backup_target_slot="a", created_at=datetime(2026, 9, 20, 23, 59)),
            BackupJob(run_id="inventory-new", pool="vms", image="disk-b", job_type="incremental",
                      status="FAILED", backup_target_slot="b", created_at=datetime(2026, 9, 21, 0, 0)),
        ])
        session.commit()

    response = dashboard_client.get(
        "/api/backups/inventory",
        params={"created_from": "2026-09-21", "created_to": "2026-09-21", "pool": "vms",
                "image": "disk-b", "job_type": "incremental", "backup_target_slot": "b", "status": "FAILED"},
    )

    assert response.status_code == 200
    assert [item["run_id"] for item in response.json()["items"]] == ["inventory-new"]
    assert response.json()["total"] == 1


def test_inventory_rejects_invalid_or_reversed_dates(dashboard_client):
    _login(dashboard_client)
    for params in (
        {"created_from": "2026-02-30"},
        {"created_from": "2026-09-22T00:00:00"},
        {"created_from": "2026-09-22", "created_to": "2026-09-21"},
    ):
        assert dashboard_client.get("/api/backups/inventory", params=params).status_code == 400


def test_job_detail_returns_scoped_lineage_without_raw_error(dashboard_client):
    with db.SessionLocal() as session:
        secondary = Cluster(name="inventory-secondary", ceph_mon_nodes="10.20.2.112",
                            ssh_user="root", ssh_key_path="/tmp/test-key")
        session.add(secondary)
        session.flush()
        full = BackupJob(cluster_id=secondary.id, run_id="chain-full", pool="vms", image="disk-b",
                         job_type="full", status="SUCCESS", backup_target_slot="cluster",
                         created_at=datetime(2026, 9, 20, 8, 0))
        session.add(full)
        session.flush()
        incremental = BackupJob(
            cluster_id=secondary.id, run_id="chain-diff", pool="vms", image="disk-b",
            job_type="incremental", status="FAILED", backup_target_slot="cluster",
            base_job_id=full.id, error_message="backend secret must never be returned",
            created_at=datetime(2026, 9, 21, 8, 0),
        )
        session.add(incremental)
        session.commit()
        cluster_id, job_id = secondary.id, incremental.id

    _login(dashboard_client)
    assert dashboard_client.get(f"/api/backups/jobs/{job_id}").status_code == 404
    response = dashboard_client.get(f"/api/backups/jobs/{job_id}", params={"cluster_id": cluster_id})
    assert response.status_code == 200
    assert response.json()["lineage_complete"] is True
    assert response.json()["lineage_scope"] == "base_links_only"
    assert [row["run_id"] for row in response.json()["lineage"]] == ["chain-full", "chain-diff"]
    assert response.json()["error_available"] is True
    assert "backend secret" not in response.text


def test_inventory_csv_export_is_scoped_and_spreadsheet_safe(dashboard_client):
    with db.SessionLocal() as session:
        session.add(BackupJob(run_id="=HYPERLINK(1)", pool="vms", image="disk-export",
                              job_type="full", status="SUCCESS", backup_target_slot="a",
                              error_message="backend secret", created_at=datetime(2026, 9, 21, 12, 0)))
        session.commit()
    _login(dashboard_client)
    response = dashboard_client.get("/api/backups/inventory/export", params={"format": "csv", "image": "disk-export"})
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    assert len(rows) == 1
    assert rows[0]["run_id"] == "'=HYPERLINK(1)"
    assert "backend secret" not in response.text
    assert response.headers["x-export-truncated"] == "false"


def test_inventory_export_rejects_unknown_format(dashboard_client):
    _login(dashboard_client)
    assert dashboard_client.get("/api/backups/inventory/export?format=xlsx").status_code == 400
