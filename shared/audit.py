import uuid

from sqlalchemy.orm import Session

from shared import incident_events
from shared.models import AuditEntry

ACTOR_SYSTEM = "system"

EVENT_SAFE_ACTION_EXECUTED = "safe_action_executed"
EVENT_SAFE_ACTION_FAILED = "safe_action_failed"

# Story 4.2/4.3: RISKY-action lifecycle (FR8/FR9).
EVENT_RISKY_ACTION_PENDING_APPROVAL = "risky_action_pending_approval"
EVENT_RISKY_ACTION_APPROVED = "risky_action_approved"
EVENT_RISKY_ACTION_REJECTED = "risky_action_rejected"
EVENT_RISKY_ACTION_EXECUTED = "risky_action_executed"
EVENT_RISKY_ACTION_FAILED = "risky_action_failed"
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
EVENT_POOL_CREATE_REQUESTED = "pool_create_requested"

# dashboard/routes/upgrade.py: an in-flight upgrade's pause/resume are
# operator-triggered commands run directly (not through the Action/approval
# pipeline — see watcher/ceph_client.py's module note on why cephadm's own
# their own audit events distinct from the EVENT_RISKY_ACTION_* family that
# covers the Action row's own PENDING_APPROVAL -> APPROVED -> EXECUTED
# lifecycle (i.e. issuing the initial `ceph orch upgrade start`).
EVENT_CLUSTER_UPGRADE_PAUSED = "cluster_upgrade_paused"
EVENT_CLUSTER_UPGRADE_RESUMED = "cluster_upgrade_resumed"
EVENT_CLUSTER_UPGRADE_OSD_FLAGS_UNSET = "cluster_upgrade_osd_flags_unset"

# Epic 9, Story 9.1: worker/backup/engine.py's retention sweep — fired once
# per object actually deleted, attached to the same incident_id/action_id
# as the backup (or manual retention_sweep_delete) Action that triggered
# the sweep, not a None/synthetic pair (AuditEntry.incident_id is a
# mandatory FK — AD-7 requires every write to be a real, attributable row).
EVENT_BACKUP_RETENTION_DELETE = "backup_retention_delete"
# Dashboard backup page: records who explicitly requested an immediate RBD
# or metadata backup, separately from the Worker's eventual success/failure
# event for the SAFE action.
EVENT_BACKUP_MANUAL_REQUESTED = "backup_manual_requested"

# AI roadmap Pha 0.3 (worker/preflight.py): fired instead of a normal
# Action row whenever run_preflight() returns allowed=False AND
# settings.ai_preflight_enforcement_enabled is True -- action_id is None
# (same "no command was ever actually proposed" shape as
# EVENT_RISKY_ACTION_AUTO_REJECTED_CLUSTER_OPERATION_IN_PROGRESS above,
# not a real Action row to attach to).
EVENT_PROPOSAL_BLOCKED_BY_PREFLIGHT = "proposal_blocked_by_preflight"
EVENT_AUTOPILOT_KILL_SWITCH_BLOCKED = "autopilot_kill_switch_blocked"
EVENT_AUTOPILOT_OPERATIONAL_GATE_BLOCKED = "autopilot_operational_gate_blocked"
EVENT_AUTOPILOT_RUNTIME_GUARD_BLOCKED = "autopilot_runtime_guard_blocked"
EVENT_AUTOPILOT_PLAYBOOK_CONTRACT_BLOCKED = "autopilot_playbook_contract_blocked"
EVENT_AUTOPILOT_EXECUTION_INCONCLUSIVE = "autopilot_execution_inconclusive"
EVENT_REMEDIATION_CASE_VERDICT_UPDATED = "remediation_case_verdict_updated"

# AI roadmap Pha 0.4 (dashboard/routes/actions.py::approve_action_core):
# fired when an operator/Telegram button tries to approve an Action past
# its Action.expires_at (stale-evidence check, section 3.3) -- the
# approval itself is refused (Action.status stays PENDING_APPROVAL,
# unlike a real rejection), so this is a DISTINCT event from
# EVENT_RISKY_ACTION_REJECTED even though both end with "operator did not
# get their approval to go through".
EVENT_RISKY_ACTION_APPROVAL_EXPIRED = "risky_action_approval_expired"


def record(
    session: Session, *, incident_id: str, action_id: str | None, event_type: str, actor: str
) -> None:
    """AD-7: the ONLY place that ever inserts an AuditEntry row. Does NOT
    commit — the caller controls the transaction boundary, so this write is
    always atomic with the Action/Incident status change it describes
    in the same transaction as the state change."""
    entry_id = str(uuid.uuid4())
    session.add(
        AuditEntry(
            id=entry_id,
            incident_id=incident_id,
            action_id=action_id,
            event_type=event_type,
            actor=actor,
        )
    )
    incident_events.record(
        session, incident_id=incident_id, action_id=action_id,
        event_type=event_type, actor=actor, source_type="audit", source_id=entry_id,
    )

# 2026-08-20: fired when a monitor auto-resolves an Incident (the underlying
# problem went away on its own) and, in the SAME transaction, closes out
# every Action still sitting in PENDING_APPROVAL underneath it —
# see shared/incident_actions.py::cancel_pending_actions for why leaving
# them open was never harmless. Distinct from EVENT_RISKY_ACTION_REJECTED
# (a human said no) and from
# EVENT_RISKY_ACTION_AUTO_REJECTED_CLUSTER_OPERATION_IN_PROGRESS (proposal
# suppressed during an upgrade window): here nobody rejected anything and
# nothing was suppressed — the request simply stopped being about a problem
# that still exists.
EVENT_RISKY_ACTION_AUTO_CANCELLED_INCIDENT_RESOLVED = (
    "risky_action_auto_cancelled_incident_resolved"
)


# 2026-08-20 (watcher/verify.py — xác minh sau khắc phục). Ba kết cục của
# một vòng kiểm chứng, tách riêng khỏi EVENT_RISKY_ACTION_EXECUTED: sự kiện
# kia chỉ nói "lệnh chạy xong exit 0", ba sự kiện này mới nói về việc VẤN ĐỀ
# còn hay hết — đúng thứ mà trước đây không ai ghi lại, và cũng là thứ
# operator thật sự muốn biết khi lật lại lịch sử một Incident.
EVENT_INCIDENT_FIX_VERIFIED = "incident_fix_verified"
EVENT_INCIDENT_FIX_NOT_EFFECTIVE = "incident_fix_not_effective"
EVENT_INCIDENT_FIX_GAVE_UP = "incident_fix_gave_up"
