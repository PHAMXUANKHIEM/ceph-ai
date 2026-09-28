from datetime import datetime, timedelta
from pathlib import Path

import dashboard.routes.volumes as volumes_route
from shared import db as db_module
from shared.models import Cluster, VolumeMetric
from watcher.block_storage_insights import build_inventory_insights


NOW = datetime(2026, 9, 21, 12, 0, 0)


def _inventory(**overrides):
    row = {
        "name": "volume-a",
        "provisioned_size": 1024,
        "used_size": 512,
        "snapshot_count": 0,
        "pool": "images",
        "attachment_state": "idle",
        "watcher_count": 0,
        "lock_count": 0,
    }
    row.update(overrides)
    return row


def test_stale_unattached_requires_zero_io_and_complete_attachment_evidence():
    result = build_inventory_insights(
        [_inventory()],
        {("images", "volume-a"): [
            {"iops": 0, "polled_at": NOW - timedelta(days=1)},
            {"iops": 0, "polled_at": NOW - timedelta(days=2)},
        ]},
        now=NOW,
    )

    assert result[0]["kind"] == "STALE_UNATTACHED"
    assert result[0]["estimated_reclaimable_bytes"] == 1024
    assert result[0]["recommendation_mode"] == "ADVISORY"
    assert result[0]["read_only"] is True
    assert result[0]["action_id"] is None


def test_missing_io_or_attachment_evidence_fails_closed():
    result = build_inventory_insights([_inventory(lock_count=None)], {}, now=NOW)

    assert result[0]["kind"] == "INSUFFICIENT_EVIDENCE"
    assert result[0]["confidence"] is None
    assert result[0]["estimated_reclaimable_bytes"] is None
    assert result[0]["action_id"] is None


def test_attached_volume_is_not_marked_stale():
    result = build_inventory_insights(
        [_inventory(attachment_state="attached", watcher_count=1)],
        {("images", "volume-a"): [{"iops": 0, "polled_at": NOW - timedelta(days=1)}]},
        now=NOW,
    )

    assert result == []


def test_snapshot_prevents_reclaim_estimate():
    result = build_inventory_insights(
        [_inventory(snapshot_count=2)],
        {("images", "volume-a"): [{"iops": 0, "polled_at": NOW - timedelta(days=1)}]},
        now=NOW,
    )

    assert result[0]["kind"] == "STALE_UNATTACHED"
    assert result[0]["estimated_reclaimable_bytes"] == 0


def test_snapshot_and_clone_dependencies_are_explicit_findings():
    result = build_inventory_insights(
        [_inventory(snapshot_count=2, clone_child_count=1, has_parent=True)],
        {("images", "volume-a"): [{"iops": 0, "polled_at": NOW - timedelta(days=1)}]},
        now=NOW,
    )

    kinds = {item["kind"] for item in result}
    assert {"STALE_UNATTACHED", "SNAPSHOT_DEPENDENCY", "CLONE_DEPENDENCY"} <= kinds
    assert all(item["read_only"] is True for item in result)


def test_backup_protection_gap_is_reported_without_claiming_reclaim():
    result = build_inventory_insights(
        [_inventory()],
        {("images", "volume-a"): [{"iops": 0, "polled_at": NOW - timedelta(days=1)}]},
        backup_rows={("images", "volume-a"): [{"status": "FAILED", "created_at": NOW}]},
        now=NOW,
    )

    gap = next(item for item in result if item["kind"] == "BACKUP_PROTECTION_GAP")
    assert gap["estimated_reclaimable_bytes"] is None
    assert gap["confidence"] is None


def test_recent_successful_backup_is_protected():
    result = build_inventory_insights(
        [_inventory()],
        {("images", "volume-a"): [{"iops": 0, "polled_at": NOW - timedelta(days=1)}]},
        backup_rows={("images", "volume-a"): [{"status": "SUCCESS", "created_at": NOW - timedelta(hours=2)}]},
        now=NOW,
    )

    assert not any(item["kind"] == "BACKUP_PROTECTION_GAP" for item in result)


def test_inventory_insights_api_is_scoped_and_read_only(dashboard_client, monkeypatch):
    monkeypatch.setattr(volumes_route, "_rbd_pools_for_request", lambda request: ["images"])
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "query_rbd_inventory",
        lambda pool: [_inventory()],
    )
    monkeypatch.setattr(
        volumes_route.ceph_client,
        "query_rbd_image_detail",
        lambda pool, image: {"watchers": [], "locks": [], "partial_errors": {}},
    )
    with db_module.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).one()
        session.add(VolumeMetric(
            cluster_id=cluster.id,
            pool="images",
            image="volume-a",
            iops=0,
            read_latency_ms=0,
            write_latency_ms=0,
            saturated=False,
            polled_at=datetime.utcnow() - timedelta(days=1),
        ))
        session.commit()

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/volumes/images/inventory-insights")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["stale_unattached"] == 1
    assert payload["read_only"] is True
    assert payload["action_id"] is None
    assert payload["cluster_id"]


def test_inventory_insights_rejects_unscoped_pool(dashboard_client, monkeypatch):
    monkeypatch.setattr(volumes_route, "_rbd_pools_for_request", lambda request: ["images"])
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    response = dashboard_client.get("/api/volumes/other/inventory-insights")

    assert response.status_code == 404


def test_volumes_ui_exposes_read_only_inventory_insight_panel():
    root = Path(__file__).resolve().parents[1]
    template = (root / "dashboard/templates/volumes.html").read_text()
    script = (root / "dashboard/static/volume_inventory.js").read_text()

    assert 'id="volume-ai-insights"' in template
    assert "/inventory-insights" in script
    assert "read-only" in script.lower()
