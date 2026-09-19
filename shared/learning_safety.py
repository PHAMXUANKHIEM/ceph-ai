"""Small, process-safe guards for bounded learning jobs.

These helpers do not persist model state and do not make remediation
decisions. They only prevent duplicate work and stop a repeatedly failing
external dependency from consuming the whole Watcher loop.
"""

from __future__ import annotations

import time
from threading import Lock


class RateLimiter:
    """Allow one operation per key during a bounded interval."""

    def __init__(self) -> None:
        self._last_run: dict[str, float] = {}
        self._lock = Lock()

    def allow(
        self,
        key: str,
        *,
        interval_seconds: float,
        now: float | None = None,
    ) -> bool:
        interval = max(0.0, float(interval_seconds))
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            previous = self._last_run.get(key)
            if previous is not None and current - previous < interval:
                return False
            self._last_run[key] = current
            return True

    def clear(self) -> None:
        with self._lock:
            self._last_run.clear()


class CircuitBreaker:
    """Fail closed after consecutive dependency failures.

    A successful call resets the failure streak. After the cooldown expires,
    one probe is allowed; concurrent callers remain blocked until that probe
    succeeds or fails. This is intentionally in-memory because the guard is
    for one Watcher process and must never become a database dependency.
    """

    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 300) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = Lock()

    def allow(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            if self._opened_at is None:
                return True
            if current - self._opened_at < self.cooldown_seconds:
                return False
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else float(now)
        with self._lock:
            self._failures += 1
            self._probe_in_flight = False
            if self._failures >= self.failure_threshold:
                self._opened_at = current

    def reset(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None
