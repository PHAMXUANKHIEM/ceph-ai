from sqlalchemy.orm import Session

from shared.models import SystemFlag

KILL_SWITCH_KEY = "kill_switch_enabled"


def is_kill_switch_enabled(session: Session) -> bool:
    """AD-4: callers must call this with a fresh session right before EVERY
    remediation command — never cache the result, and call it again for
    each command in a multi-command sequence. Takes a session (rather than
    opening its own) so the caller controls exactly when "right now" is.

    Fails CLOSED (treated as enabled/blocking) if the flag row is
    unexpectedly missing — the migration always seeds it, so this should
    never happen in practice, but "can't determine the kill-switch state"
    must default to "don't execute", matching this project's conservative
    default elsewhere (e.g. AD-5's default-to-RISKY classification).
    """
    flag = session.get(SystemFlag, KILL_SWITCH_KEY)
    if flag is None:
        return True
    return bool(flag.value)


def set_kill_switch(session: Session, enabled: bool) -> None:
    """Story 4.1: the Dashboard's only write path for the kill-switch. Does
    NOT commit — same pattern as `shared/audit.py::record()`, so the caller
    controls the transaction boundary.

    The migration that creates `system_flags` always seeds this row, so the
    update branch is the expected path in production — the create branch
    only exists so this function is correct even against a bare
    `Base.metadata.create_all()` test DB, which has the table but not the
    seeded row.
    """
    flag = session.get(SystemFlag, KILL_SWITCH_KEY)
    if flag is None:
        session.add(SystemFlag(key=KILL_SWITCH_KEY, value=enabled))
    else:
        flag.value = enabled
