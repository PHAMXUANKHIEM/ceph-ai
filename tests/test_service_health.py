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


# --- LivenessGuard (08/10/2026) ------------------------------------------------------------

def test_a_slow_but_progressing_loop_keeps_its_heartbeat(monkeypatch, tmp_path):
    monkeypatch.setenv("CEPH_AI_RUNTIME_DIR", str(tmp_path))
    now = [0.0]
    guard = service_health.LivenessGuard("watcher", stall_seconds=300, clock=lambda: now[0])

    now[0] = 120.0  # one poll cycle stuck on unreachable MONs for two minutes
    assert guard.beat_once() is True
    assert service_health.status("watcher", stale_after_seconds=45)["healthy"] is True


def test_a_loop_stuck_past_the_stall_limit_stops_the_heartbeat_until_it_moves(monkeypatch, tmp_path):
    monkeypatch.setenv("CEPH_AI_RUNTIME_DIR", str(tmp_path))
    now = [0.0]
    guard = service_health.LivenessGuard("watcher", stall_seconds=300, clock=lambda: now[0])

    now[0] = 301.0
    assert guard.beat_once() is False
    guard.progress()
    assert guard.beat_once() is True


def test_the_guard_thread_beats_and_stops(monkeypatch, tmp_path):
    import time

    monkeypatch.setenv("CEPH_AI_RUNTIME_DIR", str(tmp_path))
    guard = service_health.LivenessGuard("remediation-watcher", interval_seconds=0.05).start()
    try:
        deadline = time.monotonic() + 2
        while not (tmp_path / "remediation-watcher.json").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        guard.stop()
    assert (tmp_path / "remediation-watcher.json").exists()


def test_selfcheck_never_kills_the_containers_it_restarts():
    from pathlib import Path

    unit = (Path(__file__).resolve().parents[1] / "scripts/deploy/systemd/ceph-ai-selfcheck.service").read_text()
    assert "KillMode=process" in unit and "TimeoutStartSec=300" in unit
