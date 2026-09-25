from datetime import datetime, timedelta

import pytest

from shared.block_storage_qos import diff, validate_values, values_for_template
from watcher.block_storage_metric_health import aggregate_pool_metrics, evaluate_metric_alerts
from worker.executor.commands import get_command
from worker.executor.ssh_executor import ExecutorError


def test_qos_template_is_complete_and_diff_is_explicit():
    before = values_for_template("unlimited")
    after = values_for_template("throughput")
    assert after["rbd_qos_bps_limit"] > 0
    assert diff(before, after)["rbd_qos_bps_limit"] == {"before": 0, "after": 1_073_741_824}
    assert set(validate_values(after)) == set(before)


def test_qos_template_rejects_unknown_or_unbounded_values():
    with pytest.raises(ValueError):
        values_for_template("made-up")
    with pytest.raises(ValueError):
        validate_values({"rbd_qos_iops_limit": -1})


def test_copy_preserves_source_and_move_verifies_before_trash():
    copied = get_command("rbd_copy_volume", "mon-1", {
        "pool_name": "images", "image": "source", "dest_pool": "backup", "dest_image": "copy",
    })
    moved = get_command("rbd_move_volume", "mon-1", {
        "pool_name": "images", "image": "source", "dest_pool": "backup", "dest_image": "moved",
    })
    assert copied == "rbd cp images/source backup/copy && rbd info backup/copy --format json"
    assert "rbd trash mv images/source" in moved
    assert moved.index("rbd info backup/moved") < moved.index("rbd trash mv images/source")


def test_copy_rejects_same_source_and_destination():
    with pytest.raises(ExecutorError):
        get_command("rbd_copy_volume", "mon-1", {
            "pool_name": "images", "image": "source", "dest_pool": "images", "dest_image": "source",
        })


def test_metric_aggregation_exposes_top_consumer_stale_and_noisy_neighbor():
    now = datetime(2026, 9, 25, 10, 0, 0)
    result = aggregate_pool_metrics([
        {"pool": "images", "image": "busy", "iops": 90, "read_latency_ms": 1, "write_latency_ms": 2,
         "read_bytes_per_sec": 100, "queue_depth": 3, "polled_at": now},
        {"pool": "images", "image": "quiet", "iops": 10, "read_latency_ms": 1, "write_latency_ms": 1,
         "read_bytes_per_sec": 20, "polled_at": now},
        {"pool": "old", "image": "disk", "iops": 1, "read_latency_ms": 1,
         "polled_at": now - timedelta(minutes=10)},
    ], now=now, stale_after_seconds=120)
    images = next(item for item in result["pools"] if item["pool"] == "images")
    old = next(item for item in result["pools"] if item["pool"] == "old")
    assert images["top_consumers"][0]["image"] == "busy"
    assert images["noisy_neighbors"][0]["image"] == "busy"
    assert old["stale"] is True
    alerts = evaluate_metric_alerts(result, now=now)
    assert {item["kind"] for item in alerts} == {"BLOCK_STORAGE_METRIC_STALE", "BLOCK_STORAGE_NOISY_NEIGHBOR"}
