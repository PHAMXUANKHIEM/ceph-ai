"""Durable audit helpers for the unrestricted Single Full workflow."""

from __future__ import annotations

import hashlib
from datetime import datetime

from shared import db
from shared.models import SingleFullAudit
from shared.time import utc_now


def start_run(*, run_id: str, actor: str, chat_id: str, cluster_id: str,
              cluster_ref: str, prompt: str) -> None:
    with db.SessionLocal() as session:
        session.add(SingleFullAudit(
            run_id=run_id, actor=actor, telegram_chat_id=chat_id,
            cluster_id=cluster_id, cluster_ref=cluster_ref,
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            operator_acknowledged=True, status="RUNNING",
        ))
        session.commit()


def finish_run(run_id: str, *, status: str, result_code: str | None = None,
               error_type: str | None = None, finished_at: datetime | None = None) -> None:
    with db.SessionLocal() as session:
        row = session.query(SingleFullAudit).filter_by(run_id=run_id).one_or_none()
        if row is None:
            return
        row.status = status
        row.result_code = result_code
        row.error_type = error_type
        row.finished_at = finished_at or utc_now()
        session.commit()
