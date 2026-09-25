"""Shared PostgreSQL request throttling for Dashboard API endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from threading import Lock
from shared.time import utc_now

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from shared import db
from shared.models import ApiRateLimit

logger = logging.getLogger(__name__)

# Reserve a small block of slots in PostgreSQL, then consume that block in
# process. The database counter is incremented when the block is reserved, so
# this can reject early after a restart but can never let a replica exceed the
# shared limit. It removes one row lock/commit from every request in a browser
# burst while retaining a fail-closed, cross-replica upper bound.
_reservation_guard = Lock()
_reservation_locks: dict[tuple[int, str, int, int], Lock] = {}
_reservations: dict[tuple[int, str, int, int], tuple[int, datetime]] = {}


class RateLimitStoreUnavailable(RuntimeError):
    """Raised when the shared rate-limit store cannot be consulted."""


def _insert_row(session, key: str, now: datetime) -> None:
    values = {
        "client_key": key,
        "request_count": 0,
        "window_started_at": now,
        "updated_at": now,
    }
    dialect = session.bind.dialect.name
    if dialect == "postgresql":
        session.execute(
            postgres_insert(ApiRateLimit)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["client_key"])
        )
    elif dialect == "sqlite":
        session.execute(
            sqlite_insert(ApiRateLimit)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["client_key"])
        )
    else:
        session.execute(insert(ApiRateLimit).values(**values))


def allow_api_request(
    key: str,
    *,
    limit: int = 120,
    window_seconds: int = 60,
    reservation_size: int = 1,
    now: datetime | None = None,
) -> bool:
    """Atomically consume one API request slot for ``key``.

    A row lock makes the counter shared and safe across Dashboard processes.
    Store failures raise instead of allowing a request through, so a single
    replica cannot silently bypass the production limit.
    """
    now = now or utc_now()
    reservation_size = max(1, min(int(reservation_size), limit))
    cache_key = (id(db.SessionLocal), key, limit, window_seconds)
    with _reservation_guard:
        key_lock = _reservation_locks.setdefault(cache_key, Lock())

    with key_lock:
        cached = _reservations.get(cache_key)
        if cached is not None:
            remaining, expires_at = cached
            if now < expires_at and remaining > 0:
                if remaining == 1:
                    _reservations.pop(cache_key, None)
                else:
                    _reservations[cache_key] = (remaining - 1, expires_at)
                return True
            _reservations.pop(cache_key, None)

        try:
            with db.SessionLocal() as session:
                _insert_row(session, key, now)
                row = session.execute(
                    select(ApiRateLimit)
                    .where(ApiRateLimit.client_key == key)
                    .with_for_update()
                ).scalar_one()
                if now - row.window_started_at >= timedelta(seconds=window_seconds):
                    row.request_count = 0
                    row.window_started_at = now
                capacity = limit - row.request_count
                if capacity <= 0:
                    return False
                reserved = min(reservation_size, capacity)
                row.request_count += reserved
                row.updated_at = now
                expires_at = row.window_started_at + timedelta(seconds=window_seconds)
                session.commit()
                if reserved > 1:
                    _reservations[cache_key] = (reserved - 1, expires_at)
                return True
        except SQLAlchemyError as exc:
            logger.exception("API rate-limit store is unavailable for %s", key)
            raise RateLimitStoreUnavailable from exc


def clear_api_rate_limit_reservations() -> None:
    """Drop process-local reservations, primarily for deterministic tests."""
    with _reservation_guard:
        _reservations.clear()
        _reservation_locks.clear()
