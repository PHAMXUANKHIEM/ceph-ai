import asyncio
import hashlib
import fcntl
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from shared.time import utc_now
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from watcher import (
    bluestore_omap_monitor,
    capability_inventory,
    capacity_forecast,
    capacity_evidence,
    ceph_client,
    cluster_snapshot_collector,
    collector,
    crush_distribution_monitor,
    crush_skew_monitor,
    crush_structure_monitor,
    database_capacity_monitor,
    device_health_monitor,
    log_intel,
    node_health_monitor,
    osd_latency_monitor,
    publisher,
    trash_capacity_monitor,
    verify,
    volume_monitor,
    host_metrics,
    learning_retention,
    performance_rca_monitor,
    rgw_alerting,
    snarimax_shadow,
    volume_topology,
    vitastor_monitor,
)
from watcher.bluestore_omap_monitor import BLUESTORE_OMAP_PREFIX
from watcher.ceph_client import CephQueryError, query_cluster_health, query_cluster_health_with
from watcher.crush_skew_monitor import CRUSH_SKEW_PG_PREFIX, CRUSH_SKEW_USE_PREFIX
from watcher.database_capacity_monitor import DATABASE_SIZE_HIGH_PREFIX
from watcher.device_health_monitor import DEVICE_HEALTH_EVACUATE_PREFIX
from watcher.node_health_monitor import NODE_RESOURCE_HIGH_PREFIX
from watcher.osd_latency_monitor import OSD_LATENCY_HIGH_PREFIX
from watcher.log_analysis import LOG_ANOMALY_PREFIX
from watcher.volume_monitor import VOLUME_SATURATED_PREFIX
from watcher.performance_rca import PERFORMANCE_RCA_PREFIX
from shared import alert_lifecycle, audit, db, heartbeat, incident_outbox, service_health, telegram_alerts, telegram_outbox
from shared.cluster_snapshot import read_priority_refresh
from shared.incident_actions import cancel_pending_actions, reconcile_terminal_incident_actions
from shared.clusters import get_default_cluster_id, list_active_clusters
from shared.logging_redaction import install_logging_redaction
from shared.models import Action, ActionStatus, AuditEntry, Cluster, Incident, IncidentStatus
from shared.synthetic_incidents import is_synthetic_evidence

logger = logging.getLogger(__name__)
install_logging_redaction()

OnTransition = Callable[[Optional[str], dict], None]

_BACKGROUND_SCAN_LOCKS: dict[str, threading.Lock] = {}
_BACKGROUND_SCAN_LOCKS_GUARD = threading.Lock()
_WATCHER_PROCESS_LOCK_HANDLE = None
# Cluster rows are cheap to enumerate compared with a Ceph health query.  A
# short discovery cadence lets the Dashboard toggle/add a cluster without
# restarting Watcher, while each cluster keeps its own normal poll cadence.
_CLUSTER_DISCOVERY_INTERVAL_SECONDS = 5
# ``ceph -s`` is more expensive than the health-detail poll, but its maps are
# required by the dashboard's OSD/MON/pool/PG cards. Keep it independent and
# refresh it often enough that the initial dashboard load is populated.
_DASHBOARD_STATUS_SNAPSHOT_INTERVAL_SECONDS = 15


def _acquire_watcher_process_lock(lock_path: str | None = None):
    """Acquire the process-wide Watcher singleton lock, non-blocking."""
    path = Path(lock_path) if lock_path else Path(
        os.environ.get("CEPH_AI_RUNTIME_DIR", "/run/ceph-ai")
    ) / "watcher.lock"
    try:
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        handle = path.open("a+")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle
    except OSError:
        logger.exception("watcher: failed to create/acquire process singleton lock at %s", path)
        return None


def _run_auxiliary_scan(name: str, callback: Callable[[], None], *, background: bool) -> bool:
    """Keep slow auxiliary collectors from delaying the core health poll.

    Production uses one daemon thread and a non-overlap lock per collector;
    bounded test loops remain synchronous and deterministic.
    """
    if not background:
        callback()
        return True
    with _BACKGROUND_SCAN_LOCKS_GUARD:
        lock = _BACKGROUND_SCAN_LOCKS.setdefault(name, threading.Lock())
    if not lock.acquire(blocking=False):
        logger.info("run: auxiliary scan %s is still running; skipping overlapping tick", name)
        return False

    def run_and_release() -> None:
        try:
            callback()
        finally:
            lock.release()

    threading.Thread(
        target=run_and_release, name=f"watcher-aux-{name}", daemon=True,
    ).start()
    return True

# Only these statuses represent a problem worth an Incident — a transition
# back to HEALTH_OK is a recovery, not a new incident (Story 1.4 AC #4).
PROBLEM_STATUSES = {"HEALTH_WARN", "HEALTH_ERR"}

# Incident statuses that still mean "this problem isn't closed out yet" —
# mirrors dashboard/routes/incidents.py::OPEN_STATUSES (kept as its own copy
# rather than a cross-import: Watcher and Dashboard are independent
# processes/services). AUTO_FIXED/RESOLVED/REJECTED are deliberately
# excluded — those are already terminal, nothing to recover them FROM.
_RECOVERABLE_STATUSES = {
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    # 2026-08-20: lệnh đã chạy nhưng CHƯA xác minh là hết lỗi —
    # vẫn là một sự cố đang mở. Thiếu dòng này, mọi chỗ dùng tập
    # trạng thái này để chống trùng sẽ tưởng Incident đã đóng và
    # tạo thêm một Incident nữa cho cùng vấn đề.
    IncidentStatus.VERIFYING.value,
    IncidentStatus.FAILED.value,
}

# Operator-created volume/performance proposals are synthetic Incidents used
# only to carry an approval-gated Action. Keep this an explicit set: a broad
# RBD_/CINDER_ prefix would also swallow a future real Ceph health code and
# prevent that health Incident from ever being auto-resolved.
_OPERATOR_VOLUME_CEPH_CODES = frozenset({
    "RBD_TRASH_REMOVE",
    "RBD_TRASH_PURGE_ALL",
    "RBD_VOLUME_CREATE",
    "RBD_VOLUME_RESIZE",
    "RBD_VOLUME_RENAME",
    "RBD_VOLUME_TRASH_MOVE",
    "RBD_VOLUME_TRASH_RESTORE",
    "CINDER_VOLUME_ATTACH",
    "CINDER_VOLUME_DETACH",
    "CINDER_SNAPSHOT_CREATE",
    "VOLUME_PERF_SWEEP",
    "VM_PERF_BENCHMARK",
})

# FAILED remains visible/open until Ceph confirms recovery, but it is not an
# in-flight remediation.  Let a fresh Watcher transition create a new attempt
# for a warning that still exists; otherwise one historical router/preflight
# failure permanently suppresses Autopilot for that ceph_code.
# A SAFE action is still active while its cancellation grace window is open.
# It must block a second Incident, but it is intentionally not in
# ``_RECOVERABLE_STATUSES``: generic health reconciliation must never close a
# grace-period Incident while its Action can still resume execution.
_IN_FLIGHT_DEDUPE_STATUSES = (
    _RECOVERABLE_STATUSES - {IncidentStatus.FAILED.value}
) | {IncidentStatus.GRACE_PENDING.value}
_INFLIGHT_INCIDENT_UNIQUE_INDEX = "uq_incidents_inflight_cluster_code"
_OSD_RESTART_RETRY_AFTER_SECONDS = 30
_IDEMPOTENT_VERIFY_RETRY_ACTIONS = {
    "MON_MSGR2_NOT_ENABLED": "enable_mon_msgr2",
}

# Mirrors dashboard/routes/chat.py::CHAT_REQUEST_CEPH_CODE (kept as its own
# copy rather than a cross-import — Watcher and Dashboard are independent
# processes/services, same posture as _RECOVERABLE_STATUSES above). A
# chat-confirmed action's synthetic Incident uses this ceph_code and is
# never something Watcher itself detected — its lifecycle is owned entirely
# by the Action/Worker pipeline (SAFE/RISKY execution outcome), so Watcher
# must never touch its status.
_CHAT_REQUEST_CEPH_CODE = "CHAT_REQUEST"

# 2026-07-23: same reasoning as _CHAT_REQUEST_CEPH_CODE above, for
# dashboard/routes/upgrade.py's synthetic Cluster Upgrade Incident. Without
# this guard, an upgrade Action stuck at PENDING_APPROVAL/APPROVED — or one
# that genuinely FAILED — gets silently overwritten to RESOLVED on
# Watcher's very next poll (its ceph_code can never match a real
# `ceph health detail` check code either), hiding the real outcome from the
# operator exactly the way the CHAT_REQUEST bug did before that fix.
_CLUSTER_UPGRADE_CEPH_CODE = "CLUSTER_UPGRADE"

# Synthetic workflow rows are operator proposals, not cluster-health
# findings.  Their own approval/execution flows already send the relevant
# notifications; repeating them through the generic incident reminder makes
# a pending operation look like a fresh Ceph fault every hour.
_REMINDER_EXCLUDED_CODES = {
    _CHAT_REQUEST_CEPH_CODE,
    _CLUSTER_UPGRADE_CEPH_CODE,
}


def _recent_failed_incident_codes(session, cluster_id: str | None, now: datetime) -> set[str]:
    """Return checks whose failed attempt is still in the retry cooldown.

    ``failed_at`` is separate from ``updated_at`` so acknowledgement and mute
    changes cannot postpone a retry.  ``updated_at`` remains a compatibility
    fallback for rows created before the timestamp migration.
    """
    cutoff = now - timedelta(seconds=settings.incident_failed_retry_cooldown_seconds)
    query = session.query(Incident.ceph_code).filter(
        Incident.status == IncidentStatus.FAILED.value,
        func.coalesce(Incident.failed_at, Incident.updated_at) >= cutoff,
    )
    query = (
        query.filter(Incident.cluster_id == cluster_id)
        if cluster_id is not None
        else query.filter(Incident.cluster_id.is_(None))
    )
    return {row.ceph_code for row in query.all()}


def _is_inflight_incident_duplicate(error: IntegrityError) -> bool:
    """Return true only for the active-Incident partial unique-index race.

    An existence query after rollback is insufficient: an unrelated FK or
    schema error could occur while another active Incident already exists.
    PostgreSQL exposes the violated index in ``diag.constraint_name``; every
    other integrity error must remain visible to the caller.
    """
    diagnostic = getattr(error.orig, "diag", None)
    return getattr(diagnostic, "constraint_name", None) == _INFLIGHT_INCIDENT_UNIQUE_INDEX


def send_due_incident_reminders(
    now: datetime | None = None,
    *,
    muted_ceph_codes: frozenset[str] = frozenset(),
) -> int:
    """Re-send every still-open Incident to Telegram once per configured interval.

    `muted_ceph_codes` là các mã đang bị mute trong CHÍNH Ceph ở lần poll
    health gần nhất. Vòng nhắc lại này đọc từ bảng Incident chứ không thấy
    `ceph health detail`, nên nếu không truyền vào đây thì việc bỏ gửi lúc
    tạo Incident là vô ích: cứ mỗi chu kỳ nhắc, cảnh báo đã mute lại được
    bắn tiếp.
    """
    now = now or utc_now()
    interval = max(60, settings.telegram_incident_reminder_interval_seconds)
    cutoff = now - timedelta(seconds=interval)
    sent = 0
    pending_event_ids: list[str] = []
    with db.SessionLocal() as session:
        open_incidents = (
            session.query(Incident)
            .filter(Incident.status.in_(_RECOVERABLE_STATUSES))
            .filter(Incident.ceph_code.notin_(_REMINDER_EXCLUDED_CODES))
            .filter(~Incident.ceph_code.like(f"{PERFORMANCE_RCA_PREFIX}%"))
            .filter(or_(Incident.muted_until.is_(None), Incident.muted_until <= now))
            .order_by(Incident.created_at.desc())
            .all()
        )

        # Historical bugs (and the observed-cluster path fixed below) could
        # leave several open rows for the same real health check.  Reminding
        # all of them produces a burst of identical Telegram messages.  Keep
        # only the newest row per cluster/code, and decide whether THAT row is
        # due; an older due row must not bypass a newer row's reminder clock.
        incidents = []
        seen: set[tuple[str | None, str]] = set()
        for incident in open_incidents:
            key = (incident.cluster_id, incident.ceph_code)
            if key in seen:
                continue
            seen.add(key)
            reminder_baseline = incident.telegram_reminded_at or incident.created_at
            if reminder_baseline <= cutoff:
                incidents.append(incident)
        clusters = {
            cluster.id: cluster
            for cluster in session.query(Cluster).filter(Cluster.id.in_({i.cluster_id for i in incidents if i.cluster_id})).all()
        }
        actions = {
            action.incident_id: action
            for action in session.query(Action).filter(Action.incident_id.in_([i.id for i in incidents])).all()
        }
        for incident in incidents:
            cluster = clusters.get(incident.cluster_id)
            # An inactive cluster is deliberately no longer polled, so its
            # old Incident rows cannot be compared with current health.
            # Re-sending them forever presents stale history as a live
            # fault. Keep the rows for audit, but silence reminders until
            # the cluster is activated and observed again.
            if cluster is not None and not cluster.is_active:
                continue
            if incident.ceph_code in muted_ceph_codes:
                # Vẫn giữ Incident cho Dashboard; chỉ không làm phiền nữa.
                continue
            action = actions.get(incident.id)
            pending_event_ids.append(
                telegram_outbox.enqueue_incident_alert(
                    session,
                    incident,
                    event_kind=f"reminder:{now.isoformat()}",
                    rationale=action.rationale if action else None,
                )
            )
            incident.telegram_reminded_at = now
            sent += 1
        session.commit()
    if pending_event_ids:
        telegram_outbox.dispatch_due(
            limit=len(pending_event_ids),
            event_ids=pending_event_ids,
        )
    return sent


def default_on_transition(previous_status: Optional[str], current: dict) -> None:
    """v1: just log the transition. Story 1.4 will replace/wrap this to build
    an Incident and publish it to RabbitMQ — this function is the injection
    point, intentionally kept out of scope here."""
    logger.warning(
        "Ceph cluster health transition: %s -> %s",
        previous_status,
        current.get("status"),
    )


async def _publish_delivery(event_id: str, envelope: dict) -> None:
    try:
        await publisher.publish_incident(envelope, event_id=event_id)
    except TypeError as exc:
        # Preserve compatibility with deterministic tests/injected legacy
        # publishers that still accept only the envelope argument.
        if "unexpected keyword argument 'event_id'" not in str(exc):
            raise
        await publisher.publish_incident(envelope)


async def _publish_all(deliveries: list[tuple[str, dict]]) -> None:
    """Publish every envelope over a single AMQP connection/event loop,
    instead of a full connect+declare per incident."""
    for event_id, envelope in deliveries:
        await _publish_delivery(event_id, envelope)
        incident_outbox.mark_published(event_id=event_id)


def _ceph_check_is_muted(check_detail: dict | None) -> bool:
    """Ceph có cơ chế im lặng riêng: `ceph health mute <CODE>`.

    `ceph health detail --format json` đánh dấu check đó `"muted": true`.
    Đấy là operator đã biết vấn đề, đã chấp nhận, và đã bảo Ceph đừng nhắc
    nữa — nhưng ceph-aiops vẫn nhắc lại đều đặn qua Telegram, khiến nút mute
    của Ceph thành vô nghĩa với hệ thống này.

    Incident vẫn được tạo: mute nghĩa là "đừng làm phiền tôi", không phải
    "vấn đề này không tồn tại", nên Dashboard vẫn phải thấy nó.
    """
    return isinstance(check_detail, dict) and bool(check_detail.get("muted"))


def _append_capacity_context(log_excerpt: str | None, signal_evidence_json: str | None) -> str | None:
    context = capacity_evidence.format_capacity_alert_context(signal_evidence_json)
    if not context:
        return log_excerpt
    if log_excerpt:
        return f"{log_excerpt}\n{context}"
    return context


def _resolve_recovered_incidents(
    current_codes: set[str], cluster_id: str | None = None, include_legacy_null: bool = True
) -> None:
    """A ceph_code no longer being reported by `ceph health detail` means
    THAT specific problem has recovered — even if the cluster overall is
    still WARN/ERR from a different, still-active check (only Incidents
    whose OWN ceph_code disappeared are touched, never a blanket "close
    everything").

    Called from `run()` on EVERY successful poll — not just when
    on_transition fires. That matters because Worker writes an Incident's
    OWN status independently and *later* than when Watcher last observed a
    transition (e.g. a RISKY action lands at PENDING_APPROVAL, or a SAFE one
    at FAILED, only once Worker gets around to draining a backlog) — a
    resolve step gated behind "did the health fingerprint just change" can
    permanently miss those, since nothing about the CLUSTER changes again
    afterward to re-trigger it. Cheap even every poll: one indexed DB query,
    no extra network I/O beyond the health check that already just ran.

    `cluster_id`/`include_legacy_null` (multi-cluster observability Phase 1):
    scopes which Incidents this poll may touch — without it, an OBSERVED
    cluster's poll (whose `current_codes` only ever reflects THAT cluster)
    would incorrectly resolve the default cluster's still-open Incidents
    the moment their ceph_code isn't also present in the observed cluster's
    health check. `include_legacy_null=True` (the default — matches the
    DEFAULT cluster's loop, and every existing caller before these
    parameters existed) ALSO matches legacy rows with `cluster_id IS NULL`
    (pre-migration Incidents, or any test that never set cluster_id) — see
    shared/models.py::Incident's own docstring for that convention. Observed
    (non-default) clusters never have such legacy rows, so their loop calls
    this with `include_legacy_null=False` — a real UUID match only.
    """
    # Store plain notification values, never ORM rows. session.commit()
    # expires Cluster attributes and the session closes before Telegram is
    # sent; retaining Cluster here caused DetachedInstanceError every poll.
    recovered: dict[tuple[str | None, str], str] = {}
    with db.SessionLocal() as session:
        cluster_filter = (
            or_(Incident.cluster_id == cluster_id, Incident.cluster_id.is_(None))
            if include_legacy_null
            else Incident.cluster_id == cluster_id
        )
        open_incidents = (
            session.query(Incident)
            # VERIFYING is exclusively owned by watcher/verify.py.  If this
            # generic recovery pass closes it first, the verifier cannot
            # emit the final "ĐÃ KHẮC PHỤC" notification or audit event.
            .filter(
                Incident.status.in_(_RECOVERABLE_STATUSES - {IncidentStatus.VERIFYING.value}),
                cluster_filter,
            )
            # Watcher and remediation_main may overlap during a rollout.  A
            # row lock makes this RESOLVED transition the single ownership
            # point for the recovery alert, rather than letting both loops
            # read the same open row and send the same Telegram message.
            .with_for_update(skip_locked=True)
            .all()
        )
        for incident in open_incidents:
            if is_synthetic_evidence(incident.signal_evidence_json):
                # Synthetic lab incidents have their own lifecycle. They do
                # not represent a live Ceph health check and must not be
                # auto-resolved on the next healthy poll.
                continue
            if incident.ceph_code in _OPERATOR_VOLUME_CEPH_CODES:
                # Dashboard volume/Trash/Cinder proposals use synthetic
                # ceph_code values to attach audit entries. They are not
                # health checks and must remain pending until approval or
                # explicit rejection/execution.
                continue
            if incident.ceph_code.startswith(PERFORMANCE_RCA_PREFIX):
                # Performance RCA owns this candidate's lifecycle. It is
                # driven by the RCA scan, not by ceph health detail, and
                # must not be resolved by this generic health reconciliation.
                continue
            if incident.ceph_code in (_CHAT_REQUEST_CEPH_CODE, _CLUSTER_UPGRADE_CEPH_CODE):
                # 2026-07-23 fix: a chat-confirmed action's (or cluster-
                # upgrade proposal's) synthetic Incident never corresponds
                # to a real `ceph health detail` check code, so it would
                # ALWAYS look "recovered" here (its ceph_code can never be
                # in current_codes) — without this guard, a genuinely
                # FAILED/PENDING_APPROVAL/APPROVED action got silently
                # overwritten to RESOLVED on Watcher's very next poll,
                # hiding the real outcome from the incident history.
                continue
            if incident.ceph_code.startswith(VOLUME_SATURATED_PREFIX):
                # 2026-07-28: same reasoning as the CHAT_REQUEST/
                # CLUSTER_UPGRADE guard above — a Volume-saturation
                # Incident's ceph_code (watcher/volume_monitor.py) can never
                # appear in a real `ceph health detail` check list either.
                # That module owns this ceph_code family's own create/
                # resolve lifecycle entirely (its own rolling-window
                # saturated-set, not this function's current_codes), so it
                # must be left alone here.
                continue
            if incident.ceph_code.startswith(DEVICE_HEALTH_EVACUATE_PREFIX):
                # 2026-08-01 (Story C): same reasoning as the
                # VOLUME_SATURATED_PREFIX guard just above —
                # watcher/device_health_monitor.py owns this ceph_code
                # family's own create/resolve lifecycle (its own
                # predicted-failing-and-still-in osd_id set), never a real
                # `ceph health detail` check code.
                continue
            if incident.ceph_code.startswith(NODE_RESOURCE_HIGH_PREFIX):
                # 2026-08-05: same reasoning as the VOLUME_SATURATED_PREFIX/
                # DEVICE_HEALTH_EVACUATE_PREFIX guards above —
                # watcher/node_health_monitor.py owns this ceph_code
                # family's own create/resolve lifecycle (its own
                # consecutive-high-scans streak per host), never a real
                # `ceph health detail` check code.
                continue
            if incident.ceph_code.startswith(BLUESTORE_OMAP_PREFIX):
                # 2026-08-06: same reasoning as the guards above —
                # watcher/bluestore_omap_monitor.py owns this ceph_code
                # family's own create/resolve lifecycle (its own
                # currently-affected-osd_id set, one Incident per osd_id,
                # not per raw `ceph health detail` check code — see
                # build_and_publish_incident's own BLUESTORE_NO_PER_POOL_OMAP
                # exclusion just below for the other half of this).
                continue
            if incident.ceph_code.startswith(OSD_LATENCY_HIGH_PREFIX):
                # 2026-08-07: same reasoning as the guards above —
                # watcher/osd_latency_monitor.py owns this ceph_code
                # family's own create/resolve lifecycle (its own
                # consecutive-high-scans streak per osd_id), never a real
                # `ceph health detail` check code.
                continue
            if incident.ceph_code.startswith(LOG_ANOMALY_PREFIX):
                # Log Intelligence findings have their own evidence and live
                # recovery gate. Their synthetic code never appears in
                # `ceph health detail`, so generic health reconciliation must
                # not resolve them or cancel a Telegram approval proposal.
                continue
            if incident.ceph_code.startswith(CRUSH_SKEW_USE_PREFIX) or incident.ceph_code.startswith(
                CRUSH_SKEW_PG_PREFIX
            ):
                # 2026-08-07 (Epic 12 Story 12.2, AD-32 — CRITICAL, found
                # independently by 2 reviewers during Architecture, before
                # any code existed): same reasoning as every guard above —
                # watcher/crush_skew_monitor.py owns this ceph_code family's
                # own create/resolve lifecycle (its own consecutive-scans
                # streak per OSD/Host entity, read from
                # CrushStructureSnapshot/CrushOsdDistribution), never a real
                # `ceph health detail` check code. WITHOUT this guard, a
                # CRUSH Skew Incident self-resolves within about one
                # `ceph health detail` poll tick (far faster than
                # crush_scan_interval_seconds) every time this function
                # runs, hiding it from the operator before it can ever be
                # acted on.
                continue
            if incident.ceph_code == DATABASE_SIZE_HIGH_PREFIX:
                # 2026-08-10: same reasoning as every guard above --
                # watcher/database_capacity_monitor.py owns this
                # ceph_code's own create/resolve lifecycle (its own
                # consecutive-scans streak), never a real `ceph health
                # detail` check code. Exact `==`, not `.startswith`, since
                # this ceph_code has no dynamic per-entity suffix (there
                # is only ever one database) -- still functionally the
                # same guard, just no colon-prefixed family to match.
                continue
            if incident.ceph_code not in current_codes:
                incident.status = IncidentStatus.RESOLVED.value
                cancel_pending_actions(session, incident.id)
                key = (incident.cluster_id, incident.ceph_code)
                if key not in recovered:
                    recovered[key] = telegram_outbox.enqueue_incident_verified_alert(
                        session,
                        incident_id=incident.id,
                        ceph_code=incident.ceph_code,
                    )
        session.commit()

    for event_id in recovered.values():
        telegram_outbox.dispatch_due(limit=1, event_ids=[event_id])


def _reconcile_terminal_actions() -> int:
    """Dọn action mồ côi và đóng sự cố của cluster đã bị vô hiệu hóa."""
    with db.SessionLocal() as session:
        inactive_incidents = (
            session.query(Incident)
            .join(Cluster, Cluster.id == Incident.cluster_id)
            .filter(
                Cluster.is_active.is_(False),
                Incident.status.in_(_RECOVERABLE_STATUSES),
            )
            .all()
        )
        count = 0
        for incident in inactive_incidents:
            incident.status = IncidentStatus.RESOLVED.value
            count += cancel_pending_actions(session, incident.id)
        count += reconcile_terminal_incident_actions(session)
        session.commit()
    if inactive_incidents:
        logger.warning(
            "Đã đóng %d Incident của cluster không hoạt động", len(inactive_incidents)
        )
    if count:
        logger.warning("Đã tự huỷ %d action mồ côi của Incident đã kết thúc", count)
    return count


def build_and_publish_incident(
    previous_status: Optional[str], health: dict, cluster_id: str | None = None
) -> None:
    """Real production `on_transition` callback (Story 1.4): for a transition
    INTO HEALTH_WARN/HEALTH_ERR, create one Incident per reported check, collect
    its relevant daemon logs, write the row to DB, and publish it to RabbitMQ.

    A transition back to HEALTH_OK never creates a new Incident here — no-op
    (closing out old ones on recovery is `_resolve_recovered_incidents`,
    called every poll from `run()`, not from here — see that function's
    docstring for why it needs to run more often than "only on transition").
    Does NOT modify `default_on_transition` (Story 1.3, unchanged).

    Log collection (slow, network-bound SSH) happens OUTSIDE any DB session —
    holding a DB connection open across seconds of unrelated SSH I/O risks
    lock contention with anything else touching the DB (e.g. the Dashboard).
    All envelopes for this call are published together in one `asyncio.run()`
    rather than one per incident, avoiding a full connect+declare per check.

    `cluster_id` (multi-cluster observability Phase 1, default None means
    "the default cluster" — see Incident's own docstring): this function is
    used ONLY for the default cluster's loop (`run_all_clusters()` binds it
    via `functools.partial` before passing it as `on_transition`) — it still
    calls `collector.collect_relevant_logs`, which reads OSD/MGR/MON node
    lists and exec-mode/container settings straight from the global
    `settings` singleton (NOT parameterized), so it is only ever correct for
    the cluster `settings` actually describes. An OBSERVED (non-default)
    cluster's incidents are built by the separate, much simpler
    `_build_and_publish_incident_for_observed_cluster` below, which
    deliberately skips log collection rather than risk collecting the
    WRONG cluster's logs.
    """
    current_status = health.get("status")
    current_checks = health.get("checks", {})

    if current_status not in PROBLEM_STATUSES:
        return

    # 2026-08-20: `run()` chỉ gọi hàm này khi status/checks ĐỔI so với
    # `last_status`/`last_checks` -- hai biến chỉ sống trong RAM, nên lần
    # poll đầu tiên sau MỖI lần Watcher restart luôn thoả điều kiện "đổi"
    # và chạy vào đây với toàn bộ trạng thái sức khoẻ hiện tại. Trước bản
    # vá này vòng lặp dưới tạo Incident VÔ ĐIỀU KIỆN, không hề nhìn xem đã
    # có Incident nào đang mở cho cùng `ceph_code` chưa -- nên mỗi lần
    # restart là một loạt Incident + Telegram trùng lặp cho đúng những vấn
    # đề đang mở sẵn. Lọc ra trước, một truy vấn cho cả lượt.
    retry_dedupe_keys: dict[str, str] = {}
    with db.SessionLocal() as session:
        query = session.query(Incident.ceph_code).filter(
            Incident.status.in_(_IN_FLIGHT_DEDUPE_STATUSES)
        )
        query = (
            query.filter(Incident.cluster_id == cluster_id)
            if cluster_id is not None
            else query.filter(Incident.cluster_id.is_(None))
        )
        already_open_codes = {row.ceph_code for row in query.all()}
        # A low-confidence abstention is not an execution failure and should
        # not immediately create another identical AI request on the next
        # unrelated health transition or Watcher restart. Retry after a
        # bounded cooldown so newer evidence can still be reconsidered.
        low_confidence_cutoff = utc_now() - timedelta(
            seconds=settings.ai_low_confidence_retry_cooldown_seconds
        )
        low_confidence_query = (
            session.query(Incident.ceph_code)
            .join(AuditEntry, AuditEntry.incident_id == Incident.id)
            .filter(
                Incident.status == IncidentStatus.FAILED.value,
                AuditEntry.event_type == audit.EVENT_PROPOSAL_BLOCKED_BY_LOW_CONFIDENCE,
                AuditEntry.created_at >= low_confidence_cutoff,
            )
        )
        low_confidence_query = (
            low_confidence_query.filter(Incident.cluster_id == cluster_id)
            if cluster_id is not None
            else low_confidence_query.filter(Incident.cluster_id.is_(None))
        )
        already_open_codes.update(row.ceph_code for row in low_confidence_query.all())
        # FAILED is deliberately eligible for retry eventually, but a
        # provider/SSH failure must not create and Telegram-alert a new row
        # every time the same health check flaps during that failure window.
        already_open_codes.update(
            _recent_failed_incident_codes(session, cluster_id, utc_now())
        )

        # OSD_DOWN can recur immediately after a successful systemd restart
        # (or the daemon can die again while the previous Incident is still
        # waiting for the generic verification window).  Once the exact OSD
        # restart has been AUTO_EXECUTED for 30 seconds, a fresh Ceph
        # OSD_DOWN is new actionable evidence and must not be suppressed by
        # the old VERIFYING row.  Any other open OSD_DOWN state still blocks
        # duplicates normally.
        if "OSD_DOWN" in already_open_codes:
            osd_query = session.query(Incident).filter(
                Incident.ceph_code == "OSD_DOWN",
                Incident.status.in_(_IN_FLIGHT_DEDUPE_STATUSES),
            )
            osd_query = (
                osd_query.filter(Incident.cluster_id == cluster_id)
                if cluster_id is not None else osd_query.filter(Incident.cluster_id.is_(None))
            )
            cutoff = utc_now() - timedelta(seconds=_OSD_RESTART_RETRY_AFTER_SECONDS)
            open_osd_rows = osd_query.all()
            retryable = bool(open_osd_rows)
            for open_incident in open_osd_rows:
                if open_incident.status != IncidentStatus.VERIFYING.value:
                    retryable = False
                    break
                executed = session.query(Action).filter(
                    Action.incident_id == open_incident.id,
                    Action.action_id == "restart_osd_daemon",
                    Action.status == ActionStatus.AUTO_EXECUTED.value,
                    Action.executed_at.isnot(None),
                    Action.executed_at <= cutoff,
                ).first()
                if executed is None:
                    retryable = False
                    break
            if retryable:
                already_open_codes.discard("OSD_DOWN")
                retry_dedupe_keys["OSD_DOWN"] = (
                    "osd-down-retry:" + ",".join(sorted(row.id for row in open_osd_rows))
                )

        # An idempotent SAFE repair can be deliberately undone while its
        # previous action is still waiting in VERIFYING.  The new health
        # transition is regression evidence, not a duplicate: allow one new
        # attempt when every open row is the already AUTO_EXECUTED verifier.
        # Once that new row exists in NEW/DIAGNOSING/etc. normal dedupe takes
        # over again, so watcher restarts cannot create an incident storm.
        for retry_code, retry_action_id in _IDEMPOTENT_VERIFY_RETRY_ACTIONS.items():
            if retry_code not in already_open_codes:
                continue
            retry_query = session.query(Incident).filter(
                Incident.ceph_code == retry_code,
                Incident.status.in_(_IN_FLIGHT_DEDUPE_STATUSES),
            )
            retry_query = (
                retry_query.filter(Incident.cluster_id == cluster_id)
                if cluster_id is not None else retry_query.filter(Incident.cluster_id.is_(None))
            )
            open_rows = retry_query.all()
            if open_rows and all(
                row.status == IncidentStatus.VERIFYING.value
                and session.query(Action).filter(
                    Action.incident_id == row.id,
                    Action.action_id == retry_action_id,
                    Action.status == ActionStatus.AUTO_EXECUTED.value,
                ).first() is not None
                for row in open_rows
            ):
                already_open_codes.discard(retry_code)

    envelopes = []
    for ceph_code, check_detail in current_checks.items():
        if ceph_code in already_open_codes:
            # Vấn đề này đã có Incident đang mở -- operator đã được báo và
            # có thể đang xử lý dở. Một Incident thứ hai cho cùng
            # `ceph_code` không thêm thông tin gì, chỉ nhân đôi thông báo
            # và nhân đôi hàng chờ duyệt.
            continue
        if ceph_code == "BLUESTORE_NO_PER_POOL_OMAP":
            # 2026-08-06: watcher/bluestore_omap_monitor.py owns this real
            # ceph_code end-to-end now (its own PENDING_APPROVAL Incident+
            # Action per affected osd_id, deterministic osd_id/host
            # resolution, no LLM guessing) — creating a SECOND, generic
            # Incident for the same check here would duplicate it and send
            # it needlessly through the AI-diagnosis pipeline, which has no
            # way to safely pick a real osd_id anyway (see that module's own
            # docstring for why this action was deliberately never wired
            # into Chat-with-AI/diagnosis in the first place).
            continue
        detected_at = utc_now()
        # 2026-08-20: dict rỗng truyền xuống làm out-param — collector điền
        # {osd_id: host} đã tra thật cho những OSD check này nêu đích danh.
        osd_host_map: dict[int, str] = {}
        nodes, log_excerpt = collector.collect_relevant_logs(
            ceph_code, check_detail, osd_host_map=osd_host_map
        )
        signal_evidence_json = capacity_evidence.collect_capacity_evidence(
            ceph_code, check_detail
        )
        log_excerpt = _append_capacity_context(log_excerpt, signal_evidence_json)
        ceph_check_muted = _ceph_check_is_muted(check_detail)
        notification_event_id = None

        with db.SessionLocal() as session:
            incident = Incident(
                ceph_code=ceph_code,
                status=IncidentStatus.NEW.value,
                detected_at=detected_at,
                log_excerpt=log_excerpt,
                # Ceph's own per-check severity (e.g. this exact check may be
                # HEALTH_WARN even while the overall cluster `status` is
                # HEALTH_ERR from a different check) — not to be confused
                # with Incident.status, this codebase's own lifecycle state.
                severity=check_detail.get("severity"),
                cluster_id=cluster_id,
                dedupe_key=retry_dedupe_keys.get(ceph_code),
                signal_evidence_json=signal_evidence_json,
            )
            session.add(incident)
            try:
                # Flush assigns the primary key while the ORM instance is
                # still live. Inherit mute before the single commit so the
                # incident row and its notification preference are atomic;
                # never re-query a possibly expired/detached ORM instance.
                session.flush()
                incident_id = incident.id
                notification_muted = alert_lifecycle.inherit_active_mute(
                    session, incident, now=detected_at,
                )
                if ceph_check_muted:
                    notification_muted = True
                if not notification_muted:
                    notification_event_id = telegram_outbox.enqueue_incident_alert(
                        session, incident,
                    )
                envelope = publisher.build_envelope(
                    incident_id=incident_id,
                    ceph_code=ceph_code,
                    detected_at=detected_at.isoformat(),
                    nodes=nodes,
                    log_excerpt=log_excerpt,
                    cluster_snapshot=health,
                    cluster_id=cluster_id,
                    ssh_user=settings.ssh_user,
                    ssh_key_path=settings.ssh_key_path,
                    ceph_exec_mode=settings.ceph_exec_mode,
                    ceph_container_name=settings.ceph_container_name,
                    osd_hosts=osd_host_map,
                )
                event_id = incident_outbox.enqueue(
                    session, incident_id=incident_id, payload=envelope,
                )
                session.commit()
            except IntegrityError as exc:
                # The DB partial unique index is the authoritative dedupe
                # boundary.  Another Watcher won the concurrent insert, so
                # do not emit a second Telegram alert or queue message.
                session.rollback()
                if not _is_inflight_incident_duplicate(exc):
                    logger.exception(
                        "build_and_publish_incident: unexpected integrity error "
                        "for cluster_id=%s ceph_code=%s",
                        cluster_id,
                        ceph_code,
                    )
                    raise
                logger.info(
                    "build_and_publish_incident: duplicate in-flight incident skipped "
                    "for cluster_id=%s ceph_code=%s",
                    cluster_id,
                    ceph_code,
                )
                continue
        if ceph_check_muted:
            logger.info(
                "build_and_publish_incident: %s bị mute trong Ceph; tạo Incident nhưng không gửi Telegram",
                ceph_code,
            )

        # The Incident and immutable notification event are committed before
        # this bounded dispatch attempt. AI diagnosis remains an enrichment;
        # a delivery failure leaves the outbox row retryable and never blocks
        # RabbitMQ publishing.
        if notification_event_id:
            telegram_outbox.dispatch_due(
                limit=1,
                event_ids=[notification_event_id],
            )
        envelopes.append((event_id, envelope))

    if not envelopes:
        return
    try:
        asyncio.run(_publish_all(envelopes))
    except Exception:
        # Incident rows already exist in DB even if publish fails — not
        # lost, just not yet queued (Dev Notes: acceptable v1 trade-off).
        logger.exception(
            "build_and_publish_incident: failed to publish %d incident(s) to RabbitMQ",
            len(envelopes),
        )


def _record_heartbeat_safe(
    success: bool, mon_node: Optional[str], error_message: Optional[str], cluster_id: str | None = None
) -> None:
    """Story 5.2: records the outcome of EVERY poll attempt (success or
    failure), independent of whether `on_transition` fires — this is a
    separate signal ("is Watcher able to reach the cluster at all") from
    Incident data ("is the cluster healthy"). A failure to record the
    heartbeat itself must never kill the poll loop — that would defeat the
    whole point of this monitoring process.

    `cluster_id=None` (multi-cluster observability Phase 1 default) means
    "the default cluster" — shared/heartbeat.py's own docstring covers why
    that's a distinct, valid key rather than an error."""
    try:
        with db.SessionLocal() as session:
            heartbeat.record(
                session,
                cluster_id=cluster_id,
                success=success,
                mon_node=mon_node,
                error_message=error_message,
                polled_at=utc_now(),
            )
            session.commit()
    except Exception:
        logger.exception("run: failed to record heartbeat")


def run(
    on_transition: OnTransition = default_on_transition,
    max_iterations: Optional[int] = None,
    cluster_id: str | None = None,
) -> None:
    """Poll cluster health and fire `on_transition` when the overall status
    OR the set of reported check codes changes.

    Comparing status alone would miss a real problem: if the cluster stays at
    HEALTH_WARN while the underlying check changes (e.g. MON_CLOCK_SKEW
    resolves and OSD_DOWN appears a minute later, status never dipping to
    HEALTH_OK in between), the new check would never be detected. The
    fingerprint below — (status, the set of check codes) — catches that.

    `max_iterations=None` loops forever (real usage); a finite value lets
    tests exercise a bounded number of poll cycles.

    This is exclusively the DEFAULT cluster's loop (multi-cluster
    observability Phase 1) — it reads connection config from the global
    `settings` singleton throughout (query_cluster_health(), every secondary
    monitor below), never from a `Cluster` DB row. `cluster_id` only tags
    the Incident/heartbeat rows this iteration writes (defaults to None,
    preserving every existing caller's exact behavior); `run_all_clusters()`
    below is the real production entrypoint and passes the resolved default
    cluster's real id. An OBSERVED (non-default) cluster never calls this
    function — see `run_observed_cluster_loop` instead, which polls a
    `Cluster` row directly and skips every secondary monitor.
    """
    last_status: Optional[str] = None
    last_checks: frozenset = frozenset()
    # Avoid a restart stampede: health remains immediate, while expensive
    # auxiliary scans wait for their normal cadence in the production loop.
    # Finite test runs retain the historical first-iteration behavior.
    initial_auxiliary_scan_at = utc_now() if max_iterations is None else None
    last_device_health_scan_at: Optional[datetime] = initial_auxiliary_scan_at
    last_node_health_scan_at: Optional[datetime] = initial_auxiliary_scan_at
    last_node_reachability_scan_at: Optional[datetime] = initial_auxiliary_scan_at
    last_bluestore_omap_scan_at: Optional[datetime] = initial_auxiliary_scan_at
    last_osd_latency_scan_at: Optional[datetime] = initial_auxiliary_scan_at
    last_crush_scan_at: Optional[datetime] = initial_auxiliary_scan_at
    # Host telemetry feeds the Node Monitoring time-series and needs a much
    # shorter cadence than the CRUSH/RCA scans. Keep it independent so a
    # five-minute CRUSH interval cannot leave the chart with only one point.
    last_host_metrics_scan_at: Optional[datetime] = initial_auxiliary_scan_at
    last_snarimax_shadow_scan_at: Optional[datetime] = initial_auxiliary_scan_at
    last_volume_scan_at: Optional[datetime] = initial_auxiliary_scan_at
    last_volume_topology_scan_at: Optional[datetime] = initial_auxiliary_scan_at
    last_capacity_forecast_scan_at: Optional[datetime] = None
    last_database_size_scan_at: Optional[datetime] = None
    last_trash_capacity_scan_at: Optional[datetime] = None
    last_rgw_alert_scan_at: Optional[datetime] = None
    last_incident_reminder_scan_at: Optional[datetime] = None
    # Giữ lại giữa các vòng: khi một lần poll health thất bại, dùng tiếp tập
    # mute của lần thành công gần nhất còn hơn là coi như không có gì bị mute
    # rồi bắn lại đúng những cảnh báo operator đã tắt.
    muted_ceph_codes: frozenset[str] = frozenset()
    last_capability_scan_at: Optional[datetime] = None
    last_log_intel_scan_at: Optional[datetime] = None
    last_inventory_scan_at: Optional[datetime] = None
    last_status_snapshot_scan_at: Optional[datetime] = None
    last_priority_refresh_marker: str | None = None
    last_health_status_sent_at: Optional[datetime] = None
    iterations = 0

    def run_auxiliary_scan(name: str, callback: Callable[[], None]) -> bool:
        """Run slow scans off the critical-health loop in production.

        Bounded test runs stay synchronous so their assertions remain
        deterministic and no daemon thread outlives an isolated test DB.
        """
        return _run_auxiliary_scan(
            name,
            callback,
            background=max_iterations is None,
        )

    while max_iterations is None or iterations < max_iterations:
        service_health.record_safe("watcher")
        poll_started_monotonic = time.monotonic()
        poll_started_at = cluster_snapshot_collector.collection_timestamp()
        # Tracks whether THIS iteration already recorded a heartbeat (i.e.
        # query_cluster_health() itself succeeded) — so the generic except
        # below (Review Story 5.2) only records a FAILED heartbeat when the
        # poll itself never completed, and never overwrites an already-True
        # heartbeat just because a downstream on_transition bug raised.
        heartbeat_recorded = False
        # Reset every iteration (not just declared once outside the loop) —
        # otherwise a query_cluster_health() failure on iteration N+1 would
        # leave the bluestore-omap scan block below reading iteration N's
        # stale dict instead of correctly seeing "no health data this tick".
        health: Optional[dict] = None
        try:
            with cluster_snapshot_collector.health_collection_lock(cluster_id):
                try:
                    health = query_cluster_health()
                except CephQueryError as exc:
                    try:
                        cluster_snapshot_collector.publish_health_error(cluster_id, exc)
                    except Exception:
                        logger.exception("run: failed to record health snapshot error")
                    raise
                try:
                    cluster_snapshot_collector.publish_health_snapshot(
                        cluster_id,
                        health,
                        collection_started_monotonic=poll_started_monotonic,
                        collection_started_at=poll_started_at,
                    )
                except Exception:
                    logger.exception("run: failed to publish critical health snapshot")
            _record_heartbeat_safe(True, ceph_client.last_successful_mon_node, None, cluster_id=cluster_id)
            heartbeat_recorded = True
            current_status = health.get("status")
            current_checks = frozenset(health.get("checks", {}).keys())
            muted_ceph_codes = frozenset(
                code
                for code, detail in health.get("checks", {}).items()
                if _ceph_check_is_muted(detail)
            )
            status_now = utc_now()
            if (
                last_health_status_sent_at is None
                or (status_now - last_health_status_sent_at).total_seconds()
                >= settings.telegram_health_status_interval_seconds
            ):
                cluster = None
                if cluster_id is not None:
                    with db.SessionLocal() as session:
                        cluster = session.get(Cluster, cluster_id)
                        if cluster is not None:
                            session.expunge(cluster)
                health_status = current_status or "UNKNOWN"
                health_codes = sorted(current_checks)
                bucket = int(status_now.timestamp()) // max(
                    60, settings.telegram_health_status_interval_seconds
                )
                fingerprint = hashlib.sha1(
                    ",".join(health_codes).encode("utf-8"),
                    usedforsecurity=False,
                ).hexdigest()[:16]
                telegram_outbox.enqueue_alert_call_and_dispatch(
                    event_id=f"health-status:{cluster_id or 'default'}:{bucket}:{health_status}:{fingerprint}",
                    category="incident",
                    function="send_periodic_health_status",
                    args=(health_status, health_codes),
                    cluster_id=cluster_id,
                    cluster_name=cluster.name if cluster else settings.cluster_name,
                    sender=telegram_alerts.send_periodic_health_status,
                )
                last_health_status_sent_at = status_now
            # Every poll, regardless of whether the fingerprint below
            # changed — see _resolve_recovered_incidents' docstring for why
            # gating this behind "only on transition" can permanently miss
            # Incidents Worker resolves/fails on its own, later.
            _resolve_recovered_incidents(set(current_checks), cluster_id=cluster_id)
            _reconcile_terminal_actions()
            # 2026-08-20 (xác minh sau khắc phục): CHỈ gọi trong nhánh
            # thành công này, không bao giờ ở nhánh except bên dưới —
            # `current_checks` rỗng vì MON không trả lời trông y hệt "cụm
            # hoàn toàn khoẻ", và sẽ báo "đã khắc phục" cho mọi Incident
            # đang chờ xác minh trong khi thực tế là ta đang mù.
            try:
                verify.verify_pending_incidents(
                    set(current_checks), health=health, cluster_id=cluster_id
                )
            except Exception:
                logger.exception("run: vòng xác minh sau khắc phục thất bại")
            if current_status != last_status or current_checks != last_checks:
                on_transition(last_status, health)
                last_status = current_status
                last_checks = current_checks
        except CephQueryError as exc:
            _record_heartbeat_safe(False, None, str(exc), cluster_id=cluster_id)
            if "no MON nodes configured" in str(exc):
                # 2026-07-28 (found on a real first-time install): expected,
                # quiet state before the operator has configured a cluster
                # yet (via .env or the Settings page) — Watcher keeps
                # retrying forever regardless (see this function's own
                # docstring), so this is NOT a failure worth an ERROR +
                # full traceback every poll. A real install hit exactly
                # this and Ctrl-C'd out of Watcher, mistaking normal
                # "nothing configured yet" for something broken.
                logger.info(
                    "run: %s — cấu hình CEPH_MON_NODES (.env hoặc trang Cài đặt) để bắt đầu giám sát",
                    exc,
                )
            else:
                logger.exception("run: failed to query cluster health from any MON node")
        except Exception as exc:
            # A bug in on_transition (or anything else unexpected) must not
            # permanently kill monitoring — that would defeat the entire
            # point of this loop. Log and keep polling. Still record a
            # failed heartbeat if the poll attempt itself never succeeded
            # (AC #1: every poll iteration gets a heartbeat, success or not).
            if not heartbeat_recorded:
                _record_heartbeat_safe(False, None, str(exc), cluster_id=cluster_id)
            logger.exception("run: unexpected error during poll iteration")

        # Pools/PGs are deliberately much slower than the critical health
        # query. Keep their shared read models warm in one background scan so
        # page requests never open SSH sessions of their own.
        if max_iterations is None and cluster_id is not None:
            status_now = utc_now()
            priority = read_priority_refresh(cluster_id)
            priority_marker = str(
                priority.get("request_id") or priority.get("requested_at", "")
            ) if priority else ""
            priority_due = bool(priority_marker and priority_marker != last_priority_refresh_marker)
            if (
                last_status_snapshot_scan_at is None
                or (status_now - last_status_snapshot_scan_at).total_seconds()
                >= _DASHBOARD_STATUS_SNAPSHOT_INTERVAL_SECONDS
                or priority_due
            ):
                def scan_status() -> None:
                    try:
                        with db.SessionLocal() as session:
                            status_cluster = session.get(Cluster, cluster_id)
                            if status_cluster is not None:
                                session.expunge(status_cluster)
                        if status_cluster is not None and status_cluster.is_active:
                            cluster_snapshot_collector.collect_and_publish_status(status_cluster)
                    except Exception:
                        logger.exception("run: dashboard status snapshot collection failed")

                status_scan_started = _run_auxiliary_scan(
                    f"status-{cluster_id}", scan_status, background=True,
                )
                last_status_snapshot_scan_at = status_now

            inventory_now = utc_now()
            if (
                last_inventory_scan_at is None
                or (inventory_now - last_inventory_scan_at).total_seconds()
                >= settings.dashboard_inventory_poll_interval_seconds
                or priority_due
            ):
                def scan_inventory() -> None:
                    try:
                        with db.SessionLocal() as session:
                            inventory_cluster = session.get(Cluster, cluster_id)
                            if inventory_cluster is not None:
                                session.expunge(inventory_cluster)
                        if inventory_cluster is not None and inventory_cluster.is_active:
                            cluster_snapshot_collector.collect_and_publish_inventory(inventory_cluster)
                    except Exception:
                        logger.exception("run: inventory snapshot collection failed")

                inventory_scan_started = _run_auxiliary_scan(
                    f"inventory-{cluster_id}", scan_inventory, background=True,
                )
                last_inventory_scan_at = inventory_now
            if priority_due and status_scan_started and inventory_scan_started:
                last_priority_refresh_marker = priority_marker

        # 2026-07-28: Volume (RBD) performance/saturation check — its own
        # independent try/except, OUTSIDE the cluster-health try block
        # above, on purpose: it queries `rbd perf image iostat`, not `ceph
        # health detail`, so a MON being briefly unreachable for one must
        # not also skip the other, and vice versa. Auto-discovers every
        # RBD-application pool by default (see
        # watcher/ceph_client.py::configured_rbd_pools) — only a no-op if
        # the cluster genuinely has no RBD pools yet or is unreachable.
        # persist_last_poll_metrics writes every sample check_volumes just
        # saw to shared.models.VolumeMetric (see that function's own
        # docstring). It is deliberately on a slower, independent cadence:
        # every pool query in cephadm mode creates a temporary Podman shell.
        def scan_volumes() -> None:
            try:
                current_saturated = volume_monitor.check_volumes(cluster_id=cluster_id)
                volume_monitor.persist_last_poll_metrics(cluster_id=cluster_id)
                volume_monitor.create_or_resolve_volume_incidents(
                    current_saturated, cluster_id=cluster_id, include_legacy_null=True
                )
            except Exception:
                logger.exception("run: volume saturation check failed")
        volume_now = utc_now()
        if (
            last_volume_scan_at is None
            or (volume_now - last_volume_scan_at).total_seconds()
            >= settings.volume_scan_interval_seconds
        ):
            _run_auxiliary_scan(
                f"volume-{cluster_id or 'default'}", scan_volumes,
                background=max_iterations is None,
            )
            last_volume_scan_at = volume_now

        # Aggregate Trash needs one query per RBD pool plus `ceph df`, so
        # cap it at once per minute even when the main poll is faster.
        trash_now = utc_now()
        if (
            last_trash_capacity_scan_at is None
            or (trash_now - last_trash_capacity_scan_at).total_seconds()
            >= getattr(settings, "trash_capacity_scan_interval_seconds", 300)
        ):
            def scan_trash() -> None:
                try:
                    trash_capacity_monitor.check_and_alert()
                except Exception:
                    logger.exception("run: trash capacity check failed")
            _run_auxiliary_scan(
                f"trash-{cluster_id or 'default'}", scan_trash,
                background=max_iterations is None,
            )
            last_trash_capacity_scan_at = trash_now

        # RGW alerting consumes durable audit rows only.  It is independent
        # from the Ceph health poll and never executes an RGW mutation.
        rgw_now = utc_now()
        if (
            last_rgw_alert_scan_at is None
            or (rgw_now - last_rgw_alert_scan_at).total_seconds()
            >= rgw_alerting.RGW_ALERT_SCAN_INTERVAL_SECONDS
        ):
            def scan_rgw_alerts() -> None:
                try:
                    rgw_alerting.scan_and_alert(cluster_id)
                except Exception:
                    logger.exception("run: RGW alert scan failed")

            _run_auxiliary_scan(
                f"rgw-alerts-{cluster_id or 'default'}", scan_rgw_alerts,
                background=max_iterations is None,
            )
            last_rgw_alert_scan_at = rgw_now

        # 2026-08-01 (Story C): DeviceHealth-driven evacuation-proposal scan
        # — own independent try/except (same isolation reasoning as the
        # volume-saturation block above) and its OWN, much slower cadence
        # (settings.device_health_scan_interval_seconds, default 1h) rather
        # than running `ceph device ls`/`ceph osd dump` every single
        # watcher_poll_interval_seconds tick — see that setting's own
        # comment in config/settings.py for why.
        now = utc_now()
        if (
            last_incident_reminder_scan_at is None
            or (now - last_incident_reminder_scan_at).total_seconds() >= 60
        ):
            try:
                send_due_incident_reminders(now, muted_ceph_codes=muted_ceph_codes)
            except Exception:
                logger.exception("run: Telegram incident reminder scan failed")
            last_incident_reminder_scan_at = now
        if (
            last_device_health_scan_at is None
            or (now - last_device_health_scan_at).total_seconds()
            >= settings.device_health_scan_interval_seconds
        ):
            def scan_device_health() -> None:
                try:
                    current_predicted_failing = device_health_monitor.check_predicted_failing_osds()
                    device_health_monitor.create_or_resolve_device_health_incidents(
                        current_predicted_failing
                    )
                except Exception:
                    logger.exception("run: device health scan failed")

            run_auxiliary_scan(
                f"device-health-{cluster_id or 'default'}", scan_device_health,
            )
            last_device_health_scan_at = now

        # A down host otherwise only appears as best-effort collector errors.
        # Keep this independent from the slower CPU/RAM scan so operators get
        # a hardware-channel Telegram alert within roughly two failed probes.
        if (
            last_node_reachability_scan_at is None
            or (now - last_node_reachability_scan_at).total_seconds()
            >= settings.node_reachability_scan_interval_seconds
        ):
            def scan_node_reachability() -> None:
                try:
                    node_still_unreachable: set[str] = set()
                    current_node_reachability = node_health_monitor.check_node_reachability(
                        node_still_unreachable,
                    )
                    node_health_monitor.create_or_resolve_node_unreachable_incidents(
                        current_node_reachability, node_still_unreachable,
                    )
                except Exception:
                    logger.exception("run: node reachability scan failed")

            run_auxiliary_scan(
                f"node-reachability-{cluster_id or 'default'}", scan_node_reachability,
            )
            last_node_reachability_scan_at = now

        # 2026-08-05: Node hardware (CPU/RAM) threshold scan — own
        # independent try/except (same isolation reasoning as the volume-
        # saturation/device-health blocks above) and its OWN, much slower
        # cadence (settings.node_health_scan_interval_seconds, default 15
        # minutes) — see watcher/node_health_monitor.py's own module
        # docstring for why (a fresh SSH round trip per configured node,
        # too heavy to run on every watcher_poll_interval_seconds tick).
        if (
            last_node_health_scan_at is None
            or (now - last_node_health_scan_at).total_seconds()
            >= settings.node_health_scan_interval_seconds
        ):
            def scan_node_health() -> None:
                try:
                    node_still_over: set[str] = set()
                    current_node_resources = node_health_monitor.check_node_resources(node_still_over)
                    node_health_monitor.create_or_resolve_node_health_incidents(
                        current_node_resources, node_still_over
                    )
                except Exception:
                    logger.exception("run: node health scan failed")

            run_auxiliary_scan(
                f"node-health-{cluster_id or 'default'}", scan_node_health,
            )
            last_node_health_scan_at = now

        # 2026-08-06: BlueStore per-pool omap quick-fix, system-proposed —
        # own independent try/except (same isolation reasoning as the
        # blocks above) and its OWN slower cadence
        # (settings.bluestore_omap_scan_interval_seconds, default 15 min),
        # even though the TRIGGER data (BLUESTORE_NO_PER_POOL_OMAP's
        # presence in `health["checks"]`) is already free from this same
        # iteration's `health` above — only the osd_id->host RESOLUTION
        # step costs a fresh SSH round trip per configured OSD host (see
        # watcher/bluestore_omap_monitor.py's own docstring), which is what
        # actually needs throttling. `health` is None if query_cluster_health()
        # failed this tick — nothing to scan for, skip silently rather than
        # passing an empty/stale dict.
        if health is not None and (
            last_bluestore_omap_scan_at is None
            or (now - last_bluestore_omap_scan_at).total_seconds()
            >= settings.bluestore_omap_scan_interval_seconds
        ):
            health_for_omap = health

            def scan_bluestore_omap() -> None:
                try:
                    current_legacy_omap = bluestore_omap_monitor.check_legacy_omap_osds(
                        health_for_omap
                    )
                    bluestore_omap_monitor.create_or_resolve_bluestore_incidents(
                        current_legacy_omap
                    )
                except Exception:
                    logger.exception("run: bluestore omap scan failed")

            run_auxiliary_scan(
                f"bluestore-omap-{cluster_id or 'default'}", scan_bluestore_omap,
            )
            last_bluestore_omap_scan_at = now

        # 2026-08-07: OSD latency outlier scan — own independent try/except
        # (same isolation reasoning as the blocks above) and its OWN, much
        # SHORTER cadence (settings.osd_latency_scan_interval_seconds,
        # default 60s) than device_health/node_health above — `ceph osd
        # perf` is a single cheap JSON-RPC through a MON (no SSH round trip
        # at all), and a latency spike is far more transient than a slowly-
        # climbing CPU/RAM trend, so this can and should run much more
        # often — see watcher/osd_latency_monitor.py's own module docstring.
        if (
            last_osd_latency_scan_at is None
            or (now - last_osd_latency_scan_at).total_seconds()
            >= settings.osd_latency_scan_interval_seconds
        ):
            def scan_osd_latency() -> None:
                try:
                    latency_still_over: set[str] = set()
                    current_osd_latency = osd_latency_monitor.check_osd_latency_outliers(
                        latency_still_over
                    )
                    osd_latency_monitor.create_or_resolve_osd_latency_incidents(
                        current_osd_latency, latency_still_over
                    )
                except Exception:
                    logger.exception("run: osd latency scan failed")

            run_auxiliary_scan(
                f"osd-latency-{cluster_id or 'default'}", scan_osd_latency,
            )
            last_osd_latency_scan_at = now

        # 2026-08-07 (Epic 12, Story 12.1 + 12.2): CRUSH structure + OSD
        # distribution scan, plus Skew detection off that same data — own
        # independent try/except (same isolation reasoning as the blocks
        # above) and its OWN cadence (settings.crush_scan_interval_seconds).
        # Both `ceph osd crush dump` and `ceph osd df` are single cheap
        # JSON-RPC queries through a MON (no SSH round trip at all, same
        # cost class as osd_latency_monitor.py's `ceph osd perf` above) —
        # see crush_structure_monitor.py/crush_distribution_monitor.py's own
        # module docstrings for why this is ONE shared cadence, not two
        # (AD-25b). Story 12.2's crush_skew_monitor.py IS the
        # Incident/Action/Telegram-creating step of this block (AD-28) —
        # the 2 collection calls above it stay pure (no Incident of their
        # own), same split as Story 12.1 originally shipped.
        if (
            last_crush_scan_at is None
            or (now - last_crush_scan_at).total_seconds() >= settings.crush_scan_interval_seconds
        ):
            def scan_crush() -> None:
                try:
                    crush_structure_monitor.scan_and_store(cluster_id)
                    crush_distribution_monitor.sync_distribution(cluster_id)
                    # Skew detection reads back both tables from this same
                    # scan. Keep the work ordered inside one background job.
                    skew_still_over: set[str] = set()
                    current_crush_skew = crush_skew_monitor.check_crush_skew(
                        cluster_id, skew_still_over
                    )
                    crush_skew_monitor.create_or_resolve_crush_skew_incidents(
                        current_crush_skew, skew_still_over
                    )
                except Exception:
                    logger.exception("run: crush structure/distribution/skew scan failed")

            run_auxiliary_scan(
                f"crush-{cluster_id or 'default'}", scan_crush,
            )
            last_crush_scan_at = now

            # Cross-layer RCA needs the current RBD header-object acting set.
            # It is deliberately a background read-only scan: one rbd info +
            # one ceph osd map per sampled object must never delay health
            # polls. Keep it on a much slower cadence because each call
            # starts a fresh cephadm shell on the MON.
            if (
                max_iterations is None
                and (
                    last_volume_topology_scan_at is None
                    or (now - last_volume_topology_scan_at).total_seconds()
                    >= settings.volume_topology_scan_interval_seconds
                )
            ):
                _run_auxiliary_scan(
                    f"volume-topology-{cluster_id or 'default'}",
                    lambda: volume_topology.collect_and_store(cluster_id, None),
                    background=True,
                )
                last_volume_topology_scan_at = now
                _run_auxiliary_scan(
                    f"performance-rca-{cluster_id or 'default'}",
                    lambda: performance_rca_monitor.check_and_alert(cluster_id, None),
                    background=True,
                )

        # Node Monitoring chart data is intentionally collected independently
        # from the slower CRUSH scan. Thirty seconds gives the browser a
        # continuous series while keeping the SSH work off the health poll.
        if (
            last_host_metrics_scan_at is None
            or (now - last_host_metrics_scan_at).total_seconds() >= 30
        ):
            _run_auxiliary_scan(
                f"host-metrics-{cluster_id or 'default'}",
                lambda: host_metrics.collect_and_store(cluster_id, None),
                background=True,
            )
            last_host_metrics_scan_at = now

        if settings.snarimax_shadow_enabled and (
            last_snarimax_shadow_scan_at is None
            or (now - last_snarimax_shadow_scan_at).total_seconds()
            >= settings.snarimax_shadow_interval_seconds
        ):
            _run_auxiliary_scan(
                f"snarimax-shadow-{cluster_id or 'default'}",
                lambda: snarimax_shadow.run_cluster_shadow(
                    cluster_id or "__default__", settings.cluster_name, now=now,
                ),
                background=True,
            )
            last_snarimax_shadow_scan_at = now

        if settings.capacity_forecast_enabled and cluster_id and (
            last_capacity_forecast_scan_at is None
            or (now - last_capacity_forecast_scan_at).total_seconds()
            >= settings.capacity_forecast_scan_interval_seconds
        ):
            def scan_capacity_forecast() -> None:
                try:
                    capacity_forecast.collect_and_store(cluster_id)
                except Exception:
                    logger.exception("run: capacity forecast collection failed")

            run_auxiliary_scan(
                f"capacity-forecast-{cluster_id}", scan_capacity_forecast,
            )
            last_capacity_forecast_scan_at = now

        # 2026-08-17 (AI roadmap Pha 0.1): Cluster capability inventory --
        # own independent try/except (same isolation reasoning as every
        # scan block above) and its OWN, much slower cadence
        # (settings.capability_inventory_scan_interval_seconds, default
        # 5 min) -- see watcher/capability_inventory.py's own module
        # docstring for why version/deployment-mode data doesn't need
        # osd_latency/crush's 60s cadence.
        if (
            last_capability_scan_at is None
            or (now - last_capability_scan_at).total_seconds()
            >= settings.capability_inventory_scan_interval_seconds
        ):
            def scan_capability_inventory() -> None:
                try:
                    capability_inventory.scan_and_store(cluster_id)
                except Exception:
                    logger.exception("run: capability inventory scan failed")

            run_auxiliary_scan(
                f"capability-inventory-{cluster_id or 'default'}",
                scan_capability_inventory,
            )
            last_capability_scan_at = now

        # 2026-08-10: ceph-aiops's OWN database size — own independent
        # try/except (same isolation reasoning as every block above) and
        # its OWN, deliberately much slower cadence
        # (settings.database_size_scan_interval_seconds, default 1h) — see
        # watcher/database_capacity_monitor.py's own module docstring for
        # why this is not on the same tick as any Ceph-facing scan (this
        # one doesn't touch Ceph at all).
        if (
            last_database_size_scan_at is None
            or (now - last_database_size_scan_at).total_seconds()
            >= settings.database_size_scan_interval_seconds
        ):
            try:
                db_size_still_over: set[str] = set()
                current_database_size = database_capacity_monitor.check_database_size(db_size_still_over)
                database_capacity_monitor.create_or_resolve_database_size_incident(
                    current_database_size, db_size_still_over
                )
            except Exception:
                logger.exception("run: database size scan failed")
            last_database_size_scan_at = now

        # 2026-08-18 (Log Intelligence L0, Plan/log-intelligence-rca-plan.md):
        # log fingerprint scan — own independent try/except (same isolation
        # reasoning as every block above) and its OWN cadence
        # (settings.log_intel_scan_interval_seconds, default 15 min).
        # Deliberately the SLOWEST Ceph-facing scan here: it is the only one
        # that opens a fresh SSH round trip PER DAEMON TYPE PER NODE and
        # pulls thousands of lines each time, so it must never share the 60s
        # cadence of the cheap MON JSON-RPC queries above. Gated by
        # settings.log_intel_enabled (default False) — an operator opts in
        # before this starts reading whole log windows off every node.
        # prune_old_rows() runs on the same tick rather than its own: it is a
        # cheap bounded DELETE, and tying it here guarantees retention can
        # never lag behind ingestion (see that function's own docstring for
        # why the observations table specifically needs this).
        if settings.log_intel_enabled and (
            last_log_intel_scan_at is None
            or (now - last_log_intel_scan_at).total_seconds()
            >= settings.log_intel_scan_interval_seconds
        ):
            def scan_logs() -> None:
                try:
                    log_intel.scan_and_store(cluster_id)
                    log_intel.prune_old_rows()
                except Exception:
                    logger.exception("run: log intelligence scan failed")
            _run_auxiliary_scan(
                f"log-intel-{cluster_id or 'default'}", scan_logs,
                background=max_iterations is None,
            )
            last_log_intel_scan_at = now

        # Learning retention is independent from Log Intelligence and must
        # continue even when log collection is disabled. The helper has its
        # own process-wide cadence guard, so this remains cheap on each poll.
        learning_retention.prune_old_rows()

        iterations += 1
        time.sleep(max(0, settings.watcher_poll_interval_seconds))


def _build_and_publish_incident_for_observed_cluster(cluster: Cluster, health: dict) -> None:
    """Multi-cluster observability's incident-creation path for any cluster
    OTHER than the default one — deliberately NOT a call into
    `build_and_publish_incident` above (that one is wired to the
    default-cluster-only BLUESTORE_NO_PER_POOL_OMAP special-case that
    `watcher/bluestore_omap_monitor.py` owns the lifecycle for; no such
    specialized monitor runs for an observed cluster yet, so a check
    matching that prefix here still takes the generic path below instead of
    being silently skipped — see build_and_publish_incident's own skip
    comment for why that split exists).

    2026-08-10 (multi-tenant remediation Phase 1): DOES now call
    `collector.collect_relevant_logs(ceph_code, check_detail, cluster=cluster)`
    — every function in watcher/collector.py this touches was made
    cluster-parameterized specifically for this call site, so log collection
    runs against THIS cluster's own nodes/creds/exec-mode, never the
    default's. `nodes`/`log_excerpt` are no longer always empty/None.
    """
    current_status = health.get("status")
    current_checks = health.get("checks", {})
    if current_status not in PROBLEM_STATUSES:
        return

    # Same restart/deduplication guard as build_and_publish_incident() for
    # the default cluster.  The observed-cluster path used to omit it, so
    # every Watcher restart could create another open row and the hourly
    # reminder then sent every duplicate in one burst.
    with db.SessionLocal() as session:
        already_open_codes = {
            row.ceph_code
            for row in session.query(Incident.ceph_code)
            .filter(
                Incident.cluster_id == cluster.id,
                Incident.status.in_(_IN_FLIGHT_DEDUPE_STATUSES),
            )
            .all()
        }
        # Keep the observed-cluster lifecycle consistent with the default
        # cluster: a recent FAILED attempt must not spam alerts, but it must
        # become eligible again after the configured cooldown.
        already_open_codes.update(
            _recent_failed_incident_codes(session, cluster.id, utc_now())
        )

    envelopes = []
    for ceph_code, check_detail in current_checks.items():
        if ceph_code in already_open_codes:
            continue
        detected_at = utc_now()
        osd_host_map: dict[int, str] = {}
        nodes, log_excerpt = collector.collect_relevant_logs(
            ceph_code, check_detail, cluster=cluster, osd_host_map=osd_host_map
        )
        signal_evidence_json = capacity_evidence.collect_capacity_evidence(
            ceph_code, check_detail, cluster=cluster
        )
        log_excerpt = _append_capacity_context(log_excerpt, signal_evidence_json)
        ceph_check_muted = _ceph_check_is_muted(check_detail)
        notification_event_id = None

        with db.SessionLocal() as session:
            incident = Incident(
                ceph_code=ceph_code,
                status=IncidentStatus.NEW.value,
                detected_at=detected_at,
                log_excerpt=log_excerpt,
                severity=check_detail.get("severity"),
                cluster_id=cluster.id,
                signal_evidence_json=signal_evidence_json,
            )
            session.add(incident)
            try:
                session.flush()
                incident_id = incident.id
                notification_muted = alert_lifecycle.inherit_active_mute(
                    session, incident, now=detected_at,
                )
                if ceph_check_muted:
                    notification_muted = True
                if not notification_muted:
                    notification_event_id = telegram_outbox.enqueue_incident_alert(
                        session, incident,
                    )
                envelope = publisher.build_envelope(
                    incident_id=incident_id,
                    ceph_code=ceph_code,
                    detected_at=detected_at.isoformat(),
                    nodes=nodes,
                    log_excerpt=log_excerpt,
                    cluster_snapshot=health,
                    cluster_id=cluster.id,
                    ssh_user=cluster.ssh_user,
                    ssh_key_path=cluster.ssh_key_path,
                    ceph_exec_mode=cluster.ceph_exec_mode,
                    ceph_container_name=cluster.ceph_container_name,
                    osd_hosts=osd_host_map,
                )
                event_id = incident_outbox.enqueue(
                    session, incident_id=incident_id, payload=envelope,
                )
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if not _is_inflight_incident_duplicate(exc):
                    logger.exception(
                        "_build_and_publish_incident_for_observed_cluster: unexpected "
                        "integrity error for cluster_id=%s ceph_code=%s",
                        cluster.id,
                        ceph_code,
                    )
                    raise
                logger.info(
                    "_build_and_publish_incident_for_observed_cluster: duplicate "
                    "in-flight incident skipped for cluster_id=%s ceph_code=%s",
                    cluster.id,
                    ceph_code,
                )
                continue
        if ceph_check_muted:
            logger.info(
                "cluster %s: %s bị mute trong Ceph; tạo Incident nhưng không gửi Telegram",
                cluster.id,
                ceph_code,
            )

        if notification_event_id:
            telegram_outbox.dispatch_due(
                limit=1,
                event_ids=[notification_event_id],
            )
        envelopes.append((event_id, envelope))

    if not envelopes:
        return
    try:
        asyncio.run(_publish_all(envelopes))
    except Exception:
        logger.exception(
            "_build_and_publish_incident_for_observed_cluster: failed to publish %d "
            "incident(s) for cluster %r to RabbitMQ",
            len(envelopes),
            cluster.name,
        )


def _refresh_active_observed_cluster(cluster_id: str) -> Cluster | None:
    """Load the latest observed-cluster configuration for each poll.

    Cluster connection fields are editable in the Dashboard.  Observed-cluster
    threads are long-lived, so retaining the detached object passed at startup
    would keep polling/logging old node IPs until the Watcher was restarted.
    """
    with db.SessionLocal() as session:
        current = session.get(Cluster, cluster_id)
        if current is None or not current.is_active:
            return None
        session.expunge(current)
        return current


def _reconcile_observed_cluster_threads(
    active_clusters: list[Cluster],
    thread_registry: dict[str, tuple[threading.Thread, threading.Event]],
    *,
    thread_factory: Callable[..., threading.Thread] = threading.Thread,
) -> None:
    """Start newly-active observed clusters and forget stopped ones.

    The observed loop itself re-reads its Cluster row every poll and exits
    when the row becomes inactive/deleted.  This reconciliation layer owns
    only thread membership, which keeps add/reactivate operations idempotent
    and avoids a second loop during the small hand-off window after a stop.
    ``thread_factory`` is injectable so this lifecycle policy can be tested
    without starting real polling threads.
    """
    active_by_id = {
        cluster.id: cluster
        for cluster in active_clusters
        if not cluster.is_default
    }

    for cluster_id, (thread, stop_event) in list(thread_registry.items()):
        if cluster_id not in active_by_id:
            stop_event.set()
        if cluster_id not in active_by_id and not thread.is_alive():
            thread_registry.pop(cluster_id, None)

    for cluster_id, cluster in active_by_id.items():
        existing = thread_registry.get(cluster_id)
        if existing is not None and existing[0].is_alive():
            # The row may have been re-enabled before the old loop observed
            # the stop request. Let that still-live loop continue rather than
            # forcing an unnecessary stop/start gap.
            existing[1].clear()
            continue
        stop_event = threading.Event()
        thread = thread_factory(
            target=run_observed_cluster_loop,
            args=(cluster,),
            kwargs={"stop_event": stop_event},
            name=f"watcher-cluster-{cluster.name}",
            daemon=True,
        )
        thread_registry[cluster_id] = (thread, stop_event)
        thread.start()
        logger.info(
            "run_all_clusters: started observed-cluster loop for %r (id=%s)",
            cluster.name,
            cluster.id,
        )


def _run_observed_cluster_supervisor() -> None:
    """Keep observed-cluster loop membership aligned with active DB rows."""
    thread_registry: dict[str, tuple[threading.Thread, threading.Event]] = {}
    while True:
        try:
            with db.SessionLocal() as session:
                active_clusters = list_active_clusters(session)
                session.expunge_all()
            _reconcile_observed_cluster_threads(active_clusters, thread_registry)
        except Exception:
            # A transient DB failure must not kill the supervisor. Existing
            # loops continue their own safe refresh/query cycle; the next
            # discovery tick retries membership reconciliation.
            logger.exception("run_all_clusters: observed-cluster discovery failed")
        time.sleep(_CLUSTER_DISCOVERY_INTERVAL_SECONDS)


def run_observed_cluster_loop(
    cluster: Cluster,
    max_iterations: Optional[int] = None,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    """Multi-cluster observability Phase 1: the loop for any cluster OTHER
    than the default one — core health poll + Incident creation + heartbeat,
    plus the read-only CRUSH structure/distribution and RBD volume-performance
    collectors. Deliberately does NOT run device_health/node_health/
    bluestore_omap/osd_latency/crush-skew — those secondary monitors
    are coupled to the global `settings` singleton the
    same way watcher/collector.py is (see
    `_build_and_publish_incident_for_observed_cluster`'s docstring), and
    parameterizing all of them is explicitly deferred to a later phase (see
    the sibling architecture doc's "Explicitly deferred" section).

    Runs in its own background thread (started by `run_all_clusters()`) —
    NOT via asyncio, matching this whole module's existing synchronous/
    blocking style (`time.sleep`, not `await asyncio.sleep`); a thread per
    additional cluster is the natural fit here rather than converting this
    entire module to async for Phase 1.
    """
    last_status: Optional[str] = None
    last_checks: frozenset = frozenset()
    last_crush_scan_at: Optional[datetime] = None
    last_host_metrics_scan_at: Optional[datetime] = None
    last_snarimax_shadow_scan_at: Optional[datetime] = None
    last_volume_scan_at: Optional[datetime] = None
    last_volume_topology_scan_at: Optional[datetime] = None
    last_trash_capacity_scan_at: Optional[datetime] = None
    last_rgw_alert_scan_at: Optional[datetime] = None
    last_capacity_forecast_scan_at: Optional[datetime] = None
    last_capability_scan_at: Optional[datetime] = None
    last_log_intel_scan_at: Optional[datetime] = None
    last_inventory_scan_at: Optional[datetime] = None
    last_status_snapshot_scan_at: Optional[datetime] = None
    last_priority_refresh_marker: str | None = None
    last_health_status_sent_at: Optional[datetime] = None
    iterations = 0

    def run_auxiliary_scan(name: str, callback: Callable[[], None]) -> bool:
        """Keep bounded/test loops free of orphan daemon scans.

        The production observed-cluster loop is unbounded and can safely
        dispatch slow collectors in daemon threads. A bounded invocation is
        a deterministic probe used by tests and diagnostics; it must not
        leave a callback running after its database fixture has moved on.
        """
        if max_iterations is None:
            return _run_auxiliary_scan(name, callback, background=True)
        callback()
        return True

    while max_iterations is None or iterations < max_iterations:
        if stop_event is not None and stop_event.is_set():
            logger.info("run_observed_cluster_loop: stop requested for cluster id=%s", cluster.id)
            return
        poll_started_monotonic = time.monotonic()
        refreshed_cluster = _refresh_active_observed_cluster(cluster.id)
        if refreshed_cluster is None:
            logger.info("run_observed_cluster_loop: cluster id=%s is inactive or removed; stopping thread", cluster.id)
            return
        cluster = refreshed_cluster
        if stop_event is not None and stop_event.is_set():
            return
        mon_nodes = [h.strip() for h in cluster.ceph_mon_nodes.split(",") if h.strip()]
        try:
            poll_started_at = cluster_snapshot_collector.collection_timestamp()
            with cluster_snapshot_collector.health_collection_lock(cluster.id):
                try:
                    health = query_cluster_health_with(
                        mon_nodes,
                        cluster.ceph_container_name,
                        cluster.ssh_user,
                        cluster.ssh_key_path,
                        cluster.ceph_exec_mode,
                        # Never overwrite the DEFAULT cluster's sticky MON-node
                        # fallback (watcher/ceph_client.py::query_cluster_health_with's
                        # own docstring) — this is a DIFFERENT cluster's poll.
                        update_sticky_fallback=False,
                    )
                except CephQueryError as exc:
                    try:
                        cluster_snapshot_collector.publish_health_error(cluster.id, exc)
                    except Exception:
                        logger.exception(
                            "run_observed_cluster_loop(%r): failed to record health snapshot error",
                            cluster.name,
                        )
                    raise
                try:
                    cluster_snapshot_collector.publish_health_snapshot(
                        cluster.id,
                        health,
                        collection_started_monotonic=poll_started_monotonic,
                        collection_started_at=poll_started_at,
                    )
                except Exception:
                    logger.exception(
                        "run_observed_cluster_loop(%r): failed to publish critical health snapshot",
                        cluster.name,
                    )
            # mon_node=None on success (unlike the default loop, which
            # passes ceph_client.last_successful_mon_node) — that global is
            # deliberately not updated for this cluster (see above), so
            # there is no "which host answered" value available here
            # without a more invasive change to query_cluster_health_with's
            # return type. A small loss of detail on this cluster's
            # heartbeat row, not a correctness issue.
            _record_heartbeat_safe(True, None, None, cluster_id=cluster.id)
            current_status = health.get("status")
            current_checks = frozenset(health.get("checks", {}).keys())
            status_now = utc_now()
            if (
                last_health_status_sent_at is None
                or (status_now - last_health_status_sent_at).total_seconds()
                >= settings.telegram_health_status_interval_seconds
            ):
                health_status = current_status or "UNKNOWN"
                health_codes = sorted(current_checks)
                bucket = int(status_now.timestamp()) // max(
                    60, settings.telegram_health_status_interval_seconds
                )
                fingerprint = hashlib.sha1(
                    ",".join(health_codes).encode("utf-8"),
                    usedforsecurity=False,
                ).hexdigest()[:16]
                telegram_outbox.enqueue_alert_call_and_dispatch(
                    event_id=f"health-status:{cluster.id}:{bucket}:{health_status}:{fingerprint}",
                    category="incident",
                    function="send_periodic_health_status",
                    args=(health_status, health_codes),
                    cluster_id=str(cluster.id),
                    cluster_name=cluster.name,
                    sender=telegram_alerts.send_periodic_health_status,
                )
                last_health_status_sent_at = status_now
            _resolve_recovered_incidents(
                set(current_checks), cluster_id=cluster.id, include_legacy_null=False
            )
            try:
                verify.verify_pending_incidents(
                    set(current_checks), health=health, cluster=cluster, cluster_id=cluster.id
                )
            except Exception:
                logger.exception(
                    "run_observed_cluster_loop: xác minh sau khắc phục thất bại cho %r",
                    cluster.name,
                )
            if current_status != last_status or current_checks != last_checks:
                _build_and_publish_incident_for_observed_cluster(cluster, health)
                last_status = current_status
                last_checks = current_checks

            if stop_event is not None and stop_event.is_set():
                return
            volume_now = utc_now()
            if (
                last_volume_scan_at is None
                or (volume_now - last_volume_scan_at).total_seconds()
                >= settings.volume_scan_interval_seconds
            ):
                try:
                    current_saturated = volume_monitor.check_volumes(cluster=cluster)
                    volume_monitor.persist_last_poll_metrics(cluster_id=cluster.id)
                    volume_monitor.create_or_resolve_volume_incidents(
                        current_saturated, cluster_id=cluster.id, include_legacy_null=False
                    )
                except Exception:
                    logger.exception(
                        "run_observed_cluster_loop(%r): volume saturation check failed", cluster.name
                    )
                last_volume_scan_at = volume_now

            if stop_event is not None and stop_event.is_set():
                return
            trash_now = utc_now()
            if (
                last_trash_capacity_scan_at is None
                or (trash_now - last_trash_capacity_scan_at).total_seconds()
                >= getattr(settings, "trash_capacity_scan_interval_seconds", 300)
            ):
                run_auxiliary_scan(
                    f"trash-{cluster.id}",
                    lambda: trash_capacity_monitor.check_and_alert(cluster),
                )
                last_trash_capacity_scan_at = trash_now

            rgw_now = utc_now()
            if (
                last_rgw_alert_scan_at is None
                or (rgw_now - last_rgw_alert_scan_at).total_seconds()
                >= rgw_alerting.RGW_ALERT_SCAN_INTERVAL_SECONDS
            ):
                run_auxiliary_scan(
                    f"rgw-alerts-{cluster.id}",
                    lambda: rgw_alerting.scan_and_alert(cluster.id),
                )
                last_rgw_alert_scan_at = rgw_now

            now = utc_now()
            if stop_event is not None and stop_event.is_set():
                return
            if (
                last_crush_scan_at is None
                or (now - last_crush_scan_at).total_seconds() >= settings.crush_scan_interval_seconds
            ):
                try:
                    crush_structure_monitor.scan_and_store(cluster.id, cluster=cluster)
                    crush_distribution_monitor.sync_distribution(cluster.id, cluster=cluster)
                    if (
                        last_volume_topology_scan_at is None
                        or (now - last_volume_topology_scan_at).total_seconds()
                        >= settings.volume_topology_scan_interval_seconds
                    ):
                        run_auxiliary_scan(
                            f"volume-topology-{cluster.id}",
                            lambda: volume_topology.collect_and_store(cluster.id, cluster),
                        )
                        last_volume_topology_scan_at = now
                    run_auxiliary_scan(
                        f"performance-rca-{cluster.id}",
                        lambda: performance_rca_monitor.check_and_alert(cluster.id, cluster),
                    )
                except Exception:
                    logger.exception(
                        "run_observed_cluster_loop(%r): CRUSH scan failed", cluster.name
                    )
                last_crush_scan_at = now

            # Keep observed-cluster Node Monitoring history independent from
            # the slower five-minute CRUSH scan.
            if (
                last_host_metrics_scan_at is None
                or (now - last_host_metrics_scan_at).total_seconds() >= 30
            ):
                run_auxiliary_scan(
                    f"host-metrics-{cluster.id}",
                    lambda: host_metrics.collect_and_store(cluster.id, cluster),
                )
                last_host_metrics_scan_at = now

            if settings.snarimax_shadow_enabled and (
                last_snarimax_shadow_scan_at is None
                or (now - last_snarimax_shadow_scan_at).total_seconds()
                >= settings.snarimax_shadow_interval_seconds
            ):
                run_auxiliary_scan(
                    f"snarimax-shadow-{cluster.id}",
                    lambda: snarimax_shadow.run_cluster_shadow(
                        cluster.id, cluster.name, now=now,
                    ),
                )
                last_snarimax_shadow_scan_at = now

            if settings.capacity_forecast_enabled and (
                stop_event is None or not stop_event.is_set()
            ) and (
                last_capacity_forecast_scan_at is None
                or (now - last_capacity_forecast_scan_at).total_seconds()
                >= settings.capacity_forecast_scan_interval_seconds
            ):
                try:
                    capacity_forecast.collect_and_store(cluster.id, cluster=cluster)
                except Exception:
                    logger.exception(
                        "run_observed_cluster_loop(%r): capacity forecast collection failed", cluster.name
                    )
                last_capacity_forecast_scan_at = now

            if (
                stop_event is None or not stop_event.is_set()
            ) and (
                last_capability_scan_at is None
                or (now - last_capability_scan_at).total_seconds()
                >= settings.capability_inventory_scan_interval_seconds
            ):
                try:
                    capability_inventory.scan_and_store(cluster.id, cluster=cluster)
                except Exception:
                    logger.exception(
                        "run_observed_cluster_loop(%r): capability inventory scan failed", cluster.name
                    )
                last_capability_scan_at = now

            # 2026-08-18 (Log Intelligence L0) — same cadence/gating as the
            # default loop's own block. prune_old_rows() is deliberately NOT
            # repeated here: it is cluster-agnostic (one global cutoff sweep
            # over both tables), so running it once per tick in the default
            # loop already covers every observed cluster's rows too — calling
            # it per observed cluster would be N identical redundant DELETEs.
            if settings.log_intel_enabled and (
                stop_event is None or not stop_event.is_set()
            ) and (
                last_log_intel_scan_at is None
                or (now - last_log_intel_scan_at).total_seconds()
                >= settings.log_intel_scan_interval_seconds
            ):
                try:
                    log_intel.scan_and_store(cluster.id, cluster=cluster)
                except Exception:
                    logger.exception(
                        "run_observed_cluster_loop(%r): log intelligence scan failed", cluster.name
                    )
                last_log_intel_scan_at = now
            learning_retention.prune_old_rows()
        except CephQueryError as exc:
            _record_heartbeat_safe(False, None, str(exc), cluster_id=cluster.id)
            logger.warning("run_observed_cluster_loop(%r): %s", cluster.name, exc)
        except Exception:
            _record_heartbeat_safe(False, None, "unexpected error", cluster_id=cluster.id)
            logger.exception("run_observed_cluster_loop(%r): unexpected error during poll iteration", cluster.name)

        if max_iterations is None:
            status_now = utc_now()
            priority = read_priority_refresh(cluster.id)
            priority_marker = str(
                priority.get("request_id") or priority.get("requested_at", "")
            ) if priority else ""
            priority_due = bool(priority_marker and priority_marker != last_priority_refresh_marker)
            if (
                last_status_snapshot_scan_at is None
                or (status_now - last_status_snapshot_scan_at).total_seconds()
                >= _DASHBOARD_STATUS_SNAPSHOT_INTERVAL_SECONDS
                or priority_due
            ):
                status_scan_started = run_auxiliary_scan(
                    f"status-{cluster.id}",
                    lambda status_cluster=cluster: cluster_snapshot_collector.collect_and_publish_status(
                        status_cluster
                    ),
                )
                last_status_snapshot_scan_at = status_now

            inventory_now = utc_now()
            if (
                last_inventory_scan_at is None
                or (inventory_now - last_inventory_scan_at).total_seconds()
                >= settings.dashboard_inventory_poll_interval_seconds
                or priority_due
            ):
                def scan_inventory(inventory_cluster=cluster) -> None:
                    try:
                        cluster_snapshot_collector.collect_and_publish_inventory(inventory_cluster)
                    except Exception:
                        logger.exception(
                            "run_observed_cluster_loop(%r): inventory snapshot collection failed",
                            inventory_cluster.name,
                        )

                inventory_scan_started = run_auxiliary_scan(f"inventory-{cluster.id}", scan_inventory)
                last_inventory_scan_at = inventory_now
            if priority_due and status_scan_started and inventory_scan_started:
                last_priority_refresh_marker = priority_marker

        iterations += 1
        if stop_event is not None:
            stop_event.wait(max(0, settings.watcher_poll_interval_seconds))
        else:
            time.sleep(max(0, settings.watcher_poll_interval_seconds))


def run_all_clusters() -> None:
    """Real production entrypoint (multi-cluster observability Phase 1):
    resolves the default cluster (seeding it from `.env` on first run, same
    as Dashboard/Worker — see shared/clusters.py::ensure_default_cluster),
    starts one background supervisor that tracks additional ACTIVE `Cluster`
    rows, then runs the default cluster's own `run()` loop — unchanged in every way except tagging its writes
    with the default cluster's real id — on the main thread (blocking, so
    the process exits if and only if the default loop ever returns, same
    as before this function existed)."""
    global _WATCHER_PROCESS_LOCK_HANDLE
    _WATCHER_PROCESS_LOCK_HANDLE = _acquire_watcher_process_lock()
    if _WATCHER_PROCESS_LOCK_HANDLE is None:
        logger.error("run_all_clusters: another Watcher process is already running or lock is unavailable")
        return
    with db.SessionLocal() as session:
        default_cluster_id = get_default_cluster_id(session)

    vitastor_thread = threading.Thread(
        target=vitastor_monitor.run_all_clusters_loop,
        name="watcher-vitastor",
        daemon=True,
    )
    vitastor_thread.start()
    logger.info("run_all_clusters: started dynamic Vitastor monitoring loop")

    observed_supervisor_thread = threading.Thread(
        target=_run_observed_cluster_supervisor,
        name="watcher-cluster-supervisor",
        daemon=True,
    )
    observed_supervisor_thread.start()
    logger.info("run_all_clusters: started observed-cluster discovery supervisor")

    run(
        # Default-cluster AI incident creation is owned exclusively by
        # watcher.remediation_main.  Keeping it here too leaves a race where
        # both processes pass the DB dedupe query before either inserts.
        on_transition=default_on_transition,
        cluster_id=default_cluster_id,
    )


if __name__ == "__main__":
    # A leading "%(asctime)s" is what lets the Settings page's log-cleanup
    # feature (dashboard/routes/maintenance.py) actually filter by date —
    # without it, log lines have no per-entry timestamp to filter on at all.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    run_all_clusters()
