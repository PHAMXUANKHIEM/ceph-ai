"""Shared PostgreSQL request throttling for Dashboard API endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from shared.time import utc_now

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from shared import db
from shared.models import ApiRateLimit

logger = logging.getLogger(__name__)


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
    now: datetime | None = None,
) -> bool:
    """Atomically consume one API request slot for ``key``.

    A row lock makes the counter shared and safe across Dashboard processes.
    Store failures raise instead of allowing a request through, so a single
    replica cannot silently bypass the production limit.
    """
    now = now or utc_now()
    try:
        with db.SessionLocal() as session:
            _insert_row(session, key, now)
            row = session.execute(
                select(ApiRateLimit)
                .where(ApiRateLimit.client_key == key)
                .with_for_update()
            ).scalar_one()
            if now - row.window_started_at >= timedelta(seconds=window_seconds):
                row.request_count = 1
                row.window_started_at = now
                row.updated_at = now
                session.commit()
                return True
            if row.request_count >= limit:
                return False
            row.request_count += 1
            row.updated_at = now
            session.commit()
            return True
    except SQLAlchemyError as exc:
        logger.exception("API rate-limit store is unavailable for %s", key)
        raise RateLimitStoreUnavailable from exc
