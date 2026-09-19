"""Append-only audit trail for Dashboard HTTP mutations."""

from __future__ import annotations

import logging

from sqlalchemy.exc import SQLAlchemyError

from shared import db
from shared.models import SecurityAuditEvent

logger = logging.getLogger(__name__)


def record_mutation(request, response) -> None:
    """Persist mutation metadata without reading or storing request bodies."""
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    try:
        with db.SessionLocal() as session:
            session.add(
                SecurityAuditEvent(
                    actor=str(request.session.get("user") or "anonymous")[:64],
                    method=request.method.upper(),
                    path=request.url.path[:255],
                    status_code=int(response.status_code),
                    request_id=str(getattr(request.state, "request_id", ""))[:128],
                )
            )
            session.commit()
    except SQLAlchemyError:
        # Audit failure must never turn a completed operator mutation into a
        # second error response, but it must remain visible to operations.
        logger.exception("could not persist Dashboard mutation audit event")
