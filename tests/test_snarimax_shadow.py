from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from config.settings import settings
from watcher.snarimax_shadow import hourly_points, run_cluster_shadow


ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _row(at, cpu=10.0, ram=20.0, read=1.0, write=2.0):
    return SimpleNamespace(
        collected_at=at,
        cpu_percent=cpu,
        mem_percent=ram,
        disk_read_iops=read,
        disk_write_iops=write,
    )


def test_hourly_points_aggregates_and_excludes_partial_current_hour():
    rows = [
        _row(ORIGIN + timedelta(minutes=5), cpu=10),
        _row(ORIGIN + timedelta(minutes=35), cpu=20),
        _row(ORIGIN + timedelta(hours=1, minutes=5), cpu=30),
        _row(ORIGIN + timedelta(hours=2, minutes=5), cpu=40),
    ]

    points = hourly_points(rows, "cpu", now=ORIGIN + timedelta(hours=3, minutes=1))

    assert points == [
        (ORIGIN, 15.0),
        (ORIGIN + timedelta(hours=1), 30.0),
        (ORIGIN + timedelta(hours=2), 40.0),
    ]


def test_shadow_scan_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings, "snarimax_shadow_enabled", False)
    result = run_cluster_shadow("cluster-a", "lab")
    assert result == {"status": "DISABLED", "processed": 0, "reports": []}
