from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models import WatcherHeartbeat


def record(
    session: Session,
    *,
    cluster_id: str | None,
    success: bool,
    mon_node: str | None,
    error_message: str | None,
    polled_at: datetime,
) -> None:
    """The ONLY place that ever writes WatcherHeartbeat — upserts the one
    row for `cluster_id` in place rather than appending, since this table
    only ever answers "what happened on the LAST poll for this cluster".

    2026-08-07 (multi-cluster observability Phase 1): the upsert key used
    to be a fixed `id=1` (true singleton, single-cluster only); it's now
    `cluster_id`, so each cluster gets its own row. `cluster_id=None` is a
    valid, distinct key too (matches nothing written by this function since
    every real caller now passes a real id — see WatcherHeartbeat's own
    docstring for why the pre-migration id=1 row is simply abandoned rather
    than migrated onto this new key). Does NOT commit — the caller controls
    the transaction boundary (same pattern as shared/audit.py::record())."""
    row = session.scalar(select(WatcherHeartbeat).where(WatcherHeartbeat.cluster_id == cluster_id))
    if row is None:
        row = WatcherHeartbeat(cluster_id=cluster_id)
        session.add(row)
    row.success = success
    row.mon_node = mon_node
    row.error_message = error_message
    row.polled_at = polled_at


def get_latest(session: Session, cluster_id: str | None) -> WatcherHeartbeat | None:
    """Returns None if Watcher has never completed a poll cycle for this
    cluster yet — callers must treat that as "no connectivity data
    available", not silently assume a healthy state."""
    return session.scalar(select(WatcherHeartbeat).where(WatcherHeartbeat.cluster_id == cluster_id))
