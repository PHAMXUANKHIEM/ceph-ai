"""Fail-closed state machine contract for AI remediation.

The database keeps its historical Action/Incident values for compatibility.
This module provides one canonical lifecycle for new orchestration code without
adding enum values or requiring a migration in the first rollout.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class RemediationState(str, Enum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    INCONCLUSIVE = "INCONCLUSIVE"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


_TERMINAL = frozenset({
    RemediationState.SUCCEEDED, RemediationState.ROLLED_BACK,
    RemediationState.INCONCLUSIVE, RemediationState.EXPIRED,
    RemediationState.CANCELLED,
})
_LOCKED = frozenset({RemediationState.EXECUTING, RemediationState.VERIFYING})
_ALLOWED = {
    RemediationState.PROPOSED: frozenset({
        RemediationState.APPROVED, RemediationState.EXPIRED, RemediationState.CANCELLED,
    }),
    RemediationState.APPROVED: frozenset({
        RemediationState.EXECUTING, RemediationState.EXPIRED, RemediationState.CANCELLED,
    }),
    RemediationState.EXECUTING: frozenset({
        RemediationState.VERIFYING, RemediationState.FAILED, RemediationState.INCONCLUSIVE,
    }),
    RemediationState.VERIFYING: frozenset({
        RemediationState.SUCCEEDED, RemediationState.FAILED,
        RemediationState.ROLLED_BACK, RemediationState.INCONCLUSIVE,
    }),
    RemediationState.FAILED: frozenset({RemediationState.ROLLED_BACK}),
    RemediationState.INCONCLUSIVE: frozenset({RemediationState.ROLLED_BACK}),
}

_LEGACY_ACTION_STATUS = {
    RemediationState.PROPOSED: "PENDING",
    RemediationState.APPROVED: "APPROVED",
    RemediationState.EXECUTING: "EXECUTING",
    # Existing ActionStatus has no VERIFYING/SUCCEEDED values. EXECUTED is
    # retained while remediation_state carries the canonical lifecycle.
    RemediationState.VERIFYING: "EXECUTED",
    RemediationState.SUCCEEDED: "EXECUTED",
    RemediationState.FAILED: "FAILED",
    RemediationState.ROLLED_BACK: "FAILED",
    RemediationState.INCONCLUSIVE: "INCONCLUSIVE",
    RemediationState.EXPIRED: "REJECTED",
    RemediationState.CANCELLED: "REJECTED",
}


@dataclass(frozen=True)
class TransitionDecision:
    allowed: bool
    current: RemediationState
    requested: RemediationState
    reason: str
    requires_lock: bool = False
    terminal: bool = False


def _state(value: RemediationState | str) -> RemediationState:
    if isinstance(value, RemediationState):
        return value
    return RemediationState(str(value).strip().upper())


def transition(
    current: RemediationState | str,
    requested: RemediationState | str,
    *,
    now: datetime | None = None,
    expires_at: datetime | None = None,
    lock_owner: str | None = None,
    active_lock_owner: str | None = None,
) -> TransitionDecision:
    """Validate one lifecycle edge; never mutates DB or executes a command."""
    try:
        current_state = _state(current)
        requested_state = _state(requested)
    except ValueError as exc:
        fallback = RemediationState.INCONCLUSIVE
        return TransitionDecision(False, fallback, fallback, f"unknown remediation state: {exc}")

    if current_state in _TERMINAL:
        return TransitionDecision(
            False, current_state, requested_state,
            "terminal remediation state cannot transition", terminal=True,
        )

    effective_now = now or datetime.utcnow()
    if expires_at is not None and effective_now >= expires_at:
        if requested_state == RemediationState.EXPIRED:
            return TransitionDecision(
                True, current_state, requested_state,
                "approval/execution expiry accepted", terminal=True,
            )
        return TransitionDecision(
            False, current_state, requested_state,
            "remediation has expired; transition to EXPIRED first",
        )

    if requested_state not in _ALLOWED.get(current_state, frozenset()):
        return TransitionDecision(
            False, current_state, requested_state,
            f"transition {current_state.value} -> {requested_state.value} is not allowed",
        )

    # The distributed lease protects command dispatch and the in-flight write.
    # A worker may release it before a later read-only post-check records the
    # outcome; requiring the lease for VERIFYING -> terminal would strand
    # otherwise valid results after a normal executor hand-off.
    requires_lock = requested_state in _LOCKED
    if requires_lock:
        if not lock_owner or not active_lock_owner:
            return TransitionDecision(
                False, current_state, requested_state,
                "distributed execution lock is required", requires_lock=True,
            )
        if lock_owner != active_lock_owner:
            return TransitionDecision(
                False, current_state, requested_state,
                "distributed execution lock belongs to another worker",
                requires_lock=True,
            )

    return TransitionDecision(
        True, current_state, requested_state, "transition accepted",
        requires_lock=requires_lock, terminal=requested_state in _TERMINAL,
    )


def recover_after_worker_restart(
    current: RemediationState | str, *, lock_present: bool,
) -> TransitionDecision:
    """Recover an interrupted write without silently retrying it."""
    state = _state(current)
    if state not in _LOCKED:
        return TransitionDecision(
            False, state, state,
            "worker restart recovery is only needed for an in-flight state",
            terminal=state in _TERMINAL,
        )
    if lock_present:
        return TransitionDecision(
            False, state, state,
            "active lock remains; a single owner must finish or fail the action",
            requires_lock=True,
        )
    return TransitionDecision(
        True, state, RemediationState.INCONCLUSIVE,
        "worker lock disappeared; mark inconclusive and require operator review",
        requires_lock=True, terminal=True,
    )


def persist_transition(
    session, *, action, requested: RemediationState | str,
    incident=None, case=None, now: datetime | None = None,
    expires_at: datetime | None = None, lock_owner: str | None = None,
    active_lock_owner: str | None = None,
) -> TransitionDecision:
    """Validate and persist one canonical transition atomically.

    Legacy Action/Incident/RemediationCase fields are updated as a projection
    of the canonical Action.remediation_state. No command is executed here.
    """
    current = getattr(action, "remediation_state", None) or canonical_state(
        action_status=getattr(action, "status", None),
        case_outcome=getattr(case, "outcome", None),
        incident_status=getattr(incident, "status", None),
    ).value
    decision = transition(
        current, requested, now=now, expires_at=expires_at,
        lock_owner=lock_owner, active_lock_owner=active_lock_owner,
    )
    if not decision.allowed:
        return decision
    state = decision.requested
    action.remediation_state = state.value
    action.status = _LEGACY_ACTION_STATUS[state]
    if case is not None:
        case.outcome = {
            RemediationState.PROPOSED: "PROPOSED",
            RemediationState.APPROVED: "APPROVED",
            RemediationState.EXECUTING: "EXECUTING",
            RemediationState.VERIFYING: "EXECUTED_PENDING_VERIFY",
            RemediationState.SUCCEEDED: "VERIFIED_SUCCESS",
            RemediationState.FAILED: "VERIFIED_FAILED",
            RemediationState.ROLLED_BACK: "ROLLED_BACK",
            RemediationState.INCONCLUSIVE: "INCONCLUSIVE",
            RemediationState.EXPIRED: "EXPIRED",
            RemediationState.CANCELLED: "CANCELLED",
        }[state]
    if incident is not None:
        from shared.models import IncidentStatus
        incident.status = {
            RemediationState.PROPOSED: IncidentStatus.NEW.value,
            RemediationState.APPROVED: IncidentStatus.APPROVED.value,
            RemediationState.EXECUTING: IncidentStatus.EXECUTING.value,
            RemediationState.VERIFYING: IncidentStatus.VERIFYING.value,
            RemediationState.SUCCEEDED: IncidentStatus.RESOLVED.value,
            RemediationState.FAILED: IncidentStatus.FAILED.value,
            RemediationState.ROLLED_BACK: IncidentStatus.FAILED.value,
            RemediationState.INCONCLUSIVE: IncidentStatus.FAILED.value,
            RemediationState.EXPIRED: IncidentStatus.REJECTED.value,
            RemediationState.CANCELLED: IncidentStatus.REJECTED.value,
        }[state]
    return decision


def canonical_state(
    *, action_status: str | None = None,
    case_outcome: str | None = None,
    incident_status: str | None = None,
) -> RemediationState:
    """Map legacy persisted values to the canonical lifecycle."""
    statuses = {
        str(action_status or "").upper(),
        str(case_outcome or "").upper(),
        str(incident_status or "").upper(),
    }
    if "VERIFIED_SUCCESS" in statuses or "RESOLVED" in statuses or "AUTO_FIXED" in statuses:
        return RemediationState.SUCCEEDED
    if "ROLLED_BACK" in statuses:
        return RemediationState.ROLLED_BACK
    if "INCONCLUSIVE" in statuses:
        return RemediationState.INCONCLUSIVE
    if "VERIFIED_FAILED" in statuses or "FAILED" in statuses:
        return RemediationState.FAILED
    if "VERIFYING" in statuses or "EXECUTED_PENDING_VERIFY" in statuses:
        return RemediationState.VERIFYING
    if "EXECUTING" in statuses:
        return RemediationState.EXECUTING
    if "APPROVED" in statuses:
        return RemediationState.APPROVED
    if "EXPIRED" in statuses:
        return RemediationState.EXPIRED
    if "CANCELLED" in statuses or "REJECTED" in statuses:
        return RemediationState.CANCELLED
    return RemediationState.PROPOSED
