import asyncio
import functools
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy import or_

from config.settings import settings
from watcher import (
    bluestore_omap_monitor,
    capability_inventory,
    ceph_client,
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
    vitastor_monitor,
)
from watcher.bluestore_omap_monitor import BLUESTORE_OMAP_PREFIX
from watcher.ceph_client import CephQueryError, query_cluster_health, query_cluster_health_with
from watcher.crush_skew_monitor import CRUSH_SKEW_PG_PREFIX, CRUSH_SKEW_USE_PREFIX
from watcher.database_capacity_monitor import DATABASE_SIZE_HIGH_PREFIX
from watcher.device_health_monitor import DEVICE_HEALTH_EVACUATE_PREFIX
from watcher.node_health_monitor import NODE_RESOURCE_HIGH_PREFIX
from watcher.osd_latency_monitor import OSD_LATENCY_HIGH_PREFIX
from watcher.volume_monitor import VOLUME_SATURATED_PREFIX
from shared import db, heartbeat, telegram_alerts
from shared.incident_actions import cancel_pending_actions
from shared.clusters import get_default_cluster_id, list_active_clusters
from shared.models import Action, Cluster, Incident, IncidentStatus

logger = logging.getLogger(__name__)

OnTransition = Callable[[Optional[str], dict], None]

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


def send_due_incident_reminders(now: datetime | None = None) -> int:
    """Re-send every still-open Incident to Telegram once per configured interval."""
    now = now or datetime.utcnow()
    interval = max(60, settings.telegram_incident_reminder_interval_seconds)
    cutoff = now - timedelta(seconds=interval)
    sent = 0
    with db.SessionLocal() as session:
        open_incidents = (
            session.query(Incident)
            .filter(Incident.status.in_(_RECOVERABLE_STATUSES))
            .filter(Incident.ceph_code.notin_(_REMINDER_EXCLUDED_CODES))
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
            action = actions.get(incident.id)
            has_cluster_channel = bool(cluster and cluster.telegram_bot_token and cluster.telegram_chat_id)
            telegram_alerts.send_incident_alert(
                incident.ceph_code,
                incident.severity,
                incident.log_excerpt,
                cluster_name=cluster.name if cluster else None,
                bot_token=cluster.telegram_bot_token if has_cluster_channel else None,
                chat_id=cluster.telegram_chat_id if has_cluster_channel else None,
                enabled=cluster.telegram_enabled if has_cluster_channel else None,
                reminder=True,
                diagnosis_text=incident.diagnosis_text,
                rationale=action.rationale if action else None,
            )
            incident.telegram_reminded_at = now
            sent += 1
        session.commit()
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


async def _publish_all(envelopes: list[dict]) -> None:
    """Publish every envelope over a single AMQP connection/event loop,
    instead of a full connect+declare per incident."""
    for envelope in envelopes:
        await publisher.publish_incident(envelope)


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
    with db.SessionLocal() as session:
        cluster_filter = (
            or_(Incident.cluster_id == cluster_id, Incident.cluster_id.is_(None))
            if include_legacy_null
            else Incident.cluster_id == cluster_id
        )
        open_incidents = (
            session.query(Incident)
            .filter(Incident.status.in_(_RECOVERABLE_STATUSES), cluster_filter)
            .all()
        )
        for incident in open_incidents:
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
        session.commit()


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
    with db.SessionLocal() as session:
        query = session.query(Incident.ceph_code).filter(
            Incident.status.in_(_RECOVERABLE_STATUSES)
        )
        query = (
            query.filter(Incident.cluster_id == cluster_id)
            if cluster_id is not None
            else query.filter(Incident.cluster_id.is_(None))
        )
        already_open_codes = {row.ceph_code for row in query.all()}

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
        detected_at = datetime.utcnow()
        # 2026-08-20: dict rỗng truyền xuống làm out-param — collector điền
        # {osd_id: host} đã tra thật cho những OSD check này nêu đích danh.
        osd_host_map: dict[int, str] = {}
        nodes, log_excerpt = collector.collect_relevant_logs(
            ceph_code, check_detail, osd_host_map=osd_host_map
        )

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
            )
            session.add(incident)
            session.commit()
            session.refresh(incident)
            incident_id = incident.id

        # Alert immediately; AI diagnosis is an enrichment and must never be
        # a delivery dependency. send_incident_alert is best-effort and
        # swallows Telegram failures, so RabbitMQ publishing still proceeds.
        telegram_alerts.send_incident_alert(
            ceph_code, check_detail.get("severity"), log_excerpt
        )
        envelopes.append(
            publisher.build_envelope(
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
        )

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
                polled_at=datetime.utcnow(),
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
    last_device_health_scan_at: Optional[datetime] = None
    last_node_health_scan_at: Optional[datetime] = None
    last_bluestore_omap_scan_at: Optional[datetime] = None
    last_osd_latency_scan_at: Optional[datetime] = None
    last_crush_scan_at: Optional[datetime] = None
    last_database_size_scan_at: Optional[datetime] = None
    last_trash_capacity_scan_at: Optional[datetime] = None
    last_incident_reminder_scan_at: Optional[datetime] = None
    last_capability_scan_at: Optional[datetime] = None
    last_log_intel_scan_at: Optional[datetime] = None
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
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
            health = query_cluster_health()
            _record_heartbeat_safe(True, ceph_client.last_successful_mon_node, None, cluster_id=cluster_id)
            heartbeat_recorded = True
            current_status = health.get("status")
            current_checks = frozenset(health.get("checks", {}).keys())
            # Every poll, regardless of whether the fingerprint below
            # changed — see _resolve_recovered_incidents' docstring for why
            # gating this behind "only on transition" can permanently miss
            # Incidents Worker resolves/fails on its own, later.
            _resolve_recovered_incidents(set(current_checks), cluster_id=cluster_id)
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
        # docstring) — deliberately called every poll regardless of
        # whether anything looked saturated this cycle.
        try:
            current_saturated = volume_monitor.check_volumes(cluster_id=cluster_id)
            volume_monitor.persist_last_poll_metrics(cluster_id=cluster_id)
            volume_monitor.create_or_resolve_volume_incidents(
                current_saturated, cluster_id=cluster_id, include_legacy_null=True
            )
        except Exception:
            logger.exception("run: volume saturation check failed")

        # Aggregate Trash needs one query per RBD pool plus `ceph df`, so
        # cap it at once per minute even when the main poll is faster.
        trash_now = datetime.utcnow()
        if (
            last_trash_capacity_scan_at is None
            or (trash_now - last_trash_capacity_scan_at).total_seconds() >= 60
        ):
            try:
                trash_capacity_monitor.check_and_alert()
            except Exception:
                logger.exception("run: trash capacity check failed")
            last_trash_capacity_scan_at = trash_now

        # 2026-08-01 (Story C): DeviceHealth-driven evacuation-proposal scan
        # — own independent try/except (same isolation reasoning as the
        # volume-saturation block above) and its OWN, much slower cadence
        # (settings.device_health_scan_interval_seconds, default 1h) rather
        # than running `ceph device ls`/`ceph osd dump` every single
        # watcher_poll_interval_seconds tick — see that setting's own
        # comment in config/settings.py for why.
        now = datetime.utcnow()
        if (
            last_incident_reminder_scan_at is None
            or (now - last_incident_reminder_scan_at).total_seconds() >= 60
        ):
            try:
                send_due_incident_reminders(now)
            except Exception:
                logger.exception("run: Telegram incident reminder scan failed")
            last_incident_reminder_scan_at = now
        if (
            last_device_health_scan_at is None
            or (now - last_device_health_scan_at).total_seconds()
            >= settings.device_health_scan_interval_seconds
        ):
            try:
                current_predicted_failing = device_health_monitor.check_predicted_failing_osds()
                device_health_monitor.create_or_resolve_device_health_incidents(
                    current_predicted_failing
                )
            except Exception:
                logger.exception("run: device health scan failed")
            last_device_health_scan_at = now

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
            try:
                node_still_over: set[str] = set()
                current_node_resources = node_health_monitor.check_node_resources(node_still_over)
                node_health_monitor.create_or_resolve_node_health_incidents(
                    current_node_resources, node_still_over
                )
            except Exception:
                logger.exception("run: node health scan failed")
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
            try:
                current_legacy_omap = bluestore_omap_monitor.check_legacy_omap_osds(health)
                bluestore_omap_monitor.create_or_resolve_bluestore_incidents(current_legacy_omap)
            except Exception:
                logger.exception("run: bluestore omap scan failed")
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
            try:
                latency_still_over: set[str] = set()
                current_osd_latency = osd_latency_monitor.check_osd_latency_outliers(latency_still_over)
                osd_latency_monitor.create_or_resolve_osd_latency_incidents(
                    current_osd_latency, latency_still_over
                )
            except Exception:
                logger.exception("run: osd latency scan failed")
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
            try:
                crush_structure_monitor.scan_and_store(cluster_id)
                crush_distribution_monitor.sync_distribution(cluster_id)
                # 2026-08-07 (Epic 12, Story 12.2): Skew detection reads
                # BACK the 2 tables the calls above just wrote, on the SAME
                # tick (AD-28 — 2 independent signals, USE and PG, both
                # computed off this one shared scan). Kept in the SAME
                # try/except as the collection calls above (not a separate
                # block) so one shared failure-isolation boundary covers
                # this whole tick, same as every other scan block in this
                # function.
                skew_still_over: set[str] = set()
                current_crush_skew = crush_skew_monitor.check_crush_skew(cluster_id, skew_still_over)
                crush_skew_monitor.create_or_resolve_crush_skew_incidents(
                    current_crush_skew, skew_still_over
                )
            except Exception:
                logger.exception("run: crush structure/distribution/skew scan failed")
            last_crush_scan_at = now

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
            try:
                capability_inventory.scan_and_store(cluster_id)
            except Exception:
                logger.exception("run: capability inventory scan failed")
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
            try:
                log_intel.scan_and_store(cluster_id)
                log_intel.prune_old_rows()
            except Exception:
                logger.exception("run: log intelligence scan failed")
            last_log_intel_scan_at = now

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
                Incident.status.in_(_RECOVERABLE_STATUSES),
            )
            .all()
        }

    envelopes = []
    for ceph_code, check_detail in current_checks.items():
        if ceph_code in already_open_codes:
            continue
        detected_at = datetime.utcnow()
        nodes, log_excerpt = collector.collect_relevant_logs(ceph_code, check_detail, cluster=cluster)

        with db.SessionLocal() as session:
            incident = Incident(
                ceph_code=ceph_code,
                status=IncidentStatus.NEW.value,
                detected_at=detected_at,
                log_excerpt=log_excerpt,
                severity=check_detail.get("severity"),
                cluster_id=cluster.id,
            )
            session.add(incident)
            session.commit()
            session.refresh(incident)
            incident_id = incident.id

        has_cluster_channel = bool(cluster.telegram_bot_token and cluster.telegram_chat_id)
        telegram_alerts.send_incident_alert(
            ceph_code,
            check_detail.get("severity"),
            log_excerpt,
            cluster_name=cluster.name,
            bot_token=cluster.telegram_bot_token if has_cluster_channel else None,
            chat_id=cluster.telegram_chat_id if has_cluster_channel else None,
            enabled=cluster.telegram_enabled if has_cluster_channel else None,
        )
        envelopes.append(
            publisher.build_envelope(
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
            )
        )

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


def run_observed_cluster_loop(cluster: Cluster, max_iterations: Optional[int] = None) -> None:
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
    last_capability_scan_at: Optional[datetime] = None
    last_log_intel_scan_at: Optional[datetime] = None
    mon_nodes = [h.strip() for h in cluster.ceph_mon_nodes.split(",") if h.strip()]
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
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
            _resolve_recovered_incidents(
                set(current_checks), cluster_id=cluster.id, include_legacy_null=False
            )
            if current_status != last_status or current_checks != last_checks:
                _build_and_publish_incident_for_observed_cluster(cluster, health)
                last_status = current_status
                last_checks = current_checks

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

            now = datetime.utcnow()
            if (
                last_crush_scan_at is None
                or (now - last_crush_scan_at).total_seconds() >= settings.crush_scan_interval_seconds
            ):
                try:
                    crush_structure_monitor.scan_and_store(cluster.id, cluster=cluster)
                    crush_distribution_monitor.sync_distribution(cluster.id, cluster=cluster)
                except Exception:
                    logger.exception(
                        "run_observed_cluster_loop(%r): CRUSH scan failed", cluster.name
                    )
                last_crush_scan_at = now

            if (
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
        except CephQueryError as exc:
            _record_heartbeat_safe(False, None, str(exc), cluster_id=cluster.id)
            logger.warning("run_observed_cluster_loop(%r): %s", cluster.name, exc)
        except Exception:
            _record_heartbeat_safe(False, None, "unexpected error", cluster_id=cluster.id)
            logger.exception("run_observed_cluster_loop(%r): unexpected error during poll iteration", cluster.name)

        iterations += 1
        time.sleep(max(0, settings.watcher_poll_interval_seconds))


def run_all_clusters() -> None:
    """Real production entrypoint (multi-cluster observability Phase 1):
    resolves the default cluster (seeding it from `.env` on first run, same
    as Dashboard/Worker — see shared/clusters.py::ensure_default_cluster),
    starts one background thread per additional ACTIVE `Cluster` row
    running `run_observed_cluster_loop`, then runs the default cluster's
    own `run()` loop — unchanged in every way except tagging its writes
    with the default cluster's real id — on the main thread (blocking, so
    the process exits if and only if the default loop ever returns, same
    as before this function existed)."""
    with db.SessionLocal() as session:
        default_cluster_id = get_default_cluster_id(session)
        observed_clusters = [c for c in list_active_clusters(session) if not c.is_default]
        session.expunge_all()

    vitastor_thread = threading.Thread(
        target=vitastor_monitor.run_all_clusters_loop,
        name="watcher-vitastor",
        daemon=True,
    )
    vitastor_thread.start()
    logger.info("run_all_clusters: started dynamic Vitastor monitoring loop")

    for cluster in observed_clusters:
        thread = threading.Thread(
            target=run_observed_cluster_loop, args=(cluster,), name=f"watcher-cluster-{cluster.name}", daemon=True
        )
        thread.start()
        logger.info("run_all_clusters: started observed-cluster loop for %r (id=%s)", cluster.name, cluster.id)

    run(
        on_transition=functools.partial(build_and_publish_incident, cluster_id=default_cluster_id),
        cluster_id=default_cluster_id,
    )


if __name__ == "__main__":
    # A leading "%(asctime)s" is what lets the Settings page's log-cleanup
    # feature (dashboard/routes/maintenance.py) actually filter by date —
    # without it, log lines have no per-entry timestamp to filter on at all.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    run_all_clusters()
