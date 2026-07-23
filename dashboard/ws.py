import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import func

from shared import db, heartbeat
from shared.models import Incident

router = APIRouter()
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2
WS_POLICY_VIOLATION = 1008


def _snapshot() -> tuple[int, object, object]:
    """A cheap fingerprint of table state: Incident row count + latest
    update time, plus WatcherHeartbeat's polled_at (Story 5.2) — Watcher
    writes a heartbeat on EVERY poll (not just Incident-worthy transitions),
    so including it here makes the connection-status section refresh
    in near-real-time too, reusing this exact same polling mechanism with
    no new event type or app.js change needed.

    Polling the DB is a simple stand-in — there is no event bus wired from
    Watcher/Worker directly to the Dashboard.
    """
    with db.SessionLocal() as session:
        count = session.query(func.count(Incident.id)).scalar()
        latest_updated = session.query(func.max(Incident.updated_at)).scalar()
        latest_heartbeat = heartbeat.get_latest(session)
        polled_at = latest_heartbeat.polled_at if latest_heartbeat is not None else None
    return count, latest_updated, polled_at


@router.websocket("/ws/incidents")
async def incidents_ws(websocket: WebSocket) -> None:
    # SessionMiddleware populates websocket.session from the same signed
    # cookie used by the HTTP routes (Starlette applies session middleware
    # to the "websocket" scope too) — same require_login check as / , just
    # not expressible as a FastAPI Depends on a websocket route.
    if not websocket.session.get("user"):
        await websocket.close(code=WS_POLICY_VIOLATION)
        return

    await websocket.accept()
    last_seen = _snapshot()
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                current = _snapshot()
            except Exception:
                logger.exception("incidents_ws: failed to poll DB, closing connection")
                await websocket.close(code=1011)  # internal error
                return
            if current != last_seen:
                last_seen = current
                await websocket.send_json({"event": "incidents_changed"})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("incidents_ws: unexpected error, closing connection")
