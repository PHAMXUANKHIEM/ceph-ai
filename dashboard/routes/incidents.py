import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from dashboard.routes.audit import recent_audit_entries
from dashboard.routes.auth import require_login
from dashboard.routes.chat import CHAT_REQUEST_CEPH_CODE
from dashboard.templating import make_templates
from shared import db, heartbeat
from shared.kill_switch import is_kill_switch_enabled, set_kill_switch
from shared.models import AuditEntry, Incident, IncidentStatus, WatcherHeartbeat

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
    real_incidents = [i for i in incidents if i.ceph_code != CHAT_REQUEST_CEPH_CODE]
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


def _fetch_dashboard_data() -> tuple[list[Incident], WatcherHeartbeat | None, bool, list[AuditEntry]]:
    with db.SessionLocal() as session:
        incidents = session.query(Incident).order_by(Incident.detected_at.desc()).all()
        latest_heartbeat = heartbeat.get_latest(session)
        kill_switch_enabled = is_kill_switch_enabled(session)
        # Dashboard-page preview of the same /audit page (Story: "move Audit
        # Trail onto the Dashboard") — recent_audit_entries() is the exact
        # same query audit_trail() itself runs unfiltered, just capped, so
        # both pages stay backed by one data source instead of two.
        audit_entries = recent_audit_entries(session)
    return (incidents, latest_heartbeat, kill_switch_enabled, audit_entries)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, user: str = Depends(require_login)):
    try:
        (incidents, latest_heartbeat, kill_switch_enabled, audit_entries) = _fetch_dashboard_data()
        # Kept inside the same try as the fetch (Review Story 5.2) — these
        # derive directly from just-fetched DB data, so any failure here
        # (e.g. a malformed row) should surface the same friendly error,
        # not an unhandled 500 that a caller-facing except SQLAlchemyError
        # alone wouldn't catch.
        stale = is_heartbeat_stale(latest_heartbeat)
        status = compute_cluster_status(incidents, stale)
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
            "heartbeat": latest_heartbeat,
            "heartbeat_stale": stale,
            "cluster_mon_nodes": settings.ceph_mon_nodes,
            "cluster_container_name": settings.ceph_container_name,
            "cluster_exec_mode": settings.ceph_exec_mode,
            "kill_switch_enabled": kill_switch_enabled,
            "audit_entries": audit_entries,
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
