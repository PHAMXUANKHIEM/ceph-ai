"""SSH log-source adapter (tầng T1) -- the day-one path.

Reuses `watcher/ceph_log.py`'s already-proven fetch (cephadm daemon
discovery, journalctl vs `docker/podman logs` branching, host-key-pinned
SSH) rather than reimplementing any of it. The ONLY thing this adapter
needs that the Dashboard's own log panel didn't is a bigger window, which
is why `ceph_log._fetch` grew an optional `tail_lines` (its default, and
therefore every existing caller, is unchanged).

Why this exists at all when Loki is the chosen store (plan section 11.1):
it needs no new infrastructure, so the fingerprint corpus starts building
immediately instead of waiting on a log-store rollout -- and it stays the
correct fallback for any cluster not shipping into Loki.

Deliberately NO time filtering at the source: `tail -n` is a LINE window,
not a time window. `log_intel.py` filters the parsed records by the real
window afterward. Doing it here would mean a per-daemon `--since` flag
that journalctl supports but `docker logs`/`cephadm logs` express
differently -- three code paths for something one timestamp comparison
downstream already handles uniformly.
"""

from __future__ import annotations

from datetime import datetime

from config.settings import settings
from shared.models import Cluster
from watcher import ceph_log
from watcher.log_source.base import LogRecord, LogSourceResult

SOURCE_NAME = "ssh"

# A large window is the whole point here, but the SSH round trip still has
# to come back -- the Dashboard's 15s display timeout is far too short for
# thousands of lines through `cephadm logs`.
FETCH_TIMEOUT_SECONDS = 60


def fetch(
    host: str,
    daemon_type: str,
    window_start: datetime,
    window_end: datetime,
    cluster: Cluster | None = None,
) -> LogSourceResult:
    """Pull one (host, daemon_type) log slice.

    Never raises: an unreachable host / missing daemon comes back as an
    empty `LogSourceResult` carrying `error`, which `log_intel.py` turns
    into a PARTIAL run. One bad node must not lose the other nodes' data.
    """
    from watcher.log_intel import parse_log_lines  # local: avoids a cycle

    max_lines = max(1, settings.log_intel_max_lines_per_daemon)
    try:
        if cluster is None:
            raw = ceph_log.fetch_ceph_log_with(
                host, daemon_type, None,
                settings.ssh_user, settings.ssh_key_path, settings.ceph_exec_mode,
                settings.ceph_container_name, settings.ceph_osd_container_name,
                settings.ceph_rgw_container_name,
                tail_lines=max_lines, timeout=FETCH_TIMEOUT_SECONDS,
            )
        else:
            raw = ceph_log.fetch_ceph_log_with(
                host, daemon_type, None,
                cluster.ssh_user, cluster.ssh_key_path, cluster.ceph_exec_mode,
                cluster.ceph_container_name, cluster.ceph_osd_container_name,
                cluster.ceph_rgw_container_name,
                tail_lines=max_lines, timeout=FETCH_TIMEOUT_SECONDS,
            )
    except Exception as exc:  # ceph_log raises CephLogError, but SSH can surface others
        return LogSourceResult(records=[], error=f"{host}/{daemon_type}: {exc}")

    records = [
        record
        for record in parse_log_lines(raw, host=host, daemon_type=daemon_type)
        # A record with no parseable timestamp is KEPT (ts is None) -- see
        # log_intel.py::_in_window for why dropping it would silently lose
        # exactly the malformed/unusual lines RCA cares most about.
        if _in_window(record, window_start, window_end)
    ]
    return LogSourceResult(records=records)


def _in_window(record: LogRecord, window_start: datetime, window_end: datetime) -> bool:
    if record.ts is None:
        return True
    return window_start <= record.ts <= window_end
