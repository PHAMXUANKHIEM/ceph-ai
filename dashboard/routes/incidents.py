import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.chat import CHAT_REQUEST_CEPH_CODE
from dashboard.routes.delete_cluster import CLUSTER_DELETE_CEPH_CODE
from dashboard.routes.deploy_cluster import CLUSTER_DEPLOY_CEPH_CODE
from dashboard.routes.upgrade import CLUSTER_UPGRADE_CEPH_CODE, is_cluster_upgrade_pending_or_approved
from dashboard.templating import make_templates
from shared import db, heartbeat
from shared.kill_switch import is_kill_switch_enabled, set_kill_switch
from shared.models import Action, ActionStatus, AuditEntry, BackupJob, Incident, IncidentStatus, WatcherHeartbeat

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# AC #2: "quá lâu chưa có poll mới" threshold — a multiple of the poll
# interval rather than a fixed number of seconds, so it scales with
# whatever watcher_poll_interval_seconds is configured to.
HEARTBEAT_STALE_MULTIPLIER = 3

# Incident statuses that mean "still needs attention" — anything else
# (RESOLVED / AUTO_FIXED / REJECTED) is considered closed for status purposes.
OPEN_STATUSES = {
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    IncidentStatus.FAILED.value,
}


def compute_cluster_status(incidents: list[Incident], heartbeat_stale: bool) -> str:
    """Derive an aggregate cluster status from stored Incidents.

    The Dashboard itself never queries Ceph directly — Watcher (Story 1.3)
    does that over SSH and writes real health transitions here as Incident
    rows. This function only aggregates what's already recorded, so it's as
    fresh as Watcher's last poll (settings.watcher_poll_interval_seconds),
    not literally real-time.

    "No open incidents" alone does not mean the cluster is healthy — it
    also happens when Watcher has never successfully reached the cluster at
    all, so there is no real health data to aggregate yet (0 rows in
    `incidents` either way, indistinguishable without extra context). Only
    report "OK" when the heartbeat confirms Watcher has actually reached the
    cluster recently (`not heartbeat_stale`); otherwise report "UNKNOWN"
    rather than defaulting to a reassuring "OK" with no evidence behind it.
    Real recorded incidents (WARN/ERR below) are historical fact regardless
    of current heartbeat staleness, so they're unaffected by this check.

    2026-07-23 fix #1: `dashboard/routes/chat.py::confirm_chat_action` creates
    a synthetic Incident (ceph_code=CHAT_REQUEST_CEPH_CODE) for every
    chat-confirmed action, purely so it can reuse the existing Action/
    kill-switch/audit pipeline (see that function's docstring) — it is NOT
    evidence of real Ceph cluster health. Before this fix, a chat action
    that failed for an unrelated reason (e.g. a bad parameter, or — as
    actually happened — the Worker process running stale code) flipped
    Incident.status to FAILED and this function reported the WHOLE cluster
    as "ERR", even though `ceph health` was fine the entire time. Excluded
    here so only Watcher-detected incidents ever drive this aggregate — a
    failed chat action is still fully visible via its own Action row/audit
    trail, just not conflated with cluster health.

    Same reasoning applies to `dashboard/routes/upgrade.py`'s synthetic
    Incident (ceph_code=CLUSTER_UPGRADE_CEPH_CODE) — an upgrade proposal
    that's rejected, or whose `ceph orch upgrade start` command itself
    fails to send, must not flip the cluster-wide health badge to "ERR"
    either; it's the same kind of "our own pipeline's outcome", not a real
    `ceph health` signal.

    2026-07-25: and to `dashboard/routes/deploy_cluster.py`'s synthetic
    Incident (ceph_code=CLUSTER_DEPLOY_CEPH_CODE) — building a BRAND-NEW
    cluster that isn't even monitored yet must never be conflated with the
    health of whatever cluster IS currently configured/monitored; a failed
    deploy attempt is visible via its own Action row/audit trail only.

    2026-07-26: and to `dashboard/routes/delete_cluster.py`'s synthetic
    Incident (ceph_code=CLUSTER_DELETE_CEPH_CODE) — a failed/rejected
    delete proposal must not itself flip the cluster-health badge; the
    ACTUAL health impact of a successful deletion shows up naturally once
    the cluster is gone (heartbeat_stale/no more incidents), not via this
    synthetic row.

    2026-07-23 fix #2: this used to derive ERR from
    `Incident.status == FAILED` — i.e. "did OUR remediation attempt fail",
    not "is the cluster actually in HEALTH_ERR". Those are different
    things: a plain HEALTH_WARN check (e.g. POOL_APP_NOT_ENABLED) whose
    recommended action had no automated fix (investigate_manually) or
    whose fix genuinely failed would still get reported as cluster-wide
    "ERR", contradicting `ceph health`'s own real HEALTH_WARN status — and
    the page's own copy (index.html) explicitly promises this badge
    reflects "tình trạng CỦA CLUSTER (vd HEALTH_WARN/HEALTH_ERR thật)", not
    remediation outcome. Watcher already records Ceph's own real per-check
    severity on every Incident it creates (`Incident.severity`, from
    `checks[code]["severity"]" — see watcher/main.py::
    build_and_publish_incident) — that is the correct, authoritative signal
    to use instead. A remediation's success/failure is still fully visible
    via the Action row/pending-approval section/audit trail; it no longer
    overrides the cluster-health badge.
    """
    real_incidents = [
        i
        for i in incidents
        if i.ceph_code
        not in (
            CHAT_REQUEST_CEPH_CODE,
            CLUSTER_UPGRADE_CEPH_CODE,
            CLUSTER_DEPLOY_CEPH_CODE,
            CLUSTER_DELETE_CEPH_CODE,
        )
    ]
    open_incidents = [i for i in real_incidents if i.status in OPEN_STATUSES]
    if not open_incidents:
        return "UNKNOWN" if heartbeat_stale else "OK"
    if any(i.severity == "HEALTH_ERR" for i in open_incidents):
        return "ERR"
    return "WARN"


def is_heartbeat_stale(latest_heartbeat: WatcherHeartbeat | None) -> bool:
    """AC #2/#3: "mất kết nối cụm" is a SEPARATE signal from
    compute_cluster_status() (which only reflects recorded Incident data —
    i.e. "is the cluster healthy"). This answers "can Watcher currently
    reach the cluster at all" — true (stale/lost) when Watcher has never
    completed a poll, the last poll failed, or the last poll is old enough
    that Watcher may have silently died."""
    if latest_heartbeat is None:
        return True
    if not latest_heartbeat.success:
        return True
    age = datetime.utcnow() - latest_heartbeat.polled_at
    return age > timedelta(seconds=HEARTBEAT_STALE_MULTIPLIER * settings.watcher_poll_interval_seconds)


# Epic 9, Story 9.4 (AC #2): how far back a FAILED BackupJob still counts
# as an active alert — an old failure that's since been superseded by a
# later success shouldn't keep the banner lit forever.
BACKUP_ALERT_LOOKBACK_HOURS = 24


def _recent_backup_failure(session) -> BackupJob | None:
    """Simple, self-contained Dashboard signal (AC #2) — deliberately does
    NOT read worker/policy/backup_policy.yaml's tracked_images (AD-3:
    dashboard/ must not import worker/backup/ execution code, and
    policy_config.py exists purely to serve that code); just asks "is
    there any BackupJob that failed recently". The fuller, policy-aware
    "never backed up"/"overdue past RPO" check that also fires the
    outbound webhook lives in worker/backup/alerting.py's periodic job —
    this is only the at-a-glance Dashboard banner, same scope as
    is_heartbeat_stale()'s single-condition check above."""
    cutoff = datetime.utcnow() - timedelta(hours=BACKUP_ALERT_LOOKBACK_HOURS)
    return (
        session.query(BackupJob)
        .filter(BackupJob.status == "FAILED", BackupJob.created_at >= cutoff)
        .order_by(BackupJob.created_at.desc())
        .first()
    )


def _parse_datetime_filter(raw: str) -> datetime | None:
    """Accepts the value an HTML <input type="datetime-local"> submits
    (`YYYY-MM-DDTHH:MM`). Returns None for blank/unparseable input rather
    than raising — an invalid filter should be silently ignored (show
    everything), not 500 the whole page."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _query_audit_entries(
    session, incident_id: str, since_dt: datetime | None, until_dt: datetime | None
) -> list[AuditEntry]:
    query = session.query(AuditEntry)
    if incident_id:
        query = query.filter(AuditEntry.incident_id == incident_id)
    if since_dt is not None:
        query = query.filter(AuditEntry.created_at >= since_dt)
    if until_dt is not None:
        query = query.filter(AuditEntry.created_at <= until_dt)
    return query.order_by(AuditEntry.created_at.desc()).all()


def _fetch_dashboard_data(
    incident_id: str, since_dt: datetime | None, until_dt: datetime | None
) -> tuple[
    list[Incident],
    WatcherHeartbeat | None,
    bool,
    list[Action],
    list[AuditEntry],
    bool,
    BackupJob | None,
]:
    with db.SessionLocal() as session:
        incidents = session.query(Incident).order_by(Incident.detected_at.desc()).all()
        latest_heartbeat = heartbeat.get_latest(session)
        kill_switch_enabled = is_kill_switch_enabled(session)
        # Cheap DB-only signal (no SSH) for disabling "Duyệt" on every OTHER
        # pending risky action while a cluster upgrade is proposed/approved —
        # see dashboard/routes/upgrade.py::is_cluster_upgrade_pending_or_approved
        # for what this does and does NOT cover (the window after the Worker
        # has already sent `ceph orch upgrade start` is deliberately not
        # checked here — that would need a live SSH call on every Dashboard
        # page load; the authoritative gate for THAT window lives in
        # dashboard/routes/actions.py::approve_action instead).
        upgrade_blocks_other_actions = is_cluster_upgrade_pending_or_approved(session)
        # 2026-07-23 restore: a RISKY Action — whether from the auto-diagnosis
        # pipeline OR a chat-confirmed proposal (dashboard/routes/chat.py::
        # confirm_chat_action, same Action/Incident state machine) — lands
        # here and STAYS here until POST /actions/{id}/approve|reject is hit.
        # Between 5a29d4e (removed this card, assuming Chat-with-AI's confirm
        # click was itself sufficient) and this fix, nothing in the UI ever
        # called those endpoints for a chat-originated RISKY action, so it
        # (e.g. restart_osd_daemon, still `risky:` in action_policy.yaml)
        # sat in PENDING_APPROVAL forever — visibly proposed, silently never
        # run, with no operator-facing indication anything further was
        # needed.
        pending_actions = (
            session.query(Action)
            .filter_by(status=ActionStatus.PENDING_APPROVAL.value)
            .order_by(Action.created_at.desc())
            .all()
        )
        # Audit Trail (filters + full history) now lives directly on the
        # Dashboard — there is no separate /audit page anymore.
        audit_entries = _query_audit_entries(session, incident_id, since_dt, until_dt)
        # Epic 9, Story 9.4 (AC #2) — see _recent_backup_failure's docstring.
        backup_alert = _recent_backup_failure(session)
    return (
        incidents,
        latest_heartbeat,
        kill_switch_enabled,
        pending_actions,
        audit_entries,
        upgrade_blocks_other_actions,
        backup_alert,
    )


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    user: str = Depends(require_login),
    incident_id: str = "",
    since: str = "",
    until: str = "",
):
    incident_id = incident_id.strip()
    since_dt = _parse_datetime_filter(since.strip())
    until_dt = _parse_datetime_filter(until.strip())
    try:
        (
            incidents,
            latest_heartbeat,
            kill_switch_enabled,
            pending_actions,
            audit_entries,
            upgrade_blocks_other_actions,
            backup_alert,
        ) = _fetch_dashboard_data(incident_id, since_dt, until_dt)
        # Kept inside the same try as the fetch (Review Story 5.2) — these
        # derive directly from just-fetched DB data, so any failure here
        # (e.g. a malformed row) should surface the same friendly error,
        # not an unhandled 500 that a caller-facing except SQLAlchemyError
        # alone wouldn't catch.
        stale = is_heartbeat_stale(latest_heartbeat)
        status = compute_cluster_status(incidents, stale)
        # Incidents already fetched are looked up by id (in-memory, no extra
        # query) for the pending-approval section — every PENDING_APPROVAL
        # Action has a corresponding row in `incidents` since both derive
        # from the same DB snapshot.
        incidents_by_id = {incident.id: incident for incident in incidents}
        pending_actions_with_incident = [
            (action, incidents_by_id.get(action.incident_id)) for action in pending_actions
        ]
    except SQLAlchemyError:
        logger.exception("index: failed to query incidents from DB")
        raise HTTPException(
            status_code=503,
            detail="Không kết nối được database — đã chạy `alembic upgrade head` chưa?",
        )
    except Exception:
        # Any other failure while preparing the page (e.g. a bug in
        # compute_cluster_status/is_heartbeat_stale) must not leak a raw
        # 500/stack trace to the browser either.
        logger.exception("index: failed to prepare dashboard page")
        raise HTTPException(status_code=500, detail="Lỗi khi tải trang — xem log server để biết chi tiết")
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "status": status,
            "incidents": incidents,
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "heartbeat": latest_heartbeat,
            "heartbeat_stale": stale,
            "cluster_mon_nodes": settings.ceph_mon_nodes,
            "cluster_container_name": settings.ceph_container_name,
            "cluster_exec_mode": settings.ceph_exec_mode,
            "kill_switch_enabled": kill_switch_enabled,
            "pending_actions": pending_actions_with_incident,
            "audit_entries": audit_entries,
            "filter_incident_id": incident_id,
            "filter_since": since,
            "filter_until": until,
            "upgrade_blocks_other_actions": upgrade_blocks_other_actions,
            "backup_alert": backup_alert,
            # Sidebar tab (2026-07-24) — lands on Audit Trail if the operator
            # just used its filter form (a GET with query params, unlike
            # Settings' POST-result sections), otherwise defaults to Chờ
            # duyệt (the most actionable tab).
            "active_tab": "audit" if (incident_id or since or until) else "pending",
        },
    )


@router.post("/kill-switch", response_class=HTMLResponse)
async def kill_switch_submit(
    request: Request, user: str = Depends(require_login), enabled: str = Form(...)
):
    """Story 4.1: the Dashboard's emergency kill-switch — Worker re-reads
    this fresh before every remediation command (AD-4, NFR2), so flipping
    it here takes effect immediately, no restart needed. `enabled` is a
    literal "true"/"false" hidden form field (see templates/index.html) —
    the same button always posts the OPPOSITE of the currently-displayed
    state, so a stale/double-submitted page can't silently re-apply a value
    the operator no longer intends.
    """
    with db.SessionLocal() as session:
        set_kill_switch(session, enabled.strip().lower() == "true")
        session.commit()
    return RedirectResponse(url="/", status_code=303)
