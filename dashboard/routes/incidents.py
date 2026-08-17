import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.chat import CHAT_REQUEST_CEPH_CODE
from dashboard.routes.delete_cluster import CLUSTER_DELETE_CEPH_CODE
from dashboard.routes.deploy_cluster import CLUSTER_DEPLOY_CEPH_CODE
from dashboard.routes.upgrade import CLUSTER_UPGRADE_CEPH_CODE, is_cluster_upgrade_pending_or_approved
from dashboard.telegram_approval_bot import channels_for_incident, has_configured_channel
from dashboard.templating import make_templates
from shared import db, heartbeat
from shared.clusters import ensure_default_cluster, list_active_clusters
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import Action, ActionStatus, AuditEntry, BackupJob, Cluster, Incident, IncidentStatus, WatcherHeartbeat
from shared.object_storage_cache import get_or_load
from watcher.ceph_client import CephQueryError, run_ceph_json_command_with

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# AC #2: "quá lâu chưa có poll mới" threshold — a multiple of the poll
# interval rather than a fixed number of seconds, so it scales with
# whatever watcher_poll_interval_seconds is configured to.
HEARTBEAT_STALE_MULTIPLIER = 3


def _cached_monitor_command(cluster_id: str, mon_nodes, container_name: str, ssh_user: str,
                            ssh_key_path: str, exec_mode: str, command: str):
    return get_or_load(
        "monitor",
        f"{cluster_id}:{command}",
        lambda: run_ceph_json_command_with(
            mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode, command
        ),
        ttl_seconds=300,
    )

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

def _recent_backup_failure_for_cluster(
    session, cluster_id: str, is_default_cluster: bool
) -> BackupJob | None:
    """Latest failure for exactly the cluster currently being viewed."""
    cutoff = datetime.utcnow() - timedelta(hours=BACKUP_ALERT_LOOKBACK_HOURS)
    cluster_filter = (
        or_(BackupJob.cluster_id == cluster_id, BackupJob.cluster_id.is_(None))
        if is_default_cluster
        else BackupJob.cluster_id == cluster_id
    )
    return (
        session.query(BackupJob)
        .filter(BackupJob.status == "FAILED", BackupJob.created_at >= cutoff, cluster_filter)
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
    session,
    incident_id: str,
    since_dt: datetime | None,
    until_dt: datetime | None,
    cluster_id: str,
    is_default_cluster: bool,
) -> list[AuditEntry]:
    cluster_filter = (
        or_(Incident.cluster_id == cluster_id, Incident.cluster_id.is_(None))
        if is_default_cluster
        else Incident.cluster_id == cluster_id
    )
    query = session.query(AuditEntry).join(Incident, AuditEntry.incident_id == Incident.id).filter(cluster_filter)
    if incident_id:
        query = query.filter(AuditEntry.incident_id == incident_id)
    if since_dt is not None:
        query = query.filter(AuditEntry.created_at >= since_dt)
    if until_dt is not None:
        query = query.filter(AuditEntry.created_at <= until_dt)
    return query.order_by(AuditEntry.created_at.desc()).all()


def _fetch_dashboard_data(
    incident_id: str,
    since_dt: datetime | None,
    until_dt: datetime | None,
    cluster_id: str,
    is_default_cluster: bool,
    cluster_names_by_id: dict[str, str],
) -> tuple[
    list[Incident],
    WatcherHeartbeat | None,
    list[tuple[Action, Incident | None, str, bool]],
    list[AuditEntry],
    bool,
    BackupJob | None,
    bool,
    bool,
]:
    with db.SessionLocal() as session:
        # Every cluster-owned feed is scoped to the current selection.
        # Pre-migration Incident
        # rows have `cluster_id IS NULL`, which means "the default cluster"
        # (Incident's own docstring) — only match those when the SELECTED
        # cluster IS the default one.
        incident_cluster_filter = (
            or_(Incident.cluster_id == cluster_id, Incident.cluster_id.is_(None))
            if is_default_cluster
            else Incident.cluster_id == cluster_id
        )
        incidents = (
            session.query(Incident)
            .filter(incident_cluster_filter)
            .order_by(Incident.detected_at.desc())
            .all()
        )
        latest_heartbeat = heartbeat.get_latest(session, cluster_id)
        # Cheap DB-only signal (no SSH) for disabling "Duyệt" on every OTHER
        # pending risky action while a cluster upgrade is proposed/approved —
        # see dashboard/routes/upgrade.py::is_cluster_upgrade_pending_or_approved
        # for what this does and does NOT cover (the window after the Worker
        # has already sent `ceph orch upgrade start` is deliberately not
        # checked here — that would need a live SSH call on every Dashboard
        # page load; the authoritative gate for THAT window lives in
        # dashboard/routes/actions.py::approve_action instead).
        upgrade_blocks_other_actions = (
            is_cluster_upgrade_pending_or_approved(session) if is_default_cluster else False
        )
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
        pending_actions_rows = (
            session.query(Action)
            .join(Incident, Action.incident_id == Incident.id)
            .filter(incident_cluster_filter)
            .filter(Action.status == ActionStatus.PENDING_APPROVAL.value)
            .order_by(Action.created_at.desc())
            .all()
        )
        other_cluster_filter = (
            Incident.cluster_id.notin_([cluster_id])
            if is_default_cluster
            else or_(Incident.cluster_id != cluster_id, Incident.cluster_id.is_(None))
        )
        has_other_cluster_pending = (
            session.query(Action.id)
            .join(Incident, Action.incident_id == Incident.id)
            .filter(Action.status == ActionStatus.PENDING_APPROVAL.value, other_cluster_filter)
            .first()
            is not None
        )
        # Resolve only the selected cluster's pending action incidents.
        pending_incident_ids = {a.incident_id for a in pending_actions_rows}
        pending_incidents_by_id = (
            {
                row.id: row
                for row in session.query(Incident).filter(Incident.id.in_(pending_incident_ids)).all()
            }
            if pending_incident_ids
            else {}
        )
        pending_actions = []
        for action in pending_actions_rows:
            pending_incident = pending_incidents_by_id.get(action.incident_id)
            cluster_label = ""
            if pending_incident is not None and pending_incident.cluster_id is not None:
                cluster_label = cluster_names_by_id.get(pending_incident.cluster_id, "")
            # 2026-08-10 (multi-tenant remediation Phase 2): whether THIS
            # action actually has a reachable Telegram channel — a
            # non-default cluster with no channel of its own is NOT covered
            # by the 3 global channels anymore (channels_for_incident
            # narrows, doesn't add) — see index()'s own use of this to keep
            # the "Chờ duyệt" card from ever stranding such an action, the
            # exact bug class docs/telegram-alerts.md mục 6.7 already
            # documents having happened once before.
            telegram_covered = bool(channels_for_incident(pending_incident, session))
            pending_actions.append((action, pending_incident, cluster_label, telegram_covered))
        # Audit Trail (filters + full history) now lives directly on the
        # Dashboard — there is no separate /audit page anymore.
        audit_entries = _query_audit_entries(
            session, incident_id, since_dt, until_dt, cluster_id, is_default_cluster
        )
        # Epic 9, Story 9.4 (AC #2) — see _recent_backup_failure's docstring.
        backup_alert = _recent_backup_failure_for_cluster(session, cluster_id, is_default_cluster)
    # 2026-08-07: the "Chờ duyệt — Risky Action" card is only shown as a
    # FALLBACK now that dashboard/telegram_approval_bot.py broadcasts the
    # same proposal (with Duyệt/Từ chối buttons) to every configured
    # Telegram channel — see docs/telegram-alerts.md mục 6. Do NOT remove
    # this card outright: if no Telegram channel is configured (or it's
    # been cleared since), this is the ONLY remaining place to
    # approve/reject an auto-diagnosed or Chat-with-AI-confirmed RISKY
    # Action — the exact stranding bug test_dashboard_actions.py::
    # test_index_shows_pending_action_card's docstring already describes
    # from 2026-07-23, before Telegram approval existed.
    telegram_configured = has_configured_channel()
    return (
        incidents,
        latest_heartbeat,
        pending_actions,
        audit_entries,
        upgrade_blocks_other_actions,
        backup_alert,
        telegram_configured,
        has_other_cluster_pending,
    )


def _resolve_selected_cluster(requested_cluster_id: str, session_cluster_id: str = "") -> tuple[list[Cluster], Cluster]:
    """Multi-cluster observability Phase 1's cluster switcher — `?cluster=`
    on `/` (a plain query param, same pattern this file already uses for
    incident_id/since/until — bookmarkable, and every existing link/form on
    this page keeps working unchanged since it's additive).

    2026-08-11: `?cluster=` alone isn't enough to make a choice actually
    "stick" — nothing else in the app (nav links, the brand link back to
    `/`) ever forwards it, so leaving /volumes, /nodes etc. via the nav bar
    and coming back to Dashboard silently lost the selection and landed
    back on the default cluster even though the picker still LOOKED
    selected on cluster 2. `session_cluster_id` (backed by
    `request.session["selected_cluster_id"]`, set below in index()) is the
    fallback once the query param itself is blank — same signed-cookie
    session already used for login (dashboard/app.py's SessionMiddleware),
    not a new mechanism. The query param still wins when present, so the
    picker/bookmarked links behave exactly as before.

    Falls back to the default cluster when both are blank, unknown, or
    deactivated — a stale bookmarked link (or session pointing at a
    since-deactivated cluster) must not 404/500, it should just land back
    on the default cluster."""
    # Kept as a compatibility wrapper for existing imports/tests; the
    # dependency-free implementation belongs in dashboard.cluster_scope.
    from dashboard.cluster_scope import resolve_cluster_selection

    return resolve_cluster_selection(requested_cluster_id, session_cluster_id)


def _dashboard_health_payload(
    status: dict,
    cluster: Cluster,
    osd_perf: dict | None = None,
    cluster_nodes: dict | list | None = None,
    osd_dump: dict | None = None,
) -> dict:
    """Convert one authoritative ceph status response into card values."""
    health = status.get("health") if isinstance(status.get("health"), dict) else {}
    osdmap = status.get("osdmap") if isinstance(status.get("osdmap"), dict) else {}
    monmap = status.get("monmap") if isinstance(status.get("monmap"), dict) else {}
    pgmap = status.get("pgmap") if isinstance(status.get("pgmap"), dict) else {}

    mons = monmap.get("mons") if isinstance(monmap.get("mons"), list) else []
    mon_total = monmap.get("num_mons") if isinstance(monmap.get("num_mons"), int) else len(mons)
    quorum = status.get("quorum_names")
    if not isinstance(quorum, list):
        quorum = status.get("quorum") if isinstance(status.get("quorum"), list) else []

    bytes_used = pgmap.get("bytes_used")
    bytes_total = pgmap.get("bytes_total")
    utilization = None
    if isinstance(bytes_used, (int, float)) and isinstance(bytes_total, (int, float)) and bytes_total > 0:
        utilization = round(bytes_used * 100 / bytes_total)

    health_value = str(health.get("status") or "UNKNOWN").removeprefix("HEALTH_")
    pools = osdmap.get("num_pools")
    if not isinstance(pools, int):
        pools = pgmap.get("num_pools") if isinstance(pgmap.get("num_pools"), int) else None

    pg_states = pgmap.get("pgs_by_state") if isinstance(pgmap.get("pgs_by_state"), list) else []
    pg_okay = bool(pg_states) and all(
        isinstance(row, dict) and set(str(row.get("state_name") or "").split("+")) <= {"active", "clean"}
        for row in pg_states
    )

    perf_rows = []
    if isinstance(osd_perf, dict):
        candidate = osd_perf.get("osd_perf_infos")
        if isinstance(candidate, list):
            perf_rows = candidate
    latency_values = []
    for row in perf_rows:
        if not isinstance(row, dict):
            continue
        perf = row.get("perf_stats") if isinstance(row.get("perf_stats"), dict) else row
        for key in ("apply_latency_ms", "commit_latency_ms"):
            value = perf.get(key)
            if isinstance(value, (int, float)):
                latency_values.append(float(value))

    online_hosts: set[str] = set()
    server_total = len(configured_nodes(cluster))
    if isinstance(cluster_nodes, list):
        # `ceph orch host ls`: an empty status means online; offline/error
        # hosts carry an explicit status string.
        for row in cluster_nodes:
            if not isinstance(row, dict):
                continue
            hostname = str(row.get("hostname") or row.get("host") or "").strip()
            status_text = str(row.get("status") or "").strip().lower()
            if hostname and status_text not in {"offline", "maintenance", "error"}:
                online_hosts.add(hostname)
        server_total = max(server_total, len(cluster_nodes))
    elif isinstance(cluster_nodes, dict):
        # `ceph node ls` works for both legacy and cephadm clusters. Its
        # shape is role -> hostname -> daemon-id list.
        for values in cluster_nodes.values():
            if isinstance(values, dict):
                online_hosts.update(str(host) for host in values if host)
            elif isinstance(values, list):
                online_hosts.update(str(value) for value in values if value)

    read_bps = pgmap.get("read_bytes_sec")
    write_bps = pgmap.get("write_bytes_sec")
    read_ops = pgmap.get("read_op_per_sec")
    write_ops = pgmap.get("write_op_per_sec")
    bandwidth_bps = sum(value for value in (read_bps, write_bps) if isinstance(value, (int, float)))
    iops = sum(value for value in (read_ops, write_ops) if isinstance(value, (int, float)))

    osd_total = osdmap.get("num_osds")
    osd_up = osdmap.get("num_up_osds")
    dump_rows = osd_dump.get("osds") if isinstance(osd_dump, dict) else None
    if isinstance(dump_rows, list):
        valid_rows = [row for row in dump_rows if isinstance(row, dict) and "osd" in row]
        if valid_rows:
            osd_total = len(valid_rows)
            osd_up = sum(1 for row in valid_rows if row.get("up") in (1, True, "1"))

    return {
        "health": health_value,
        "osds": {"up": osd_up, "total": osd_total},
        "mons": {"up": len(quorum), "total": mon_total},
        "servers": {"online": len(online_hosts) if cluster_nodes is not None else None, "total": server_total},
        "utilization": {
            "percent": utilization,
            "bytes_used": bytes_used if isinstance(bytes_used, (int, float)) else None,
            "pools": pools,
        },
        "metrics": {
            "latency_ms": round(sum(latency_values) / len(latency_values), 2) if latency_values else None,
            "bandwidth_bps": bandwidth_bps,
            "iops": iops,
        },
        "placement_groups": "OKAY" if pg_okay else "WARN",
    }


@router.get("/api/dashboard/health")
async def dashboard_health(request: Request, _user: str = Depends(require_login)):
    """Return live card data for the cluster selected in this session."""
    selected_cluster = None
    try:
        _clusters, selected_cluster = _resolve_selected_cluster(
            request.query_params.get("cluster", "").strip(),
            request.session.get("selected_cluster_id", ""),
        )
        mon_nodes = [node.strip() for node in selected_cluster.ceph_mon_nodes.split(",") if node.strip()]
        if not mon_nodes:
            raise CephQueryError("Cụm chưa cấu hình MON node")
        ssh_user, ssh_key_path, exec_mode, container_name = resolve_ssh_creds(selected_cluster)
        _host, payload = await asyncio.to_thread(
            _cached_monitor_command,
            selected_cluster.id,
            mon_nodes,
            container_name,
            ssh_user,
            ssh_key_path,
            exec_mode,
            "ceph -s",
        )
        if not isinstance(payload, dict):
            raise CephQueryError("ceph -s returned an unexpected response")
        osd_perf = None
        osd_dump = None
        cluster_nodes = None
        try:
            _host, osd_perf = await asyncio.to_thread(
                _cached_monitor_command, selected_cluster.id, mon_nodes, container_name, ssh_user,
                ssh_key_path, exec_mode, "ceph osd perf",
            )
        except Exception as exc:
            logger.info("dashboard_health: osd latency unavailable: %s", exc)
        try:
            _host, osd_dump = await asyncio.to_thread(
                _cached_monitor_command, selected_cluster.id, mon_nodes, container_name, ssh_user,
                ssh_key_path, exec_mode, "ceph osd dump",
            )
        except Exception as exc:
            logger.info("dashboard_health: detailed OSD state unavailable: %s", exc)
        node_commands = ("ceph orch host ls", "ceph node ls") if exec_mode == "cephadm" else ("ceph node ls",)
        for node_command in node_commands:
            try:
                _host, cluster_nodes = await asyncio.to_thread(
                    _cached_monitor_command, selected_cluster.id, mon_nodes, container_name, ssh_user,
                    ssh_key_path, exec_mode, node_command,
                )
                break
            except Exception as exc:
                logger.info("dashboard_health: server inventory unavailable via %s: %s", node_command, exc)
        return _dashboard_health_payload(payload, selected_cluster, osd_perf, cluster_nodes, osd_dump)
    except CephQueryError as exc:
        cluster_name = selected_cluster.name if selected_cluster is not None else "đã chọn"
        logger.warning("dashboard_health(%s): live Ceph query failed: %s", cluster_name, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    user: str = Depends(require_login),
    incident_id: str = "",
    since: str = "",
    until: str = "",
    cluster: str = "",
):
    incident_id = incident_id.strip()
    since_dt = _parse_datetime_filter(since.strip())
    until_dt = _parse_datetime_filter(until.strip())
    try:
        # Inside the try (not resolved before it) — this hits the DB same
        # as everything else _fetch_dashboard_data does below, and must
        # fail the same clean 503 way if the DB is unreachable, not an
        # unhandled 500 from before the try block even started.
        clusters, selected_cluster = _resolve_selected_cluster(
            cluster.strip(), request.session.get("selected_cluster_id", "")
        )
        # Persist whatever we landed on (explicit ?cluster=, prior session
        # value, or the default-cluster fallback) so the NEXT visit to `/`
        # with no query param — e.g. clicking "Dashboard" in the nav bar
        # after navigating to /volumes — resumes on this cluster instead of
        # silently resetting to the default one (see _resolve_selected_
        # cluster's docstring above).
        request.session["selected_cluster_id"] = selected_cluster.id
        cluster_names_by_id = {c.id: c.name for c in clusters}
        (
            incidents,
            latest_heartbeat,
            pending_actions_with_incident,
            audit_entries,
            upgrade_blocks_other_actions,
            backup_alert,
            telegram_configured,
            has_other_cluster_pending,
        ) = _fetch_dashboard_data(
            incident_id, since_dt, until_dt, selected_cluster.id, selected_cluster.is_default, cluster_names_by_id
        )
        # Kept inside the same try as the fetch (Review Story 5.2) — these
        # derive directly from just-fetched DB data, so any failure here
        # (e.g. a malformed row) should surface the same friendly error,
        # not an unhandled 500 that a caller-facing except SQLAlchemyError
        # alone wouldn't catch.
        stale = is_heartbeat_stale(latest_heartbeat)
        status = compute_cluster_status(incidents, stale)
        # 2026-08-10 (multi-tenant remediation Phase 2): the "Chờ duyệt" card
        # used to hide the instant the 3 GLOBAL channels were configured —
        # now that a non-default cluster's own channel can NARROW coverage
        # instead of the global ones always covering everything, that alone
        # would strand an uncovered action (the "Chat-with-AI confirm was
        # assumed sufficient" bug docs/telegram-alerts.md mục 6.7 already
        # describes fixing once). Force the card visible whenever at least
        # one pending action isn't actually covered by any reachable
        # Telegram channel, even if the global 3 are configured — default
        # single-cluster behavior (every action always default-cluster-
        # covered) is unchanged.
        show_pending_card = not telegram_configured or any(
            not covered for _, _, _, covered in pending_actions_with_incident
        )
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
            "cluster_mon_nodes": selected_cluster.ceph_mon_nodes,
            "cluster_container_name": selected_cluster.ceph_container_name,
            "cluster_exec_mode": selected_cluster.ceph_exec_mode,
            "clusters": clusters,
            "selected_cluster": selected_cluster,
            "pending_actions": pending_actions_with_incident,
            "audit_entries": audit_entries,
            "filter_incident_id": incident_id,
            "filter_since": since,
            "filter_until": until,
            "upgrade_blocks_other_actions": upgrade_blocks_other_actions,
            "backup_alert": backup_alert,
            # 2026-08-07: the "Chờ duyệt" card only renders when Telegram
            # approval doesn't already cover every pending action (see
            # show_pending_card's own comment above, and
            # _fetch_dashboard_data's docstring) — Duyệt/Từ chối reaches a
            # covered RISKY Action's Telegram channel regardless of where it
            # originated; the card stays as the fallback for anything that
            # isn't reachable that way.
            "telegram_approval_configured": not show_pending_card,
            "has_other_cluster_pending": has_other_cluster_pending,
            # Sidebar tab (2026-07-24) — lands on Audit Trail if the operator
            # just used its filter form (a GET with query params, unlike
            # Settings' POST-result sections), otherwise defaults to Chờ
            # duyệt (the most actionable tab) when that card exists, else
            # Incident Feed.
            "active_tab": (
                "audit" if (incident_id or since or until)
                else "pending" if show_pending_card
                else "incidents"
            ),
        },
    )
