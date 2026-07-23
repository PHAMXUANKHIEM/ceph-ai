from sqlalchemy.orm import Session

from shared.models import SystemFlag

# Scoped to exactly one action_id (restart_osd_daemon) — the only RISKY
# action with a real, working remediation command right now (pg_repair_force
# has none, see worker/executor/commands.py). A single flag per action_id
# rather than a blanket "auto-approve everything RISKY" switch, so turning
# this on can't silently start auto-executing some future RISKY action_id
# nobody's reviewed for this yet.
AUTO_APPROVE_RESTART_OSD_KEY = "auto_approve_restart_osd_daemon"


def is_auto_approve_restart_osd_enabled(session: Session) -> bool:
    """Fails CLOSED (False / still requires approval) when the flag row is
    missing — same conservative-by-default posture as AD-5's classification
    (gate.py) and AD-4's kill-switch (shared/kill_switch.py): "can't tell"
    must mean "keep the human in the loop", not "skip them"."""
    flag = session.get(SystemFlag, AUTO_APPROVE_RESTART_OSD_KEY)
    if flag is None:
        return False
    return bool(flag.value)


def set_auto_approve_restart_osd(session: Session, enabled: bool) -> None:
    """The Dashboard's only write path for this flag. Does NOT commit — same
    pattern as shared/kill_switch.py::set_kill_switch, caller controls the
    transaction boundary."""
    flag = session.get(SystemFlag, AUTO_APPROVE_RESTART_OSD_KEY)
    if flag is None:
        session.add(SystemFlag(key=AUTO_APPROVE_RESTART_OSD_KEY, value=enabled))
    else:
        flag.value = enabled
