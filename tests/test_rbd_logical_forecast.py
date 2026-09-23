from datetime import datetime, timedelta
from pathlib import Path

from shared import db
from shared.models import RbdCapacitySample
from watcher.rbd_logical_forecast import fit_logical_series, logical_forecasts


def _sample(cluster_id, pool, at, provisioned, *, head=100, snapshot=10):
    return RbdCapacitySample(
        cluster_id=cluster_id, pool=pool, captured_at=at, image_count=1,
        snapshot_count=1, provisioned_bytes=provisioned,
        head_used_bytes=head, snapshot_used_bytes=snapshot,
        physical_pool_used_bytes=500,
    )


def test_logical_forecast_uses_real_history_without_affecting_physical_capacity(
    dashboard_client, default_cluster_id,
):
    now = datetime(2026, 9, 23, 12)
    with db.SessionLocal() as session:
        session.add_all(_sample(default_cluster_id, "volumes", now - timedelta(days=8 - index),
                                1000 + 100 * index, head=200 + 20 * index)
                        for index in range(9))
        session.commit()

    report = logical_forecasts(default_cluster_id, now=now)
    assert report["execution_mode"] == "ADVISORY"
    provisioned = report["pools"][0]["series"][0]
    assert provisioned["status"] == "ADVISORY"
    assert provisioned["growth_bytes_per_day"] == 100
    assert provisioned["prediction_bytes"] == 2500
    assert provisioned["prediction_low_bytes"] <= 2500 <= provisioned["prediction_high_bytes"]
    assert len(provisioned["history"]) == 9
    assert "physical" in provisioned["reason"]

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/capacity-forecast/rbd-logical", params={"cluster_id": default_cluster_id})
    assert response.status_code == 200
    assert response.json()["cluster_id"] == default_cluster_id


def test_logical_forecast_rejects_missing_and_stale_usage(default_cluster_id):
    now = datetime(2026, 9, 23, 12)
    missing = [_sample(default_cluster_id, "volumes", now - timedelta(days=8 - index),
                       1000 + index * 100, snapshot=None) for index in range(9)]
    assert fit_logical_series(missing, "snapshot_used_bytes", now=now)["status"] == "INSUFFICIENT_EVIDENCE"
    stale = [_sample(default_cluster_id, "volumes", now - timedelta(days=18 - index),
                     1000 + index * 100) for index in range(9)]
    assert fit_logical_series(stale, "provisioned_bytes", now=now)["status"] == "STALE"


def test_logical_forecasts_do_not_cross_cluster(default_cluster_id, db_session):
    from shared.models import Cluster

    other = Cluster(name="rbd-logical-other", ceph_mon_nodes="10.0.0.2",
                    ssh_user="root", ssh_key_path="/tmp/key")
    db_session.add(other)
    db_session.flush()
    now = datetime(2026, 9, 23, 12)
    db_session.add(_sample(other.id, "private-pool", now, 2000))
    db_session.commit()
    report = logical_forecasts(default_cluster_id, now=now)
    assert all(item["pool"] != "private-pool" for item in report["pools"])


def test_capacity_page_has_read_only_logical_chart(dashboard_client):
    source = (Path(__file__).resolve().parents[1] / "dashboard/templates/capacity_forecast.html").read_text()
    assert 'id="rbd-logical-chart"' in source
    assert "/static/rbd_logical_forecast.js" in source
    assert "Không dùng biểu đồ này để suy ra dung lượng raw" in source
