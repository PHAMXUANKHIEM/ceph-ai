import asyncio
import logging
import time
from datetime import datetime
from typing import Callable, Optional

from config.settings import settings
from watcher import (
    bluestore_omap_monitor,
    ceph_client,
    collector,
    device_health_monitor,
    node_health_monitor,
    osd_latency_monitor,
    publisher,
    volume_monitor,
)
from watcher.bluestore_omap_monitor import BLUESTORE_OMAP_PREFIX
from watcher.ceph_client import CephQueryError, query_cluster_health
from watcher.device_health_monitor import DEVICE_HEALTH_EVACUATE_PREFIX
from watcher.node_health_monitor import NODE_RESOURCE_HIGH_PREFIX
from watcher.osd_latency_monitor import OSD_LATENCY_HIGH_PREFIX
from watcher.volume_monitor import VOLUME_SATURATED_PREFIX
from shared import db, heartbeat
from shared.models import Incident, IncidentStatus
from shared.telegram_alerts import send_incident_alert

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


def _resolve_recovered_incidents(current_codes: set[str]) -> None:
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
    """
    with db.SessionLocal() as session:
        open_incidents = (
            session.query(Incident).filter(Incident.status.in_(_RECOVERABLE_STATUSES)).all()
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
            if incident.ceph_code not in current_codes:
                incident.status = IncidentStatus.RESOLVED.value
        session.commit()


def build_and_publish_incident(previous_status: Optional[str], health: dict) -> None:
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
    """
    current_status = health.get("status")
    current_checks = health.get("checks", {})

    if current_status not in PROBLEM_STATUSES:
        return

    envelopes = []
    for ceph_code, check_detail in current_checks.items():
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
        nodes, log_excerpt = collector.collect_relevant_logs(ceph_code, check_detail)

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
            )
            session.add(incident)
            session.commit()
            session.refresh(incident)
            incident_id = incident.id

        # 2026-08-05: "cảnh báo lỗi cụm" Telegram channel (shared/
        # telegram_alerts.py) — its own independent bot token/chat id
        # (telegram_incident_bot_token/telegram_incident_chat_id), checked
        # inside send_incident_alert itself. One message per ceph_code check,
        # matching the one-Incident-per-check granularity above. Best-
        # effort/never raises (see that module's own docstring) — placed
        # OUTSIDE the DB session block above on purpose, same "don't hold a
        # DB connection open across unrelated network I/O" reasoning this
        # function's own docstring already gives for log collection.
        send_incident_alert(ceph_code, check_detail.get("severity"), log_excerpt)

        envelopes.append(
            publisher.build_envelope(
                incident_id=incident_id,
                ceph_code=ceph_code,
                detected_at=detected_at.isoformat(),
                nodes=nodes,
                log_excerpt=log_excerpt,
                cluster_snapshot=health,
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
    success: bool, mon_node: Optional[str], error_message: Optional[str]
) -> None:
    """Story 5.2: records the outcome of EVERY poll attempt (success or
    failure), independent of whether `on_transition` fires — this is a
    separate signal ("is Watcher able to reach the cluster at all") from
    Incident data ("is the cluster healthy"). A failure to record the
    heartbeat itself must never kill the poll loop — that would defeat the
    whole point of this monitoring process."""
    try:
        with db.SessionLocal() as session:
            heartbeat.record(
                session,
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
    """
    last_status: Optional[str] = None
    last_checks: frozenset = frozenset()
    last_device_health_scan_at: Optional[datetime] = None
    last_node_health_scan_at: Optional[datetime] = None
    last_bluestore_omap_scan_at: Optional[datetime] = None
    last_osd_latency_scan_at: Optional[datetime] = None
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
            _record_heartbeat_safe(True, ceph_client.last_successful_mon_node, None)
            heartbeat_recorded = True
            current_status = health.get("status")
            current_checks = frozenset(health.get("checks", {}).keys())
            # Every poll, regardless of whether the fingerprint below
            # changed — see _resolve_recovered_incidents' docstring for why
            # gating this behind "only on transition" can permanently miss
            # Incidents Worker resolves/fails on its own, later.
            _resolve_recovered_incidents(set(current_checks))
            if current_status != last_status or current_checks != last_checks:
                on_transition(last_status, health)
                last_status = current_status
                last_checks = current_checks
        except CephQueryError as exc:
            _record_heartbeat_safe(False, None, str(exc))
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
                _record_heartbeat_safe(False, None, str(exc))
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
            current_saturated = volume_monitor.check_volumes()
            volume_monitor.persist_last_poll_metrics()
            volume_monitor.create_or_resolve_volume_incidents(current_saturated)
        except Exception:
            logger.exception("run: volume saturation check failed")

        # 2026-08-01 (Story C): DeviceHealth-driven evacuation-proposal scan
        # — own independent try/except (same isolation reasoning as the
        # volume-saturation block above) and its OWN, much slower cadence
        # (settings.device_health_scan_interval_seconds, default 1h) rather
        # than running `ceph device ls`/`ceph osd dump` every single
        # watcher_poll_interval_seconds tick — see that setting's own
        # comment in config/settings.py for why.
        now = datetime.utcnow()
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
                current_node_resources = node_health_monitor.check_node_resources()
                node_health_monitor.create_or_resolve_node_health_incidents(current_node_resources)
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
                current_osd_latency = osd_latency_monitor.check_osd_latency_outliers()
                osd_latency_monitor.create_or_resolve_osd_latency_incidents(current_osd_latency)
            except Exception:
                logger.exception("run: osd latency scan failed")
            last_osd_latency_scan_at = now

        iterations += 1
        time.sleep(max(0, settings.watcher_poll_interval_seconds))


if __name__ == "__main__":
    # A leading "%(asctime)s" is what lets the Settings page's log-cleanup
    # feature (dashboard/routes/maintenance.py) actually filter by date —
    # without it, log lines have no per-entry timestamp to filter on at all.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
    run(on_transition=build_and_publish_incident)
