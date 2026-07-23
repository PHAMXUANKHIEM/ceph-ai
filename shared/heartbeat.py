from datetime import datetime

from sqlalchemy.orm import Session

from shared.models import WatcherHeartbeat

HEARTBEAT_ID = 1


def record(
    session: Session,
    *,
    success: bool,
    mon_node: str | None,
    error_message: str | None,
    polled_at: datetime,
) -> None:
    """The ONLY place that ever writes WatcherHeartbeat — upserts the single
    row (id=HEARTBEAT_ID) in place rather than appending, since this table
    only ever answers "what happened on the LAST poll". Does NOT commit —
    the caller controls the transaction boundary (same pattern as
    shared/audit.py::record())."""
    row = session.get(WatcherHeartbeat, HEARTBEAT_ID)
    if row is None:
        row = WatcherHeartbeat(id=HEARTBEAT_ID)
        session.add(row)
    row.success = success
    row.mon_node = mon_node
    row.error_message = error_message
    row.polled_at = polled_at


def get_latest(session: Session) -> WatcherHeartbeat | None:
    """Returns None if Watcher has never completed a poll cycle yet —
    callers must treat that as "no connectivity data available", not
    silently assume a healthy state."""
    return session.get(WatcherHeartbeat, HEARTBEAT_ID)
