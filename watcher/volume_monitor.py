"""Volume (RBD image) performance monitoring — detects when a VM's Volume
has effectively hit its real performance ceiling, so the operator finds out
from an Incident instead of a VM just quietly running slow.

2026-07-28, NOT verified against a real cluster yet (no RBD-backed pool
was ever created in this project's own lab cluster this session) — see
watcher/ceph_client.py::query_rbd_iostat's own docstring for what
specifically is unverified there. This module's own logic (rolling window,
saturation heuristic, Incident/Action creation) has been reviewed against
this codebase's existing patterns but not exercised against real `rbd perf
image iostat` output.

Deliberately entirely in-process/in-memory state (module-level `_state`
dict below), never persisted to the DB — same "no time-series storage in
this app" posture already established by watcher/node_metrics.py (CPU/RAM/
disk metrics are live-queried + client-side-rolling-windowed, never
written to a table). Watcher is already a long-lived single process with
its own poll loop (watcher/main.py::run()), so this in-memory window
survives across polls fine; it only resets on a Watcher restart, which
just means saturation detection needs a fresh few polls to warm back up —
an acceptable v1 trade-off, not a correctness bug.

Only the LATENCY-KNEE heuristic is implemented (current IOPS near the
volume's own recently-observed peak WHILE latency is elevated well above
its own recent baseline, sustained for several consecutive polls) — this
works whether or not the operator has ever configured an RBD QoS cap.
Comparing against a configured `rbd_qos_iops_limit`/`rbd_qos_bps_limit`
(simpler and more precise when a cap IS set) was designed but deliberately
NOT implemented in this pass — it would need one extra `rbd config image
get` SSH round trip per image per poll, unverified output shape, for a
signal genuinely useful only on the subset of deployments that bother
setting QoS caps. Left as a documented follow-up rather than adding that
much unverified surface area for a Volume-cap-only complement to the
single heuristic below, which already covers the general case.
"""

import json
import logging
from collections import deque
from datetime import datetime

from shared import audit, db
from shared.models import Action, ActionStatus, Incident, IncidentStatus, VolumeMetric
from watcher import ceph_client
from watcher.ceph_client import CephQueryError
from worker.policy import gate

logger = logging.getLogger(__name__)

VOLUME_SATURATED_PREFIX = "VOLUME_SATURATED:"

# Mirrors watcher/main.py::_RECOVERABLE_STATUSES (kept as its own copy
# rather than a cross-import to avoid a watcher.main <-> watcher.volume_monitor
# circular import — same "independent copy, not a cross-import" posture that
# module's own _CHAT_REQUEST_CEPH_CODE/_CLUSTER_UPGRADE_CEPH_CODE constants
# already document for the identical reason).
_RECOVERABLE_STATUSES = {
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    IncidentStatus.FAILED.value,
}

# --- Tuning knobs — internal constants, not config/settings.py fields
# (operational tuning, not per-deployment config an operator needs to set;
# same posture as worker/executor/cluster_deploy.py's
# _QUORUM_POLL_INTERVAL_SECONDS). Adjust here directly if these turn out
# wrong once verified against a real cluster's actual IOPS/latency shape.
ROLLING_WINDOW_SIZE = 12  # samples kept per volume — a few minutes of history at a 15s poll interval
CONSECUTIVE_POLLS_REQUIRED = 3  # must look saturated this many polls IN A ROW before an Incident fires — one noisy sample must never trigger one
NEAR_PEAK_RATIO = 0.9  # current IOPS >= this fraction of the window's own observed peak
LATENCY_SPIKE_MULTIPLIER = 2.0  # current latency >= this multiple of the window's own median (baseline) latency


def ceph_code_for(pool: str, image: str) -> str:
    return f"{VOLUME_SATURATED_PREFIX}{pool}/{image}"


class _VolumeState:
    """Per-(pool,image) rolling window + consecutive-saturated-polls streak
    counter. Never persisted — see this module's own docstring."""

    __slots__ = ("iops_window", "latency_window", "consecutive_saturated_polls")

    def __init__(self) -> None:
        self.iops_window: deque[float] = deque(maxlen=ROLLING_WINDOW_SIZE)
        self.latency_window: deque[float] = deque(maxlen=ROLLING_WINDOW_SIZE)
        self.consecutive_saturated_polls = 0


# Module-level, process-lifetime state — one entry per volume ever seen
# since this Watcher process started. Never cleared/evicted: a lab/small
# deployment's volume count is small and stable enough that this isn't a
# real unbounded-growth risk in practice, same judgment call already made
# for this codebase's other long-lived module-level state
# (watcher/ceph_client.py::last_successful_mon_node).
_state: dict[tuple[str, str], _VolumeState] = {}

# Overwritten (not appended to) on every check_volumes() call — this
# process's most recent poll's full sample set (every image from every
# pool, not just the ones that ended up in check_volumes()'s own returned
# saturated dict), read by persist_last_poll_metrics() below. Kept as its
# own module-level variable rather than folded into check_volumes()'s
# return value on purpose: check_volumes()'s existing large test suite
# exercises the saturation heuristic without wiring up a DB, and changing
# its return shape would force every one of those tests to also care about
# persistence. watcher/main.py's poll loop is the only real caller of
# either function, and always calls both, once per poll, in order.
_last_poll_samples: list[dict] = []


def _looks_saturated(state: _VolumeState, iops: float, latency_ms: float) -> bool:
    """True if THIS SINGLE sample looks saturated — the window must already
    be full (ROLLING_WINDOW_SIZE samples of history) before this can ever
    return True, so a volume this process just started watching gets a
    warm-up period rather than an immediate (baseline-less) judgment."""
    if len(state.iops_window) < ROLLING_WINDOW_SIZE:
        return False
    peak_iops = max(state.iops_window)
    sorted_latencies = sorted(state.latency_window)
    baseline_latency_ms = sorted_latencies[len(sorted_latencies) // 2]  # median
    near_peak = peak_iops > 0 and iops >= NEAR_PEAK_RATIO * peak_iops
    latency_elevated = baseline_latency_ms > 0 and latency_ms >= LATENCY_SPIKE_MULTIPLIER * baseline_latency_ms
    return near_peak and latency_elevated


def check_volumes() -> dict[str, dict]:
    """Polls every settings.ceph_rbd_pools-configured pool's per-image
    iostat, updates each image's rolling window + streak counter, and
    returns {ceph_code: detail} for every volume whose streak has reached
    CONSECUTIVE_POLLS_REQUIRED on THIS poll. A pool that fails to query
    (e.g. transient SSH hiccup) is skipped for this poll, logged, and
    simply retried next poll — never raises, matching every other
    best-effort per-poll check in watcher/main.py's loop."""
    saturated: dict[str, dict] = {}
    _last_poll_samples.clear()
    for pool in ceph_client.configured_rbd_pools():
        try:
            samples = ceph_client.query_rbd_iostat(pool)
        except CephQueryError as exc:
            logger.warning("check_volumes: failed to query RBD pool %r: %s", pool, exc)
            continue

        for sample in samples:
            key = (sample["pool"], sample["image"])
            state = _state.setdefault(key, _VolumeState())
            latency_ms = max(sample["read_latency_ms"], sample["write_latency_ms"])

            if _looks_saturated(state, sample["iops"], latency_ms):
                state.consecutive_saturated_polls += 1
            else:
                state.consecutive_saturated_polls = 0

            state.iops_window.append(sample["iops"])
            state.latency_window.append(latency_ms)

            is_saturated_now = state.consecutive_saturated_polls >= CONSECUTIVE_POLLS_REQUIRED
            _last_poll_samples.append(
                {
                    "pool": sample["pool"],
                    "image": sample["image"],
                    "iops": sample["iops"],
                    "read_latency_ms": sample["read_latency_ms"],
                    "write_latency_ms": sample["write_latency_ms"],
                    "saturated": is_saturated_now,
                }
            )

            if is_saturated_now:
                saturated[ceph_code_for(*key)] = {
                    "pool": sample["pool"],
                    "image": sample["image"],
                    "iops": sample["iops"],
                    "latency_ms": latency_ms,
                    "consecutive_polls": state.consecutive_saturated_polls,
                }
    return saturated


def persist_last_poll_metrics() -> None:
    """Writes one VolumeMetric row per sample check_volumes() saw on its
    most recent call — the persisted, queryable history that this module's
    own in-memory rolling window (see this module's docstring) deliberately
    doesn't provide on its own. A no-op if check_volumes() found nothing to
    poll this cycle (no pools configured/discovered, or every pool query
    failed) — same "next poll just retries" posture as check_volumes()
    itself. Deliberately a separate call from check_volumes() (see
    _last_poll_samples' own comment for why) — watcher/main.py's poll loop
    calls both, every poll."""
    if not _last_poll_samples:
        return
    polled_at = datetime.utcnow()
    with db.SessionLocal() as session:
        session.add_all(
            VolumeMetric(
                pool=sample["pool"],
                image=sample["image"],
                iops=sample["iops"],
                read_latency_ms=sample["read_latency_ms"],
                write_latency_ms=sample["write_latency_ms"],
                saturated=sample["saturated"],
                polled_at=polled_at,
            )
            for sample in _last_poll_samples
        )
        session.commit()


def _rationale_for(detail: dict) -> str:
    return (
        f"Volume {detail['pool']}/{detail['image']} có dấu hiệu bão hoà hiệu năng: IOPS hiện "
        f"tại {detail['iops']:.0f}, gần đỉnh đã quan sát gần đây của chính volume này, trong khi "
        f"độ trễ {detail['latency_ms']:.1f}ms cao bất thường so với baseline — lặp lại "
        f"{detail['consecutive_polls']} lần kiểm tra liên tiếp. Đây là dấu hiệu volume đã chạm "
        f"giới hạn hiệu năng thực tế (không phải lỗi cấu hình có thể tự sửa) — cân nhắc tăng "
        f"OSD/pool, đặt QoS cap, hoặc di dời sang volume/pool khác."
    )


def create_or_resolve_volume_incidents(current_saturated: dict[str, dict]) -> None:
    """Creates a new Incident+Action (action_id=investigate_manually, RISKY)
    for every newly-saturated volume that doesn't already have one open, and
    resolves any open VOLUME_SATURATED: Incident whose volume is no longer
    in `current_saturated` — same role as watcher/main.py's
    `_resolve_recovered_incidents`, but scoped to this ceph_code family and
    driven by this module's own saturated-set instead of `ceph health
    detail`'s current_codes (that function has its own VOLUME_SATURATED:
    guard so it never touches these rows — a synthetic ceph_code can never
    appear in a real health-check list, so without that guard it would
    incorrectly "recover" every volume Incident on every single poll).

    No LLM diagnosis involved — same posture as
    dashboard/routes/upgrade.py's synthetic CLUSTER_UPGRADE Incident: the
    rationale here is already fully deterministic from the rolling-window
    numbers, so skipping the AI-diagnosis round trip both saves a call and
    avoids the risk of the LLM picking some unrelated action_id for a
    ceph_code family it has no specific guidance about.
    """
    with db.SessionLocal() as session:
        open_volume_incidents = (
            session.query(Incident)
            .filter(Incident.ceph_code.like(f"{VOLUME_SATURATED_PREFIX}%"))
            .filter(Incident.status.in_(_RECOVERABLE_STATUSES))
            .all()
        )
        open_codes = {incident.ceph_code for incident in open_volume_incidents}

        for incident in open_volume_incidents:
            if incident.ceph_code not in current_saturated:
                incident.status = IncidentStatus.RESOLVED.value

        for ceph_code, detail in current_saturated.items():
            if ceph_code in open_codes:
                continue  # already has an open Incident — don't duplicate

            rationale = _rationale_for(detail)
            incident = Incident(
                ceph_code=ceph_code,
                status=IncidentStatus.PENDING_APPROVAL.value,
                detected_at=datetime.utcnow(),
                log_excerpt=rationale,
            )
            session.add(incident)
            session.flush()  # assigns incident.id, needed by the Action FK below

            action = Action(
                incident_id=incident.id,
                action_id="investigate_manually",
                classification=gate.classify_action("investigate_manually").value,
                status=ActionStatus.PENDING_APPROVAL.value,
                rationale=rationale,
                # No specific host to target — investigate_manually has no
                # automated Command regardless (has_command() is False for
                # it), so target_nodes is informational only here.
                target_nodes=json.dumps([]),
                action_params=json.dumps({"pool": detail["pool"], "image": detail["image"]}),
            )
            session.add(action)
            session.flush()

            audit.record(
                session,
                incident_id=incident.id,
                action_id=action.id,
                event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
                actor=audit.ACTOR_SYSTEM,
            )
        session.commit()
