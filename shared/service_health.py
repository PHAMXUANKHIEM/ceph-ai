"""Process liveness files shared by systemd services and the health API."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def runtime_dir() -> Path:
    return Path(os.environ.get("CEPH_AI_RUNTIME_DIR", "/tmp/ceph-ai"))


def _containerized() -> bool:
    return os.environ.get("CEPH_AI_CONTAINERIZED", "").lower() == "true"


def _pid_namespace() -> str | None:
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def record(service: str) -> None:
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{service}.json"
    temporary = directory / f".{service}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps({
        "service": service,
        "pid": os.getpid(),
        "pid_namespace": _pid_namespace(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")
    os.replace(temporary, target)


def record_safe(service: str) -> bool:
    """Record a heartbeat without taking down the monitored service."""
    try:
        record(service)
        return True
    except OSError:
        logger.warning("Unable to write %s service heartbeat", service, exc_info=True)
        return False


def status(service: str, *, stale_after_seconds: int = 60) -> dict:
    target = runtime_dir() / f"{service}.json"
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(str(payload["updated_at"]))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - updated).total_seconds())
        pid = int(payload["pid"])
        recorded_pid_namespace = payload.get("pid_namespace")
        current_pid_namespace = _pid_namespace()
        same_pid_namespace = (
            (recorded_pid_namespace is None and not _containerized())
            or current_pid_namespace is None
            or recorded_pid_namespace == current_pid_namespace
        )
        pid_alive = Path(f"/proc/{pid}").exists() if same_pid_namespace else None
        pid_ok = pid_alive is not False
        return {
            "healthy": pid_ok and age <= stale_after_seconds,
            "pid": pid,
            "pid_alive": pid_alive,
            "pid_namespace": recorded_pid_namespace,
            "age_seconds": round(age, 1),
            "updated_at": updated.isoformat(),
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {
            "healthy": False,
            "pid": None,
            "pid_alive": None,
            "pid_namespace": None,
            "age_seconds": None,
            "updated_at": None,
        }
