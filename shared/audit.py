from sqlalchemy.orm import Session

from shared.models import AuditEntry

ACTOR_SYSTEM = "system"

EVENT_SAFE_ACTION_EXECUTED = "safe_action_executed"
EVENT_SAFE_ACTION_FAILED = "safe_action_failed"
EVENT_SAFE_ACTION_BLOCKED_BY_KILL_SWITCH = "safe_action_blocked_by_kill_switch"

# Story 4.2/4.3: RISKY-action lifecycle (FR8/FR9, AD-4).
EVENT_RISKY_ACTION_PENDING_APPROVAL = "risky_action_pending_approval"
EVENT_RISKY_ACTION_APPROVED = "risky_action_approved"
EVENT_RISKY_ACTION_REJECTED = "risky_action_rejected"
EVENT_RISKY_ACTION_EXECUTED = "risky_action_executed"
EVENT_RISKY_ACTION_FAILED = "risky_action_failed"
EVENT_RISKY_ACTION_BLOCKED_BY_KILL_SWITCH = "risky_action_blocked_by_kill_switch"
# 2026-07-23: fired by dashboard/routes/actions.py::approve_action instead
# of EVENT_RISKY_ACTION_APPROVED when the action_id has no automated
# command at all (investigate_manually, pg_repair_force — see
# worker/executor/commands.py::has_command) — "Duyệt" here means "operator
# acknowledges, will handle manually", never routed to Worker execution, so
# it must not read as a normal approved-and-executed action in the trail.
EVENT_RISKY_ACTION_ACKNOWLEDGED_NO_COMMAND = "risky_action_acknowledged_no_command"
# 2026-07-24: a cluster upgrade/patch install restarts every Ceph daemon on
# every node one at a time — each restart routinely trips a transient
# OSD_DOWN/MGR_DOWN incident that would otherwise spam the operator with a
# new RISKY proposal to reject, every few seconds, for the whole duration of
# the upgrade (verified live: 4 such proposals in under 90 seconds during
# one upgrade run). worker/llm/router_client.py::diagnose_incident auto-
# rejects RISKY proposals instead of surfacing them while
# CLUSTER_UPGRADE_ACTION_IDS/PATCH_INSTALL_ACTION_ID has anything
# PENDING_APPROVAL/APPROVED — this fires instead of
# EVENT_RISKY_ACTION_PENDING_APPROVAL for that window; normal proposal
# resumes automatically once the in-flight upgrade/patch action leaves that
# window (EXECUTED/FAILED), no separate "re-enable" step needed.
EVENT_RISKY_ACTION_AUTO_REJECTED_CLUSTER_OPERATION_IN_PROGRESS = (
    "risky_action_auto_rejected_cluster_operation_in_progress"
)

# dashboard/routes/chat.py: fired once, when an operator confirms a chat
# proposal and the Incident/Action rows get created — separate from
# EVENT_RISKY_ACTION_PENDING_APPROVAL / the SAFE execution events (those
# still fire too, from the normal pipeline that owns the row after this) so
# the audit trail can tell "this Action originated from a chat request" from
# "this Action originated from a real detected Incident".
EVENT_CHAT_ACTION_REQUESTED = "chat_action_requested"

# dashboard/routes/upgrade.py: an in-flight upgrade's pause/resume are
# operator-triggered commands run directly (not through the Action/approval
# pipeline — see watcher/ceph_client.py's module note on why cephadm's own
# upgrade loop isn't gated by the kill-switch once started), so they need
# their own audit events distinct from the EVENT_RISKY_ACTION_* family that
# covers the Action row's own PENDING_APPROVAL -> APPROVED -> EXECUTED
# lifecycle (i.e. issuing the initial `ceph orch upgrade start`).
EVENT_CLUSTER_UPGRADE_PAUSED = "cluster_upgrade_paused"
EVENT_CLUSTER_UPGRADE_RESUMED = "cluster_upgrade_resumed"

# Epic 9, Story 9.1: worker/backup/engine.py's retention sweep — fired once
# per object actually deleted, attached to the same incident_id/action_id
# as the backup (or manual retention_sweep_delete) Action that triggered
# the sweep, not a None/synthetic pair (AuditEntry.incident_id is a
# mandatory FK — AD-7 requires every write to be a real, attributable row).
EVENT_BACKUP_RETENTION_DELETE = "backup_retention_delete"


def record(
    session: Session, *, incident_id: str, action_id: str | None, event_type: str, actor: str
) -> None:
    """AD-7: the ONLY place that ever inserts an AuditEntry row. Does NOT
    commit — the caller controls the transaction boundary, so this write is
    always atomic with the Action/Incident status change it describes
    (same pattern as shared/kill_switch.py::is_kill_switch_enabled)."""
    session.add(
        AuditEntry(
            incident_id=incident_id,
            action_id=action_id,
            event_type=event_type,
            actor=actor,
        )
    )
