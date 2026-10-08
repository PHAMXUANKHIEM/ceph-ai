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


class LivenessGuard:
    """Keep a service heartbeat fresh while its main loop keeps making progress.

    The Watchers recorded their heartbeat once per poll cycle, so a cycle
    slowed down by an unreachable cluster (every MON timing out) looked like
    a dead process: the container turned unhealthy and was restarted in the
    middle of the outage (CS-LAB, 08/10/2026). This thread writes the
    heartbeat every ``interval_seconds`` as long as the loop called
    ``progress()`` within ``stall_seconds``; a loop that is really stuck
    stops the heartbeat, so the container is still reported unhealthy.
    """

    def __init__(self, service: str, *, stall_seconds: float = 300.0, interval_seconds: float = 10.0,
                 clock=None) -> None:
        import threading
        import time

        self.service = service
        self.stall_seconds = stall_seconds
        self.interval_seconds = interval_seconds
        self._clock = clock or time.monotonic
        self._last_progress = self._clock()
        self._stop = threading.Event()
        self._thread: object | None = None
        self._stalled_logged = False

    def progress(self) -> None:
        self._last_progress = self._clock()
        self._stalled_logged = False

    def beat_once(self) -> bool:
        """Record the heartbeat if the loop is not stalled; True when written."""
        stalled_for = self._clock() - self._last_progress
        if stalled_for > self.stall_seconds:
            if not self._stalled_logged:
                logger.error("%s main loop made no progress for %.0f s; heartbeat stopped", self.service, stalled_for)
                self._stalled_logged = True
            return False
        return record_safe(self.service)

    def start(self) -> "LivenessGuard":
        import threading

        def run() -> None:
            while not self._stop.wait(self.interval_seconds):
                self.beat_once()

        self._thread = threading.Thread(target=run, name=f"{self.service}-liveness", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
