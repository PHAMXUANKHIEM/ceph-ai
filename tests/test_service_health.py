import json
from datetime import datetime, timedelta, timezone

from shared import service_health


def test_service_health_records_live_process(tmp_path, monkeypatch):
    monkeypatch.setenv("CEPH_AI_RUNTIME_DIR", str(tmp_path))
    service_health.record("worker")

    result = service_health.status("worker")

    assert result["healthy"] is True
    assert result["pid"] is not None
    assert result["pid_alive"] is True


def test_service_health_rejects_stale_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CEPH_AI_RUNTIME_DIR", str(tmp_path))
    (tmp_path / "watcher.json").write_text(json.dumps({
        "service": "watcher", "pid": 1,
        "updated_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    }))

    assert service_health.status("watcher", stale_after_seconds=60)["healthy"] is False


def test_service_health_accepts_fresh_heartbeat_from_other_pid_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("CEPH_AI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(service_health, "_pid_namespace", lambda: "pid:[current]")
    (tmp_path / "telegram-ai.json").write_text(json.dumps({
        "service": "telegram-ai",
        "pid": 999999,
        "pid_namespace": "pid:[other-container]",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))

    result = service_health.status("telegram-ai", stale_after_seconds=60)

    assert result["healthy"] is True
    assert result["pid_alive"] is None


def test_service_health_accepts_fresh_legacy_heartbeat_when_containerized(tmp_path, monkeypatch):
    monkeypatch.setenv("CEPH_AI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CEPH_AI_CONTAINERIZED", "true")
    monkeypatch.setattr(service_health, "_pid_namespace", lambda: "pid:[current]")
    (tmp_path / "telegram-ai.json").write_text(json.dumps({
        "service": "telegram-ai",
        "pid": 999999,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))

    result = service_health.status("telegram-ai", stale_after_seconds=60)

    assert result["healthy"] is True
    assert result["pid_alive"] is None


def test_service_health_rejects_dead_pid_in_same_pid_namespace(tmp_path, monkeypatch):
    monkeypatch.setenv("CEPH_AI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(service_health, "_pid_namespace", lambda: "pid:[same]")
    (tmp_path / "watcher.json").write_text(json.dumps({
        "service": "watcher",
        "pid": 999999,
        "pid_namespace": "pid:[same]",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }))

    result = service_health.status("watcher", stale_after_seconds=60)

    assert result["healthy"] is False
    assert result["pid_alive"] is False


def test_record_safe_does_not_crash_service(monkeypatch):
    monkeypatch.setattr(service_health, "record", lambda _service: (_ for _ in ()).throw(OSError("disk")))

    assert service_health.record_safe("watcher") is False
