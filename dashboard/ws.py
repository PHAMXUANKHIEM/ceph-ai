import asyncio
import logging
import re
from time import monotonic
from copy import deepcopy
from threading import Lock

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import func, or_

from config.settings import settings
from dashboard.cluster_authorization import CAPABILITY_READ, has_cluster_capability
from dashboard.routes.auth import _session_is_valid
from shared import db
from shared.cluster_events import read_latest_event
from shared.cluster_snapshot import section_snapshot_fingerprint, snapshot_fingerprint
from shared.clusters import ensure_default_cluster, list_active_clusters
from shared.models import Incident
from shared.request_context import reset_request_id, set_request_id
from shared.realtime_observability import record_query

router = APIRouter()
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2
WS_POLICY_VIOLATION = 1008
_METRICS_LOCK = Lock()
_METRICS = {
    "cluster_state_connections_total": 0,
    "cluster_state_messages_total": 0,
    "cluster_state_disconnects_total": 0,
    "cluster_state_policy_rejections_total": 0,
    "cluster_state_connections_open": 0,
    "cluster_state_reconnect_total": 0,
    "cluster_state_events_action_total": 0,
    "cluster_state_events_snapshot_total": 0,
}


def _record_metric(name: str, delta: int = 1) -> None:
    with _METRICS_LOCK:
        _METRICS[name] += delta


def get_metrics() -> dict:
    """Return bounded WebSocket transport counters for admin observability."""
    with _METRICS_LOCK:
        return deepcopy(_METRICS)


def _snapshot(cluster_id: str | None = None, is_default_cluster: bool = True) -> tuple[object, ...]:
    """A cheap fingerprint of incident state for the selected cluster.

    Watcher heartbeat is deliberately excluded: it changes on every poll
    and used to trigger a full browser reload every few seconds even when
    the cluster state had not changed.

    Snapshot changes are included so a Watcher publish can notify the browser
    across process/container boundaries while this polling fallback remains
    the transport of record. They are detected with a `stat`-based
    fingerprint, not by reading the snapshots: this runs every
    POLL_INTERVAL_SECONDS for EVERY open tab, and reading six full payloads
    to compare six integers meant deserializing and deep-copying tens of KB
    of PG/pool/CRUSH data per tab per poll, all inside one global cache lock.
    """
    with db.SessionLocal() as session:
        default_cluster = ensure_default_cluster(session)
        effective_id = cluster_id or default_cluster.id
        cluster_filter = (
            or_(Incident.cluster_id == effective_id, Incident.cluster_id.is_(None))
            if is_default_cluster
            else Incident.cluster_id == effective_id
        )
        count = session.query(func.count(Incident.id)).filter(cluster_filter).scalar()
        latest_updated = session.query(func.max(Incident.updated_at)).filter(cluster_filter).scalar()
    section_versions = [("health", snapshot_fingerprint(effective_id))]
    for section in ("status", "pools", "pgs", "crush", "nodes"):
        section_versions.append((section, section_snapshot_fingerprint(effective_id, section)))
    return count, latest_updated, tuple(section_versions)


async def _reject_socket(websocket: WebSocket, *, accept: bool, record: bool) -> None:
    if record:
        _record_metric("cluster_state_policy_rejections_total")
    if accept:
        await websocket.accept()
    await websocket.close(code=WS_POLICY_VIOLATION)


async def _authorize_cluster_socket(
    websocket: WebSocket, *, accept_invalid_session: bool, count_every_rejection: bool,
) -> tuple[str, bool] | None:
    """Resolve the session's cluster and enforce read capability server-side.

    Returns ``(cluster_id, is_default)`` or closes the socket with a policy
    violation and returns ``None``. A ``?cluster=`` that differs from the
    session's selection is always rejected and counted: a socket must never
    stream another cluster's events.
    """
    if not _session_is_valid(websocket.session) or websocket.session.get("product") == "vitastor":
        await _reject_socket(websocket, accept=accept_invalid_session, record=count_every_rejection)
        return None
    with db.SessionLocal() as session:
        default_cluster = ensure_default_cluster(session)
        active = {cluster.id: cluster for cluster in list_active_clusters(session)}
        selected = active.get(websocket.session.get("selected_cluster_id"), default_cluster)
        selected_id = selected.id
        selected_is_default = selected.is_default
    if not has_cluster_capability(
        str(websocket.session.get("user") or ""), selected_id, CAPABILITY_READ
    ):
        await _reject_socket(websocket, accept=True, record=count_every_rejection)
        return None
    requested_id = websocket.query_params.get("cluster_id") or websocket.query_params.get("cluster")
    if requested_id and requested_id != selected_id:
        await _reject_socket(websocket, accept=True, record=True)
        return None
    return selected_id, selected_is_default


@router.websocket("/ws/incidents")
async def incidents_ws(websocket: WebSocket) -> None:
    # SessionMiddleware populates websocket.session from the same signed
    # cookie used by the HTTP routes (Starlette applies session middleware
    # to the "websocket" scope too) — same require_login check as / , just
    # not expressible as a FastAPI Depends on a websocket route.
    authorized = await _authorize_cluster_socket(
        websocket, accept_invalid_session=False, count_every_rejection=True,
    )
    if authorized is None:
        return
    selected_id, selected_is_default = authorized
    await websocket.accept()
    last_seen = _snapshot(selected_id, selected_is_default)
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                current = _snapshot(selected_id, selected_is_default)
            except Exception:
                logger.exception("incidents_ws: failed to poll DB, closing connection")
                await websocket.close(code=1011)  # internal error
                return
            if current[:2] != last_seen[:2]:
                await websocket.send_json({"event": "incidents_changed"})
            if len(current) > 2 and len(last_seen) > 2 and current[2] != last_seen[2]:
                changed_sections = [
                    section for (section, generation), (_old_section, old_generation)
                    in zip(current[2], last_seen[2])
                    if generation != old_generation
                ]
                await websocket.send_json({
                    "event": "snapshot_changed",
                    "cluster_id": selected_id,
                    "sections": changed_sections,
                })
            if current != last_seen:
                last_seen = current
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("incidents_ws: unexpected error, closing connection")


_OPTIONAL_EVENT_FIELDS = ("action_status", "action_state", "action_id", "request_id")


def _cluster_state_message(current: dict, cluster_id: str) -> dict:
    message = {
        "event": current.get("event"),
        "cluster_id": cluster_id,
        "sections": current.get("sections", []),
        "generation": current.get("generation"),
        "collected_at": current.get("collected_at"),
    }
    message.update({key: current[key] for key in _OPTIONAL_EVENT_FIELDS if current.get(key)})
    return message


def _open_cluster_state_connection(websocket: WebSocket):
    """Count the connection and bind a client-supplied request id if it is safe."""
    _record_metric("cluster_state_connections_total")
    _record_metric("cluster_state_connections_open")
    if websocket.query_params.get("reconnect") == "1":
        _record_metric("cluster_state_reconnect_total")
    requested_request_id = websocket.query_params.get("request_id", "").strip()
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", requested_request_id):
        return set_request_id(requested_request_id)
    return None


def _record_event_metrics(event: object) -> None:
    _record_metric("cluster_state_messages_total")
    if event == "action_state_changed":
        _record_metric("cluster_state_events_action_total")
    elif event == "snapshot_changed":
        _record_metric("cluster_state_events_snapshot_total")


@router.websocket("/ws/cluster-state")
async def cluster_state_ws(websocket: WebSocket) -> None:
    """Send cluster-scoped invalidation metadata over the shared event store.

    The event is only a hint. Clients must fetch the normal authenticated HTTP
    API to obtain the snapshot, which keeps the transport small and makes a
    missed event safe. The legacy incidents socket remains available for older
    dashboard clients.
    """
    if not settings.dashboard_cluster_events_enabled:
        await websocket.accept()
        await websocket.close(code=1013)
        return
    authorized = await _authorize_cluster_socket(
        websocket, accept_invalid_session=True, count_every_rejection=False,
    )
    if authorized is None:
        return
    selected_id = authorized[0]

    await websocket.accept()
    request_token = _open_cluster_state_connection(websocket)
    connection_open = True
    last_event_id = None
    initial = read_latest_event(selected_id)
    if initial:
        last_event_id = initial.get("generation")
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            started = monotonic()
            current = read_latest_event(selected_id)
            record_query(
                selected_id,
                "ws_event_read",
                (monotonic() - started) * 1000,
                status="success" if current is not None else "empty",
            )
            if not current or current.get("generation") == last_event_id:
                continue
            last_event_id = current.get("generation")
            await websocket.send_json(_cluster_state_message(current, selected_id))
            _record_event_metrics(current.get("event"))
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("cluster_state_ws: unexpected error, closing connection")
    finally:
        if connection_open:
            _record_metric("cluster_state_disconnects_total")
            _record_metric("cluster_state_connections_open", -1)
        if request_token is not None:
            reset_request_id(request_token)
