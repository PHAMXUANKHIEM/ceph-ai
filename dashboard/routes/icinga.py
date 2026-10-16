"""POST /api/icinga/events: Icinga's ceph-ai-webhook (plan icinga-alerting-handoff, I1; 16/10/2026).

No Dashboard login: Icinga authenticates with a bearer token kept in settings.icinga_webhook_token_file
(0600, never shown). The path is exempt from the browser CSRF checks in dashboard/app.py for that
reason; the token is checked here, in constant time, before anything else. The event is only
recorded and linked to open incidents (shared/icinga_events.py): it never opens an incident or
runs an action.
"""

from __future__ import annotations

import hmac
import logging
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException
from pydantic import ValidationError

from config.settings import settings
from shared import icinga_events

router = APIRouter()
log = logging.getLogger(__name__)
WEBHOOK_PATH = "/api/icinga/events"


def _expected_token() -> str | None:
    try:
        value = Path(settings.icinga_webhook_token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if len(value) >= 32 else None


@router.post(WEBHOOK_PATH)
def icinga_event(body: dict, authorization: str = Header(default="")) -> dict:  # sync: runs in the threadpool (DB + file I/O)
    expected = _expected_token()
    if expected is None:
        raise HTTPException(status_code=503, detail="Icinga webhook chưa được cấu hình (thiếu token).")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip().encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Token không hợp lệ.")
    try:
        event = icinga_events.IcingaEvent.model_validate(body).checked()
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Sự kiện không hợp lệ: {exc}") from exc
    result = icinga_events.record(event)
    log.info("icinga event %s %s/%s %s linked=%s", event.notification_type, event.host, event.service,
             event.state, result.get("linked_incidents"))
    return result
