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
