"""Skip the expensive health read while the mgr's metrics show nothing changed.

Every Watcher tick read `ceph health detail` through `cephadm shell`: a
throw-away container on a MON, 5-25 s, competing for the per-MON cephadm
lock (09/10/2026: ~9 health reads a minute). The mgr prometheus module
serves the same health picture in ~7 ms: `ceph_health_status` and one
`ceph_health_detail{name=...}` per check (1 = active; checks seen earlier
stay listed with 0; muted checks are 1).

Modes (CEPH_HEALTH_MGR_GATE):

* ``off``: always read health the old way;
* ``shadow`` (default): read it the old way, and count how often the gate
  would have skipped and whether the mgr's active checks matched;
* ``on``: reuse the last full health while the mgr's (status, active
  checks) fingerprint is unchanged and that full read is younger than
  CEPH_HEALTH_MGR_GATE_MAX_AGE_SECONDS.

Any doubt means a full read: the mgr URL cannot be found, the fetch fails
or times out, a standby answers with an empty page (it returns 200 and no
body), or ``ceph_health_status`` is missing.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass

from config.settings import settings

logger = logging.getLogger(__name__)

MGR_SERVICES_TTL_SECONDS = 600
FETCH_TIMEOUT_SECONDS = 3
DISAGREEMENT_LOG_SECONDS = 3600
_DETAIL_RE = re.compile(r'^ceph_health_detail\{[^}]*name="([A-Z0-9_]+)"[^}]*\}\s+([0-9.eE+-]+)')
_STATUS_RE = re.compile(r"^ceph_health_status\s+([0-9.eE+-]+)")


@dataclass(frozen=True)
class MgrHealth:
    status: int
    checks: frozenset[str]

    @property
    def fingerprint(self) -> tuple[int, frozenset[str]]:
        return self.status, self.checks


def parse_health(text: str) -> MgrHealth | None:
    """Status and active check names from the mgr's metrics page; None if it is not a real page."""
    status = None
    checks = set()
    for line in text.splitlines():
        match = _STATUS_RE.match(line)
        if match:
            status = int(float(match.group(1)))
            continue
        match = _DETAIL_RE.match(line)
        if match and float(match.group(2)) > 0:
            checks.add(match.group(1))
    return None if status is None else MgrHealth(status, frozenset(checks))


class _Endpoint:
    """The active mgr's prometheus URL from `ceph mgr services`, re-read every 10 min or after a failure."""

    def __init__(self, discover):
        self._discover = discover
        self._url: str | None = None
        self._read_at = 0.0
        self._lock = threading.Lock()

    def url(self) -> str | None:
        with self._lock:
            if self._url is None or time.monotonic() - self._read_at > MGR_SERVICES_TTL_SECONDS:
                self._url = self._discover()
                self._read_at = time.monotonic()
            return self._url

    def forget(self) -> None:
        with self._lock:
            self._url = None


def _discover_from_mgr_services() -> str | None:
    from watcher import ceph_client

    try:
        _host, payload = ceph_client.run_ceph_json_command("ceph mgr services")
    except Exception as exc:
        logger.info("mgr health gate: ceph mgr services unavailable: %s", exc)
        return None
    url = payload.get("prometheus") if isinstance(payload, dict) else None
    return url.rstrip("/") + "/metrics" if isinstance(url, str) and url.startswith("http") else None


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT_SECONDS) as response:  # nosec B310 - http URL from Ceph
        return response.read().decode("utf-8", errors="replace")


class MgrHealthGate:
    def __init__(self, *, discover=_discover_from_mgr_services, fetch=_fetch, clock=time.monotonic):
        self._endpoint = _Endpoint(discover)
        self._fetch = fetch
        self._clock = clock
        self._last_health: dict | None = None
        self._last_fingerprint: tuple | None = None
        self._last_full_at = 0.0
        self._last_disagreement_log = -DISAGREEMENT_LOG_SECONDS
        self._last_summary_log = clock()
        self.metrics = {"full_reads": 0, "skipped": 0, "would_skip": 0, "mgr_unavailable": 0,
                        "agree": 0, "disagree": 0}

    def mgr_health(self) -> MgrHealth | None:
        url = self._endpoint.url()
        if url is None:
            self.metrics["mgr_unavailable"] += 1
            return None
        try:
            parsed = parse_health(self._fetch(url))
        except Exception as exc:
            logger.info("mgr health gate: %s unavailable: %s", url, exc)
            parsed = None
        if parsed is None:  # failure, or a standby's empty page: look the active mgr up again next time
            self._endpoint.forget()
            self.metrics["mgr_unavailable"] += 1
        return parsed

    def read(self, full_read) -> dict:
        """The health dict for this tick: reused when the gate is on and nothing moved, else ``full_read()``."""
        mode = str(settings.ceph_health_mgr_gate or "off").lower()
        if mode not in ("shadow", "on"):
            return full_read()
        mgr = self.mgr_health()
        fresh = self._clock() - self._last_full_at < float(settings.ceph_health_mgr_gate_max_age_seconds)
        last = self._last_health
        unchanged = mgr is not None and last is not None and mgr.fingerprint == self._last_fingerprint
        if unchanged and fresh and last is not None:
            if mode == "on":
                self.metrics["skipped"] += 1
                return dict(last)
            self.metrics["would_skip"] += 1
        health = full_read()
        self.metrics["full_reads"] += 1
        self._log_summary(mode)
        if mgr is not None:
            self._compare(mgr, health)
        self._last_health = dict(health)
        self._last_fingerprint = mgr.fingerprint if mgr is not None else None
        self._last_full_at = self._clock()
        return health

    def _log_summary(self, mode: str) -> None:
        if self._clock() - self._last_summary_log >= DISAGREEMENT_LOG_SECONDS:
            self._last_summary_log = self._clock()
            logger.info("mgr health gate (%s): %s", mode, self.metrics)

    def _compare(self, mgr: MgrHealth, health: dict) -> None:
        """Shadow evidence: do the mgr's active checks match `ceph health detail`?"""
        full = frozenset((health.get("checks") or {}).keys())
        if full == mgr.checks:
            self.metrics["agree"] += 1
            return
        self.metrics["disagree"] += 1
        if self._clock() - self._last_disagreement_log >= DISAGREEMENT_LOG_SECONDS:
            self._last_disagreement_log = self._clock()
            logger.warning("mgr health gate: mgr checks %s differ from health detail %s",
                           sorted(mgr.checks ^ full), sorted(full))


_GATE = MgrHealthGate()


def read_health(full_read) -> dict:
    return _GATE.read(full_read)


def get_metrics() -> dict:
    return dict(_GATE.metrics)
