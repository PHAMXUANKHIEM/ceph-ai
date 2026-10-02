"""Open BLUESTORE_SLOW_OP_ALERT Incidents on trend, not on presence (WP1.2).

Ceph keeps BLUESTORE_SLOW_OP_ALERT for ``bluestore_slow_ops_warn_lifetime``
(86400 s by default) after a single slow op (threshold 1), so the bare check
says little: one isolated op holds it for a day, and it produced 1,040
Incidents in 30 days before the in-flight unique index. Every poll that sees
the check stores one sample per sample interval; an Incident opens only when

* ``same_host``: two or more affected OSDs run on one host (host/disk fault),
* ``spike``: more OSDs are affected than the P95 of the baseline window times
  ``bluestore_slow_op_spike_factor`` (and at least two),
* ``persisted``: the episode outlived ``bluestore_slow_op_open_after_seconds``,
  longer than one warning lifetime, i.e. slow ops keep recurring.

Otherwise the samples are the record. Any failure here opens the Incident,
exactly as before this gate existed: the gate may only remove noise, never
hide a fault.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from config.settings import settings
from shared import db
from shared.models import BluestoreSlowOpSample
from shared.time import utc_now
from watcher.osd_hosts import osd_ids_in_detail, resolve_osd_hosts

logger = logging.getLogger(__name__)

BLUESTORE_SLOW_OP_CODE = "BLUESTORE_SLOW_OP_ALERT"
REASON_SAME_HOST = "same_host"
REASON_SPIKE = "spike"
REASON_PERSISTED = "persisted"
REASON_GATE_ERROR = "gate_error"
# A gap longer than this many sample intervals ends an episode.
_EPISODE_GAP_INTERVALS = 3
_BASELINE_CACHE_SECONDS = 3600

HostResolver = Callable[..., dict[int, str]]


@dataclass(frozen=True)
class SlowOpDecision:
    open: bool
    reasons: tuple[str, ...]
    osd_ids: tuple[int, ...]
    osd_hosts: dict[int, str]
    episode_started_at: datetime
    episode_seconds: int
    baseline_p95: float | None

    def evidence(self) -> dict[str, Any]:
        """Signal evidence; ``osd_id``/``host`` feed the WP3 runbook context."""
        payload: dict[str, Any] = {
            "bluestore_slow_ops": {
                "open_reasons": list(self.reasons),
                "osd_ids": list(self.osd_ids),
                "osd_hosts": {str(osd): host for osd, host in sorted(self.osd_hosts.items())},
                "episode_started_at": self.episode_started_at.isoformat(),
                "episode_seconds": self.episode_seconds,
                "baseline_p95_osds": self.baseline_p95,
            }
        }
        if self.osd_ids:
            first = self.osd_ids[0]
            payload["osd_id"] = str(first)
            if first in self.osd_hosts:
                payload["host"] = self.osd_hosts[first]
        return payload


@dataclass
class _ClusterState:
    episode_started_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_sample_at: datetime | None = None
    last_sample_ids: tuple[int, ...] = ()
    baseline_p95: float | None = None
    baseline_computed_at: float = 0.0
    host_cache: dict[int, str] = field(default_factory=dict)
    host_cache_expires_at: float = 0.0
    loaded: bool = False


_states: dict[str, _ClusterState] = {}
_lock = threading.Lock()


def _key(cluster_id: str | None) -> str:
    return cluster_id or ""


def reset_state() -> None:
    """Forget in-memory episodes (tests and process restart semantics)."""
    with _lock:
        _states.clear()


def _gap_limit() -> timedelta:
    return timedelta(seconds=settings.bluestore_slow_op_sample_interval_seconds * _EPISODE_GAP_INTERVALS)


def _cluster_filter(query, cluster_id: str | None):
    if cluster_id is None:
        return query.filter(BluestoreSlowOpSample.cluster_id.is_(None))
    return query.filter(BluestoreSlowOpSample.cluster_id == cluster_id)


def _load_from_db(session, state: _ClusterState, cluster_id: str | None, now: datetime) -> None:
    """Resume an episode a previous process was already tracking."""
    horizon = now - timedelta(seconds=settings.bluestore_slow_op_open_after_seconds) - _gap_limit() * 2
    rows = (
        _cluster_filter(session.query(BluestoreSlowOpSample), cluster_id)
        .filter(BluestoreSlowOpSample.sampled_at >= horizon)
        .order_by(BluestoreSlowOpSample.sampled_at.desc())
        .all()
    )
    state.loaded = True
    if not rows or now - rows[0].sampled_at > _gap_limit():
        return
    start = rows[0].sampled_at
    for newer, older in zip(rows, rows[1:]):
        if newer.sampled_at - older.sampled_at > _gap_limit():
            break
        start = older.sampled_at
    state.episode_started_at = start
    state.last_seen_at = rows[0].sampled_at
    state.last_sample_at = rows[0].sampled_at
    state.last_sample_ids = tuple(json.loads(rows[0].osd_ids_json))


def percentile_95(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return float(ordered[rank])


def _baseline(session, state: _ClusterState, cluster_id: str | None, now: datetime) -> float | None:
    if time.monotonic() - state.baseline_computed_at < _BASELINE_CACHE_SECONDS and state.baseline_computed_at:
        return state.baseline_p95
    episode_start = state.episode_started_at or now
    since = now - timedelta(days=settings.bluestore_slow_op_baseline_days)
    counts = [
        row.osd_count
        for row in _cluster_filter(session.query(BluestoreSlowOpSample.osd_count), cluster_id)
        .filter(BluestoreSlowOpSample.sampled_at >= since)
        .filter(BluestoreSlowOpSample.sampled_at < episode_start)
        .all()
    ]
    enough = len(counts) >= settings.bluestore_slow_op_min_baseline_samples
    state.baseline_p95 = percentile_95(counts) if enough else None
    state.baseline_computed_at = time.monotonic()
    return state.baseline_p95


def _hosts(state: _ClusterState, osd_ids: tuple[int, ...], cluster: Any, resolver: HostResolver) -> dict[int, str]:
    missing = {osd for osd in osd_ids if osd not in state.host_cache}
    if missing and (time.monotonic() >= state.host_cache_expires_at or not state.host_cache):
        try:
            state.host_cache.update(resolver(missing, cluster=cluster))
        except Exception:
            logger.warning("bluestore slow ops: OSD host lookup failed", exc_info=True)
        state.host_cache_expires_at = time.monotonic() + settings.bluestore_slow_op_host_cache_seconds
    return {osd: state.host_cache[osd] for osd in osd_ids if osd in state.host_cache}


def _reasons(osd_ids: tuple[int, ...], hosts: dict[int, str], episode_seconds: int,
             baseline_p95: float | None) -> tuple[str, ...]:
    reasons = []
    if any(count >= 2 for count in Counter(hosts.values()).values()):
        reasons.append(REASON_SAME_HOST)
    if (
        baseline_p95 is not None
        and len(osd_ids) >= 2
        and len(osd_ids) > baseline_p95 * settings.bluestore_slow_op_spike_factor
    ):
        reasons.append(REASON_SPIKE)
    if episode_seconds >= settings.bluestore_slow_op_open_after_seconds:
        reasons.append(REASON_PERSISTED)
    return tuple(reasons)


def observe(
    cluster_id: str | None,
    check_detail: dict,
    *,
    cluster: Any = None,
    now: datetime | None = None,
    resolver: HostResolver | None = None,
) -> SlowOpDecision:
    """Record this poll's observation and decide whether it deserves an Incident."""
    now = now or utc_now()
    osd_ids = tuple(sorted(osd_ids_in_detail(check_detail)))
    with _lock:
        state = _states.setdefault(_key(cluster_id), _ClusterState())
    with db.SessionLocal() as session:
        if not state.loaded:
            _load_from_db(session, state, cluster_id, now)
        if state.last_seen_at is None or now - state.last_seen_at > _gap_limit():
            state.episode_started_at = now
            state.baseline_computed_at = 0.0
        state.last_seen_at = now
        hosts = _hosts(state, osd_ids, cluster, resolver or resolve_osd_hosts)
        baseline_p95 = _baseline(session, state, cluster_id, now)
        interval = timedelta(seconds=settings.bluestore_slow_op_sample_interval_seconds)
        if (
            state.last_sample_at is None
            or now - state.last_sample_at >= interval
            or osd_ids != state.last_sample_ids
        ):
            session.add(BluestoreSlowOpSample(
                cluster_id=cluster_id,
                sampled_at=now,
                osd_count=len(osd_ids),
                osd_ids_json=json.dumps(list(osd_ids)),
                osd_hosts_json=json.dumps({str(osd): host for osd, host in sorted(hosts.items())}),
            ))
            session.commit()
            state.last_sample_at = now
            state.last_sample_ids = osd_ids
    episode_started_at = state.episode_started_at or now
    episode_seconds = int((now - episode_started_at).total_seconds())
    reasons = _reasons(osd_ids, hosts, episode_seconds, baseline_p95)
    return SlowOpDecision(
        open=bool(reasons),
        reasons=reasons,
        osd_ids=osd_ids,
        osd_hosts=hosts,
        episode_started_at=episode_started_at,
        episode_seconds=episode_seconds,
        baseline_p95=baseline_p95,
    )


def gate_incident(
    ceph_code: str, check_detail: dict, *, cluster_id: str | None, cluster: Any = None,
) -> tuple[bool, dict | None]:
    """(skip, evidence) for one health check in the Incident build loop.

    Non-BlueStore codes pass through untouched. Never raises: on failure the
    Incident opens, as it did before this gate.
    """
    if ceph_code != BLUESTORE_SLOW_OP_CODE:
        return False, None
    try:
        decision = observe(cluster_id, check_detail, cluster=cluster)
    except Exception:
        logger.exception("bluestore slow ops: gate failed; opening the Incident")
        return False, {"bluestore_slow_ops": {"open_reasons": [REASON_GATE_ERROR]}}
    if not decision.open:
        logger.info(
            "bluestore slow ops: %d OSD(s) for %ss, no open reason; sample only",
            len(decision.osd_ids), decision.episode_seconds,
        )
    return not decision.open, decision.evidence()


def merge_evidence(signal_evidence_json: str | None, extra: dict | None) -> str | None:
    """Add the gate's evidence to the Incident's existing signal evidence."""
    if not extra:
        return signal_evidence_json
    try:
        base = json.loads(signal_evidence_json) if signal_evidence_json else {}
    except (TypeError, ValueError):
        base = {}
    if not isinstance(base, dict):
        base = {"signal": base}
    for key, value in extra.items():
        base.setdefault(key, value)
    return json.dumps(base, ensure_ascii=False, sort_keys=True)
