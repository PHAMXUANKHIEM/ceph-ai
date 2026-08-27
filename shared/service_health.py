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


def record(service: str) -> None:
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{service}.json"
    temporary = directory / f".{service}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps({
        "service": service,
        "pid": os.getpid(),
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
        alive = Path(f"/proc/{pid}").exists()
        return {"healthy": alive and age <= stale_after_seconds, "pid": pid,
                "age_seconds": round(age, 1), "updated_at": updated.isoformat()}
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {"healthy": False, "pid": None, "age_seconds": None, "updated_at": None}
