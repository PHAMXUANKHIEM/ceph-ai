"""Live provider-toggle reload for containerized ceph-ai services.

The Settings page persists provider activation in a shared env file. Changing
that file does not update an existing container's environment, so Worker,
Watcher and Telegram previously kept a stale provider until recreation.
"""

from __future__ import annotations

import os
from pathlib import Path

from config.settings import settings

_NAMES = {
    "CODEX_CHAT_ENABLED": "codex_chat_enabled",
    "CLAUDE_CHAT_ENABLED": "claude_chat_enabled",
}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def refresh_chat_provider_flags() -> None:
    """Apply only persisted non-secret provider booleans in containers."""
    if os.environ.get("CEPH_AI_CONTAINERIZED", "").lower() != "true":
        return
    path = Path(os.environ.get("CEPH_AI_ENV_FILE", "/var/lib/ceph-ai/config/.env"))
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        name, raw_value = text.split("=", 1)
        field = _NAMES.get(name.strip())
        if field is None:
            continue
        value = raw_value.strip().strip("\"'").lower()
        if value in _TRUE:
            setattr(settings, field, True)
        elif value in _FALSE:
            setattr(settings, field, False)
