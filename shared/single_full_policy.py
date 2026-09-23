"""Server-side gate for isolated code-repair automation.

Single Full is an explicitly authenticated operator mode and intentionally
keeps its unrestricted provider permissions. This policy only protects the
separate automated code-repair pipeline from running its bypass mode in
production.
"""

from __future__ import annotations

from config.settings import settings


class CodeRepairFullAccessDisabledError(RuntimeError):
    """Raised when automated code-repair bypass mode is not allowed."""


def _environment() -> str:
    return str(getattr(settings, "ceph_ai_environment", "development") or "").strip().lower()


def ensure_code_repair_full_access_allowed() -> None:
    """Allow isolated code-repair bypass mode only outside production.

    This is separate from Telegram Single Full: the nightly repair pipeline
    operates on an isolated candidate worktree and is controlled by its
    existing automation flag.  It is still denied in production.
    """
    if _environment() == "production":
        raise CodeRepairFullAccessDisabledError(
            "Code-repair full-access bị vô hiệu hóa trong production"
        )
    if not bool(getattr(settings, "code_repair_auto_enabled", False)):
        raise CodeRepairFullAccessDisabledError(
            "Code-repair full-access yêu cầu CODE_REPAIR_AUTO_ENABLED=true"
        )
