"""Authentication material for the isolated Telegram Single Full executor."""

from __future__ import annotations

import os
from pathlib import Path


def executor_token() -> str:
    """Read the per-container bearer token without putting it in app config.

    Container deployments mount this file only into ``telegram-ai`` and
    ``full-executor``.  The environment fallback preserves the legacy
    systemd/development path, where no container secret exists.
    """
    token_file = os.environ.get("SINGLE_FULL_EXECUTOR_TOKEN_FILE", "").strip()
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get("SINGLE_FULL_EXECUTOR_TOKEN", "").strip()
