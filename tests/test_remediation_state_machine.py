from datetime import datetime, timedelta

from shared.remediation_state_machine import (
    RemediationState, canonical_state, recover_after_worker_restart, transition,
)


def test_happy_path_requires_lock_for_execution_and_verification():
    now = datetime(2026, 9, 21, 10, 0, 0)
    assert transition("PROPOSED", "APPROVED", now=now).allowed
    assert transition("APPROVED", "EXECUTING", now=now, lock_owner="w1", active_lock_owner="w1").allowed
    assert transition("EXECUTING", "VERIFYING", now=now, lock_owner="w1", active_lock_owner="w1").allowed
    assert transition("VERIFYING", "SUCCEEDED", now=now).allowed


def test_invalid_transition_lock_mismatch_and_expiry_fail_closed():
    now = datetime(2026, 9, 21, 10, 0, 0)
    assert not transition("PROPOSED", "EXECUTING", now=now).allowed
    decision = transition("APPROVED", "EXECUTING", now=now, lock_owner="w1", active_lock_owner="w2")
    assert not decision.allowed and "another worker" in decision.reason
    expired = transition("APPROVED", "EXECUTING", now=now, expires_at=now - timedelta(seconds=1), lock_owner="w1", active_lock_owner="w1")
    assert not expired.allowed and "expired" in expired.reason
    assert transition("APPROVED", "EXPIRED", now=now, expires_at=now - timedelta(seconds=1)).allowed


def test_restart_recovery_never_retries_a_lost_write():
    decision = recover_after_worker_restart("EXECUTING", lock_present=False)
    assert decision.allowed and decision.requested == RemediationState.INCONCLUSIVE
    assert not recover_after_worker_restart("EXECUTING", lock_present=True).allowed


def test_legacy_values_map_to_canonical_lifecycle():
    assert canonical_state(action_status="APPROVED") == RemediationState.APPROVED
    assert canonical_state(case_outcome="EXECUTED_PENDING_VERIFY") == RemediationState.VERIFYING
    assert canonical_state(case_outcome="VERIFIED_SUCCESS") == RemediationState.SUCCEEDED
    assert canonical_state(action_status="REJECTED") == RemediationState.CANCELLED
