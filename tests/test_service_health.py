import json
from datetime import datetime, timedelta, timezone

from shared import service_health


def test_service_health_records_live_process(tmp_path, monkeypatch):
    monkeypatch.setenv("CEPH_AI_RUNTIME_DIR", str(tmp_path))
    service_health.record("worker")

    result = service_health.status("worker")

    assert result["healthy"] is True
    assert result["pid"] is not None


def test_service_health_rejects_stale_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CEPH_AI_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "watcher.json").write_text(json.dumps({
        "service": "watcher", "pid": 1,
        "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    }))

    assert service_health.status("watcher", stale_after_seconds=60)["healthy"] is False
