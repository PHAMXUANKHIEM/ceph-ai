from datetime import datetime, timedelta

from shared import db
from shared.models import RbdCapacitySample
from watcher import capacity_rbd


def test_rbd_du_separates_image_heads_from_snapshot_usage():
    result = capacity_rbd.summarize_rbd_du({"images": [
        {"name": "volume-a", "snapshot": "snap-1", "used_size": 30},
        {"name": "volume-a", "provisioned_size": 1000, "used_size": 200},
        {"name": "volume-b", "provisioned_size": 2000, "used_size": 400},
    ]})

    assert result == {
        "image_count": 2, "snapshot_count": 1,
        "provisioned_bytes": 3000, "head_used_bytes": 600,
        "snapshot_used_bytes": 30,
    }


def test_rbd_du_unknown_usage_is_not_zero():
    result = capacity_rbd.summarize_rbd_du({"images": [
        {"name": "volume-a", "snapshot": "snap-1"},
        {"name": "volume-a", "provisioned_size": 1000},
    ]})

    assert result["head_used_bytes"] is None
    assert result["snapshot_used_bytes"] is None
    assert capacity_rbd.summarize_rbd_du({"images": [
        {"name": "volume-a", "provisioned_size": 1},
        {"name": "volume-a", "provisioned_size": 1},
    ]}) is None


def test_rbd_collection_is_bounded_and_growth_is_observed(
    dashboard_client, default_cluster_id, monkeypatch,
):
    monkeypatch.setattr(capacity_rbd, "_configured_pools", lambda _cluster: ["volumes", "other"])
    state = {"size": 1000, "snapshots": 1}

    def query(_cluster, command):
        assert command == "rbd du --pool volumes"
        return {"images": [
            {"name": "a", "snapshot": f"s{index}", "used_size": 10}
            for index in range(state["snapshots"])
        ] + [{"name": "a", "provisioned_size": state["size"], "used_size": 100}]}

    monkeypatch.setattr(capacity_rbd, "_query", query)
    initial = datetime(2026, 9, 23, 0, 0)
    first = capacity_rbd.collect_and_store(
        default_cluster_id, None, [{"pool": "volumes", "used_bytes": 500}], now=initial,
    )
    state.update(size=1200, snapshots=2)
    second = capacity_rbd.collect_and_store(
        default_cluster_id, None, [{"pool": "volumes", "used_bytes": 550}], now=initial + timedelta(days=1),
    )
    report = capacity_rbd.evidence(default_cluster_id, now=initial + timedelta(days=1, minutes=5))

    assert first["stored"] == second["stored"] == 1
    assert report["pools"][0]["snapshot_count"] == 2
    assert report["pools"][0]["observed_growth"]["provisioned_bytes_per_day"] == 200
    assert report["pools"][0]["observed_growth"]["snapshot_count_per_day"] == 1
    assert report["pools"][0]["thin_provisioned_to_physical_used_ratio"] == round(1200 / 550, 4)
    with db.SessionLocal() as session:
        assert session.query(RbdCapacitySample).filter_by(cluster_id=default_cluster_id).count() == 2


def test_failed_rbd_query_does_not_store_fake_zero(dashboard_client, default_cluster_id, monkeypatch):
    monkeypatch.setattr(capacity_rbd, "_configured_pools", lambda _cluster: ["volumes"])
    monkeypatch.setattr(capacity_rbd, "_query", lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")))

    report = capacity_rbd.collect_and_store(
        default_cluster_id, None, [{"pool": "volumes", "used_bytes": 500}],
        now=datetime(2026, 9, 23),
    )

    assert report["stored"] == 0
    assert report["gaps"] == ["volumes: RBD usage unavailable"]
    with db.SessionLocal() as session:
        assert session.query(RbdCapacitySample).filter_by(cluster_id=default_cluster_id).count() == 0


def test_evidence_returns_only_two_recent_samples_and_labels_staleness(dashboard_client, default_cluster_id):
    now = datetime(2026, 9, 23, 12)
    with db.SessionLocal() as session:
        session.add_all(RbdCapacitySample(
            cluster_id=default_cluster_id, pool="volumes", captured_at=now - timedelta(hours=age),
            image_count=1, snapshot_count=age, provisioned_bytes=1000 + age,
            head_used_bytes=100, snapshot_used_bytes=age, physical_pool_used_bytes=500,
        ) for age in (3, 4, 5))
        session.commit()

    report = capacity_rbd.evidence(default_cluster_id, now=now)

    assert report["status"] == "insufficient_evidence"
    assert report["gaps"] == ["volumes: RBD capacity evidence is stale"]
    assert report["pools"][0]["snapshot_count"] == 3
    assert report["pools"][0]["observed_growth"]["snapshot_count_per_day"] == -24
