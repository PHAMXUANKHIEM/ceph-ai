"""The contract every log-source adapter implements (tầng T1).

`LogRecord` is deliberately the SMALLEST shape the analysis layer needs --
it is the boundary the plan (section 11) proposes between "đội RCA sở hữu
tầng lưu trữ log" and "ceph-aiops sở hữu tầng phân tích". Anything an
adapter can't determine is None rather than guessed; `log_intel.py` treats
a None timestamp as "fall back to collection time" and a None severity as
"unknown", never as a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

DAEMON_TYPES = ("mon", "mgr", "osd", "rgw")


class LogSourceError(Exception):
    """Raised when an adapter cannot fetch from a host/stream at all
    (SSH failure, Loki unreachable, adapter misconfigured).

    Per-host scope on purpose: `log_intel.py` catches this per host and
    records a PARTIAL run rather than aborting the whole scan, the same
    best-effort posture every other Watcher collector already has.
    """


@dataclass(frozen=True)
class LogRecord:
    """One log line, normalized across sources.

    `raw` is kept because fingerprinting runs on the message text but an
    operator-facing sample line reads better with the original prefix. It
    is truncated and redacted before anything is persisted (constraint
    R1/R6) -- this dataclass itself is in-memory only and never stored.
    """

    ts: datetime | None
    host: str
    daemon_type: str
    message: str
    raw: str
    severity: int | None = None


@dataclass
class LogSourceResult:
    """What one adapter call returns for one (host, daemon_type) pair.

    `error` being set does NOT mean zero records -- a partially readable
    stream returns what it got plus the reason the rest is missing, which
    is exactly the input `LogIngestStatus.PARTIAL` exists to record.
    """

    records: list[LogRecord]
    error: str | None = None
