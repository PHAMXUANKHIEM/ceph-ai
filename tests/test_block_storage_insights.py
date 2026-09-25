from datetime import datetime, timedelta

import dashboard.routes.volumes as volumes_route
from shared import db as db_module
from shared.models import BackupJob, Cluster, VolumeDependencySnapshot, VolumeMetric
from watcher.block_storage_insights import (
    build_capacity_waste_summary,
    build_inventory_insights,
    build_protection_gap_insights,
    build_snapshot_clone_insights,
    persist_dependency_snapshots,
    owner_project_evidence,
)


NOW = datetime(2026, 9, 19, 12, 0, 0)


def test_capacity_summary_aggregates_measured_bytes_and_preserves_unknowns():
    summary = build_capacity_waste_summary([
        _inventory(project_id="project-a"),
        _inventory(name="volume-b", provisioned_size=500, used_size=100, project_id="project-a"),
        _inventory(name="volume-cinder", provisioned_size=250, used_size=50,
                   cinder={"project_id": "project-b"}),
        _inventory(name="volume-unknown", used_size=None),
    ])

    assert summary["volume_count"] == 4
    assert summary["measured_volume_count"] == 3
    assert summary["unknown_capacity_count"] == 1
    assert summary["provisioned_bytes"] == 1750
    assert summary["used_bytes"] == 550
    assert summary["unconsumed_provisioned_bytes"] == 1200
    assert summary["by_project"]["project-a"] == {
        "volumes": 2, "provisioned_bytes": 1500, "used_bytes": 500,
    }
    assert summary["by_project"]["project-b"]["volumes"] == 1
    assert "not a safe" in summary["interpretation"]


def test_owner_project_evidence_never_guesses_and_accepts_explicit_cinder_fields():
    assert owner_project_evidence({"name": "volume-project-secret"}) == {
        "owner_id": None, "project_id": None, "source": None, "verified": False,
    }
    assert owner_project_evidence({"cinder": {"project_id": "project-1", "user_id": "user-1"}}) == {
        "owner_id": "user-1", "project_id": "project-1",
        "source": "cinder_metadata", "verified": True,
    }


def _inventory(**overrides):
    row = {
        "pool": "images", "name": "volume-a", "provisioned_size": 1000,
        "used_size": 400, "snapshot_count": 0,
        "attachment_state": "idle", "watcher_count": 0,
    }
    row.update(overrides)
    return row


def test_stale_unattached_requires_recent_zero_io_evidence():
    result = build_inventory_insights(
        [_inventory()],
        {("images", "volume-a"): [
            {"iops": 0, "polled_at": NOW - timedelta(days=1)},
            {"iops": 0, "polled_at": NOW - timedelta(days=2)},
        ]},
        now=NOW,
    )

    assert result[0]["kind"] == "STALE_UNATTACHED"
    assert result[0]["estimated_reclaimable_bytes"] == 1000
    assert result[0]["confidence"] == 0.9
    assert result[0]["recommendation_mode"] == "ADVISORY"
    assert result[0]["read_only"] is True
    assert result[0]["action_id"] is None
    assert result[0]["expected_saving_bytes"] == 1000
    assert result[0]["ttl_seconds"] == 900
    assert result[0]["evidence_expires_at"].endswith("Z")


def test_missing_history_is_not_called_stale():
    result = build_inventory_insights([_inventory()], {}, now=NOW)

    assert result[0]["kind"] == "INSUFFICIENT_EVIDENCE"
    assert result[0]["estimated_reclaimable_bytes"] is None
    assert result[0]["confidence"] is None


def test_snapshot_protects_stale_volume_from_reclaim_estimate():
    result = build_inventory_insights(
        [_inventory(snapshot_count=2)],
        {("images", "volume-a"): [{"iops": 0, "polled_at": NOW - timedelta(days=1)}]},
        now=NOW,
    )

    assert result[0]["kind"] == "STALE_UNATTACHED"
    assert result[0]["estimated_reclaimable_bytes"] == 0
    assert "snapshot" in result[0]["reason"]


def test_snapshot_policy_blocks_reclaim_estimate_but_keeps_review():
    result = build_inventory_insights(
        [_inventory(snapshot_count=2)],
        {("images", "volume-a"): [{"iops": 0, "polled_at": NOW - timedelta(days=1)}]},
        now=NOW, policy_keys={("images", "volume-a")},
    )

    assert result[0]["kind"] == "STALE_UNATTACHED"
    assert result[0]["estimated_reclaimable_bytes"] == 0


def test_attached_volume_is_not_stale_even_when_idle():
    result = build_inventory_insights(
        [_inventory(attachment_state="attached", watcher_count=1)],
        {("images", "volume-a"): [{"iops": 0, "polled_at": NOW - timedelta(days=1)}]},
        now=NOW,
    )

    assert result == []


def test_inventory_insights_api_returns_evidence_and_coverage(dashboard_client, monkeypatch):
    monkeypatch.setattr(volumes_route, "_rbd_pools_for_request", lambda request: ["images"])
    monkeypatch.setattr(
        volumes_route, "_cached_rbd_inventory_with_state",
        lambda cluster, pool: ([_inventory()], {
            "stale": False, "refreshing": False, "age_seconds": 2.0,
            "source": "cache",
        }),
    )
    with db_module.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).one()
        session.add(VolumeMetric(
            cluster_id=cluster.id, pool="images", image="volume-a", iops=0,
            read_latency_ms=0, write_latency_ms=0, saturated=False,
            polled_at=NOW - timedelta(days=1),
        ))
        session.commit()

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/volumes/images/inventory-insights")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["stale_unattached"] == 1
    assert payload["coverage"]["owner_project"] is False
    assert payload["coverage"]["owner_project_mapped"] == 0
    assert payload["summary"]["capacity"]["provisioned_bytes"] == 1000
    assert payload["summary"]["capacity"]["used_bytes"] == 400
    assert payload["insights"][0]["kind"] == "STALE_UNATTACHED"


def test_snapshot_and_clone_insights_report_protection_and_dependency():
    result = build_snapshot_clone_insights([
        {
            "pool": "images", "name": "base", "snapshots": [{"name": "gold"}],
            "children": [{"pool": "images", "image": "clone-a"}],
            "parent": None, "partial_errors": {},
        },
        {
            "pool": "images", "name": "clone-a", "snapshots": [],
            "children": [], "parent": "images/base@gold", "partial_errors": {},
        },
    ])

    assert {item["kind"] for item in result} == {
        "SNAPSHOT_RETENTION_GAP", "CLONE_PARENT", "CLONE_CHILD",
    }
    assert any(item["recommendation"].startswith("Resolve child") for item in result)
    assert all(item["recommendation_mode"] == "ADVISORY" and item["read_only"] for item in result)
    assert all(item["action_id"] is None and item["ttl_seconds"] == 900 for item in result)


def test_snapshot_clone_insights_fail_closed_on_partial_evidence():
    result = build_snapshot_clone_insights([{
        "pool": "images", "name": "base", "snapshots": [], "children": [],
        "parent": None, "partial_errors": {"children": "permission denied"},
    }])

    assert result[0]["kind"] == "INSUFFICIENT_EVIDENCE"
    assert result[0]["confidence"] is None


def test_protection_gap_reports_missing_backup_and_restore_drill():
    result = build_protection_gap_insights(
        [_inventory()], {("images", "volume-a"): []}, now=NOW,
        restore_drill_rows=[],
    )

    assert {item["kind"] for item in result} == {"NO_SUCCESSFUL_BACKUP", "RESTORE_DRILL_GAP"}
    assert result[0]["recommendation"]
    assert result[0]["evidence_gaps"]
    assert result[0]["recommendation_mode"] == "ADVISORY"
    assert result[0]["read_only"] is True
    assert result[0]["action_id"] is None
    assert result[0]["expected_saving_bytes"] is None
    assert result[0]["ttl_seconds"] == 900


def test_protection_gap_reports_stale_and_newer_failed_backup():
    result = build_protection_gap_insights(
        [_inventory()],
        {("images", "volume-a"): [
            {"job_type": "full", "status": "SUCCESS", "created_at": NOW - timedelta(days=3)},
            {"job_type": "incremental", "status": "FAILED", "created_at": NOW - timedelta(days=1)},
        ]},
        now=NOW, backup_max_age_hours=24,
        restore_drill_rows=[{"status": "SUCCESS", "created_at": NOW - timedelta(hours=1)}],
    )

    assert result[0]["kind"] == "BACKUP_FAILED"
    assert result[0]["last_failure_at"].startswith("2026-09-18")


def test_protection_gap_does_not_warn_for_recent_successful_backup_and_drill():
    result = build_protection_gap_insights(
        [_inventory()],
        {("images", "volume-a"): [
            {"job_type": "full", "status": "SUCCESS", "created_at": NOW - timedelta(hours=2)},
        ]},
        now=NOW, backup_max_age_hours=24,
        restore_drill_rows=[{"status": "SUCCESS", "created_at": NOW - timedelta(hours=1)}],
    )

    assert result == []


def test_protection_insights_api_is_read_only_and_exposes_coverage(dashboard_client, monkeypatch):
    monkeypatch.setattr(volumes_route, "_rbd_pools_for_request", lambda request: ["images"])
    monkeypatch.setattr(
        volumes_route, "_cached_rbd_inventory_with_state",
        lambda cluster, pool: ([_inventory()], {
            "stale": False, "refreshing": False, "age_seconds": 2.0,
            "source": "cache",
        }),
    )
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/volumes/images/protection-insights")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["no_successful_backup"] == 1
    assert payload["summary"]["restore_drill_gap"] == 1
    assert payload["coverage"]["restore_drill_per_volume"] is False


def test_protection_insights_does_not_leak_backup_history_between_clusters(dashboard_client, monkeypatch):
    monkeypatch.setattr(volumes_route, "_rbd_pools_for_request", lambda request: ["images"])
    monkeypatch.setattr(
        volumes_route, "_cached_rbd_inventory_with_state",
        lambda cluster, pool: ([_inventory()], {
            "stale": False, "refreshing": False, "age_seconds": 1.0,
            "source": "cache",
        }),
    )
    with db_module.SessionLocal() as session:
        default = session.query(Cluster).filter_by(is_default=True).one()
        secondary = Cluster(
            name="protection-secondary", ceph_mon_nodes="10.2.0.2", ceph_container_name="mon",
            ssh_user="ceph", ssh_key_path="/key", ceph_exec_mode="cephadm",
            is_default=False, is_active=True,
        )
        session.add(secondary)
        session.flush()
        session.add_all([
            BackupJob(
                cluster_id=default.id, run_id="run-default-failed", pool="images", image="volume-a",
                job_type="full", status="FAILED", created_at=datetime.utcnow() - timedelta(hours=1),
            ),
            BackupJob(
                cluster_id=secondary.id, run_id="run-secondary-success", pool="images", image="volume-a",
                job_type="full", status="SUCCESS", created_at=datetime.utcnow() - timedelta(hours=1),
            ),
            BackupJob(
                cluster_id=secondary.id, run_id="run-secondary-drill", pool="images", image="drill",
                job_type="restore_drill", status="SUCCESS", created_at=datetime.utcnow() - timedelta(hours=1),
            ),
        ])
        session.commit()
        secondary_id = secondary.id

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get(f"/api/volumes/images/protection-insights?cluster={secondary_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cluster_id"] == secondary_id
    kinds = {item["kind"] for item in payload["insights"]}
    assert "BACKUP_FAILED" not in kinds
    assert "NO_SUCCESSFUL_BACKUP" not in kinds
    assert "RESTORE_DRILL_GAP" not in kinds


def test_snapshot_clone_insights_api_is_bounded_and_read_only(dashboard_client, monkeypatch):
    monkeypatch.setattr(volumes_route, "_rbd_pools_for_request", lambda request: ["images"])
    monkeypatch.setattr(
        volumes_route, "_cached_rbd_inventory_with_state",
        lambda cluster, pool: ([
            {"name": "base", "snapshot_count": 1},
            {"name": "idle", "snapshot_count": 0},
        ], {"stale": False, "refreshing": False, "age_seconds": 1.0, "source": "cache"}),
    )
    calls = []

    def detail(pool, image):
        calls.append((pool, image))
        return {
            "pool": pool, "name": image,
            "snapshots": [{"name": "gold"}] if image == "base" else [],
            "children": [], "parent": None, "partial_errors": {},
        }

    monkeypatch.setattr(volumes_route.ceph_client, "query_rbd_image_detail", detail)
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/volumes/images/snapshot-clone-insights?max_images=1")

    assert response.status_code == 200
    assert response.json()["queried_images"] == 1
    assert response.json()["persisted_observations"] == 1
    assert response.json()["insights"][0]["kind"] == "SNAPSHOT_RETENTION_GAP"
    assert calls == [("images", "base")]


def test_dependency_snapshots_are_persisted_and_pruned(dashboard_client):
    with db_module.SessionLocal() as session:
        cluster = session.query(Cluster).filter_by(is_default=True).one()
        session.add(VolumeDependencySnapshot(
            cluster_id=cluster.id, pool="images", image="old", snapshot_count=1,
            parent_json="null", children_json="[]", partial_errors_json="{}",
            captured_at=NOW - timedelta(days=31),
        ))
        session.commit()
        cluster_id = cluster.id

    count = persist_dependency_snapshots(cluster_id, [{
        "pool": "images", "name": "base", "snapshots": [{"name": "gold"}],
        "parent": None, "children": [{"pool": "images", "image": "clone-a"}],
        "partial_errors": {},
    }], captured_at=NOW)

    assert count == 1
    with db_module.SessionLocal() as session:
        rows = session.query(VolumeDependencySnapshot).filter_by(cluster_id=cluster_id).all()
        assert {row.image for row in rows} == {"base"}
        assert rows[0].snapshot_count == 1
        assert "clone-a" in rows[0].children_json
