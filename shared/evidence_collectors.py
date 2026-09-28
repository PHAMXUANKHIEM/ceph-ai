"""Read-only evidence collectors (autonomy plan WP3.1).

Before a diagnosis, the system should look at the cluster the way an
operator would: ``ceph osd perf`` for a latency alert, a ping from a MON
for an unreachable host.  Each collector here is a typed, fixed command
(no free-form strings), with validated parameters, a timeout, an output
cap and redaction.  Every rendered command is checked against a list of
mutating verbs before it can reach a transport, so a collector cannot
change the cluster even if a template were edited carelessly.

Runs are bounded: at most one run per incident every ``INCIDENT_COOLDOWN``,
``RUN_BUDGET_SECONDS`` per run, ``HOST_CONCURRENCY`` collectors at a time
per host, and a per-host circuit breaker after repeated failures.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol

from shared.ai_redaction import redact_text
from shared.time import utc_now

CEPH, HOST = "ceph", "host"
OK, ERROR, TIMEOUT, REFUSED = "ok", "error", "timeout", "refused"
SKIPPED_BUDGET, SKIPPED_BUSY, SKIPPED_BREAKER = "skipped_budget", "skipped_busy", "skipped_breaker"

RUN_BUDGET_SECONDS = 60.0
HOST_CONCURRENCY = 2
INCIDENT_COOLDOWN = timedelta(minutes=10)
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_SECONDS = 300.0

# Same vocabulary as scripts/live_readonly_acceptance.py plus the shell and
# daemon verbs a host command could use.
MUTATING_VERBS = frozenset({
    "create", "rm", "remove", "delete", "purge", "set", "unset", "reweight", "out", "in", "mv",
    "rename", "resize", "flatten", "rollback", "enable", "disable", "restart", "stop", "start",
    "kill", "mark", "repair", "scrub", "deep-scrub", "import", "export", "map", "unmap", "lock",
    "evict", "trash", "protect", "unprotect", "add", "clear", "reset", "apply", "upgrade",
    "injectargs", "config-key", "reboot", "shutdown", "halt", "poweroff", "zap", "mkfs", "dd",
    "wipefs", "tee", "sudo", "su", "chmod", "chown", "systemctl", "down", "destroy", "drain",
})
_SHELL_METACHARACTERS = (";", "|", "&", ">", "<", "`", "$(", "\n", "\\")
PARAMETER_PATTERNS: dict[str, re.Pattern[str]] = {
    "osd_id": re.compile(r"^\d{1,6}$"),
    "devid": re.compile(r"^[A-Za-z0-9_.:-]{1,128}$"),
    "target": re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)$"),
}


class CollectorRefused(ValueError):
    """A rendered command or a parameter failed the read-only checks."""


@dataclass(frozen=True)
class Collector:
    id: str
    description: str
    kind: str                 # CEPH: run through a MON; HOST: run on the named host over SSH
    template: str
    params: tuple[str, ...] = ()
    timeout_seconds: int = 20
    max_chars: int = 6000
    json_output: bool = False


COLLECTORS: dict[str, Collector] = {c.id: c for c in (
    Collector("ceph_health_detail", "Chi tiết health check đang bật", CEPH, "ceph health detail", json_output=True),
    Collector("ceph_osd_tree", "Cây OSD: host, trạng thái up/down, in/out", CEPH, "ceph osd tree", json_output=True),
    Collector("ceph_osd_perf", "Commit/apply latency từng OSD", CEPH, "ceph osd perf", json_output=True),
    Collector("ceph_osd_df", "Dung lượng và số PG từng OSD", CEPH, "ceph osd df", json_output=True),
    Collector("ceph_pg_dump_stuck", "PG bị kẹt (inactive/unclean/stale)", CEPH, "ceph pg dump_stuck", json_output=True),
    Collector("ceph_device_ls", "Thiết bị và daemon dùng chúng", CEPH, "ceph device ls", json_output=True),
    Collector("ceph_crash_ls_new", "Crash chưa được archive", CEPH, "ceph crash ls-new", json_output=True),
    Collector("ceph_time_sync", "Đồng bộ giờ giữa các MON", CEPH, "ceph time-sync-status", json_output=True),
    Collector("ceph_osd_metadata", "Metadata của một OSD (host, thiết bị, phiên bản)", CEPH,
              "ceph osd metadata {osd_id}", ("osd_id",), json_output=True),
    Collector("ceph_osd_slow_ops", "Slow op gần đây của một OSD", CEPH,
              "ceph tell osd.{osd_id} dump_historic_slow_ops", ("osd_id",), timeout_seconds=15, json_output=True),
    Collector("ceph_device_health", "Chỉ số SMART đã lưu của một thiết bị", CEPH,
              "ceph device get-health-metrics {devid}", ("devid",), json_output=True),
    Collector("host_uptime", "Uptime và load của host", HOST, "uptime", timeout_seconds=10, max_chars=500),
    Collector("host_memory", "Bộ nhớ của host", HOST, "free -m", timeout_seconds=10, max_chars=1000),
    Collector("host_links", "Bộ đếm lỗi card mạng", HOST, "ip -s link", timeout_seconds=10),
    Collector("mon_ping", "Ping từ MON tới host đích", HOST, "ping -c 3 -W 1 {target}", ("target",),
              timeout_seconds=10, max_chars=1500),
)}


def assert_read_only(command: str) -> None:
    if any(token in command for token in _SHELL_METACHARACTERS):
        raise CollectorRefused(f"shell metacharacter in {command!r}")
    words = set(command.lower().split())
    blocked = words & MUTATING_VERBS
    if blocked:
        raise CollectorRefused(f"mutating verb {sorted(blocked)} in {command!r}")


def render(collector_id: str, params: dict[str, Any] | None = None) -> tuple[Collector, str]:
    collector = COLLECTORS.get(collector_id)
    if collector is None:
        raise CollectorRefused(f"unknown collector {collector_id!r}")
    params = params or {}
    values = {}
    for name in collector.params:
        value = str(params.get(name, ""))
        if not PARAMETER_PATTERNS[name].match(value):
            raise CollectorRefused(f"invalid {name} for {collector_id}: {value!r}")
        values[name] = value
    command = collector.template.format(**values)
    assert_read_only(command)
    return collector, command


class Transport(Protocol):
    def ceph(self, command: str, timeout: int) -> Any: ...

    def host(self, host: str, command: str, timeout: int) -> str: ...


@dataclass
class EvidenceRequest:
    collector_id: str
    host: str | None = None          # HOST collectors: where to run; CEPH: None (any MON)
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidenceResult:
    collector_id: str
    target: str
    status: str
    command: str = ""
    output: str = ""
    duration_ms: int = 0
    truncated: bool = False

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _format_output(collector: Collector, raw: Any) -> tuple[str, bool]:
    text = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True, default=str)
    text = redact_text(text)
    if len(text) > collector.max_chars:
        return text[:collector.max_chars], True
    return text, False


class CircuitBreaker:
    def __init__(self, failures: int = BREAKER_FAILURES, cooldown: float = BREAKER_COOLDOWN_SECONDS,
                 clock: Callable[[], float] = time.monotonic):
        self.failures, self.cooldown, self.clock = failures, cooldown, clock
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def is_open(self, key: str) -> bool:
        with self._lock:
            count, opened_at = self._state.get(key, (0, 0.0))
            if count < self.failures:
                return False
            if self.clock() - opened_at >= self.cooldown:
                self._state[key] = (self.failures - 1, 0.0)   # half-open: one more failure re-opens
                return False
            return True

    def record(self, key: str, success: bool) -> None:
        with self._lock:
            if success:
                self._state.pop(key, None)
                return
            count, _ = self._state.get(key, (0, 0.0))
            self._state[key] = (count + 1, self.clock())


class EvidenceRunner:
    """Runs evidence requests for an incident within the WP3.1 limits."""

    def __init__(self, transport: Transport, *, budget_seconds: float = RUN_BUDGET_SECONDS,
                 host_concurrency: int = HOST_CONCURRENCY, cooldown: timedelta = INCIDENT_COOLDOWN,
                 breaker: CircuitBreaker | None = None, clock: Callable[[], float] = time.monotonic):
        self.transport = transport
        self.budget_seconds, self.host_concurrency, self.cooldown = budget_seconds, host_concurrency, cooldown
        self.breaker = breaker or CircuitBreaker(clock=clock)
        self.clock = clock
        self._host_slots: dict[str, threading.BoundedSemaphore] = {}
        self._last_run: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def _slot(self, key: str) -> threading.BoundedSemaphore:
        with self._lock:
            return self._host_slots.setdefault(key, threading.BoundedSemaphore(self.host_concurrency))

    def claim(self, incident_id: str, now: datetime | None = None) -> bool:
        """At most one run per incident per cooldown window."""
        now = now or utc_now()
        with self._lock:
            last = self._last_run.get(incident_id)
            if last is not None and now - last < self.cooldown:
                return False
            self._last_run[incident_id] = now
            return True

    def run(self, requests: list[EvidenceRequest]) -> list[EvidenceResult]:
        started = self.clock()
        return [self._run_one(request, started) for request in requests]

    def _run_one(self, request: EvidenceRequest, started: float) -> EvidenceResult:
        target = request.host or "mon"
        try:
            collector, command = render(request.collector_id, request.params)
        except CollectorRefused as exc:
            return EvidenceResult(request.collector_id, target, REFUSED, output=str(exc)[:300])
        if collector.kind == HOST and not request.host:
            return EvidenceResult(collector.id, target, REFUSED, command, "HOST collector needs a host")
        remaining = self.budget_seconds - (self.clock() - started)
        if remaining <= 0:
            return EvidenceResult(collector.id, target, SKIPPED_BUDGET, command)
        if self.breaker.is_open(target):
            return EvidenceResult(collector.id, target, SKIPPED_BREAKER, command)
        slot = self._slot(target)
        if not slot.acquire(timeout=1):
            return EvidenceResult(collector.id, target, SKIPPED_BUSY, command)
        # Hard deadline: wait at most the collector's own timeout and never
        # past the run budget. The call runs in a daemon thread that keeps
        # the host slot until the command really ends, so an abandoned slow
        # command still counts against HOST_CONCURRENCY.
        deadline = max(0.1, min(float(collector.timeout_seconds), remaining))
        box: dict[str, Any] = {}
        done = threading.Event()

        def call() -> None:
            try:
                box["result"] = self._execute(collector, command, target, request, int(deadline) or 1)
            finally:
                slot.release()
                done.set()

        begin = self.clock()
        threading.Thread(target=call, name=f"evidence-{collector.id}", daemon=True).start()
        if not done.wait(deadline):
            self.breaker.record(target, success=False)
            return EvidenceResult(collector.id, target, TIMEOUT, command,
                                  f"vượt deadline {deadline:.0f}s (timeout collector/ngân sách lượt)",
                                  int((self.clock() - begin) * 1000))
        return box["result"]

    def _execute(self, collector: Collector, command: str, target: str,
                 request: EvidenceRequest, timeout: int) -> EvidenceResult:
        begin = self.clock()
        try:
            if collector.kind == CEPH:
                raw = self.transport.ceph(command, timeout)
            else:
                raw = self.transport.host(str(request.host), command, timeout)
        except Exception as exc:  # noqa: BLE001 - every failure becomes evidence, never an exception
            self.breaker.record(target, success=False)
            status = TIMEOUT if "timed out" in str(exc).lower() or isinstance(exc, TimeoutError) else ERROR
            return EvidenceResult(collector.id, target, status, command, redact_text(str(exc))[:500],
                                  int((self.clock() - begin) * 1000))
        self.breaker.record(target, success=True)
        output, truncated = _format_output(collector, raw)
        return EvidenceResult(collector.id, target, OK, command, output,
                              int((self.clock() - begin) * 1000), truncated)


class SshTransport:
    """Production transport over the cluster's read-only SSH identity.
    ``cluster=None`` means the default cluster configured in ``settings``."""

    def __init__(self, cluster=None):
        from config.settings import settings
        from shared.cluster_nodes import resolve_ssh_creds

        mon_nodes = cluster.ceph_mon_nodes if cluster is not None else settings.ceph_mon_nodes
        self.mon_nodes = [node.strip() for node in str(mon_nodes or "").split(",") if node.strip()]
        self.ssh_user, self.ssh_key_path, self.exec_mode, self.container_name = resolve_ssh_creds(cluster)

    def ceph(self, command: str, timeout: int) -> Any:
        from watcher.ceph_client import run_ceph_json_command_with

        assert_read_only(command)
        return run_ceph_json_command_with(
            self.mon_nodes, self.container_name, self.ssh_user, self.ssh_key_path, self.exec_mode, command,
        )[1]

    def host(self, host: str, command: str, timeout: int) -> str:
        from watcher.ceph_client import _run_remote_command_with

        assert_read_only(command)
        return _run_remote_command_with(host, command, self.ssh_user, self.ssh_key_path, timeout)
