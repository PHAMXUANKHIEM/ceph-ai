from shared.autopilot_guardrails import (
    GuardrailContext, evaluate_guardrails, operator_snapshot, resolve_mode,
)


def _ctx(**overrides):
    values = {"mode": "LIMITED_AUTOPILOT", "kill_switch": True, "cluster_enabled": True, "action_id": "resync_ntp", "classification": "SAFE", "allowlisted": True, "target_count": 1, "max_targets": 2, "actions_used": 0, "action_budget": 5, "cooldown_active": False, "health_status": "HEALTH_OK", "maintenance_window_open": True}
    values.update(overrides)
    return GuardrailContext(**values)


def test_limited_autopilot_requires_every_guardrail():
    assert evaluate_guardrails(_ctx()).allowed
    for key, value in (("kill_switch", False), ("cluster_enabled", False), ("allowlisted", False), ("maintenance_window_open", False), ("cooldown_active", True)):
        assert not evaluate_guardrails(_ctx(**{key: value})).allowed


def test_mode_and_health_and_budget_are_fail_closed():
    assert not evaluate_guardrails(_ctx(mode="ADVISORY")).allowed
    assert not evaluate_guardrails(_ctx(classification="RISKY")).allowed
    assert not evaluate_guardrails(_ctx(actions_used=5)).allowed
    assert not evaluate_guardrails(_ctx(health_status="HEALTH_ERR")).allowed
    assert not evaluate_guardrails(_ctx(mode="UNKNOWN")).allowed


def test_mode_resolution_and_operator_snapshot_are_explainable():
    assert resolve_mode(global_enabled=False, cluster_enabled=True).value == "APPROVAL_REQUIRED"
    assert resolve_mode(global_enabled=True, cluster_enabled=True).value == "LIMITED_AUTOPILOT"
    assert resolve_mode(global_enabled=True, cluster_enabled=True, advisory_only=True).value == "ADVISORY"
    snapshot = operator_snapshot(_ctx(maintenance_window_open=False))
    assert snapshot["decision"] == "BLOCKED"
    assert snapshot["reason"] == "outside the maintenance window"
    assert snapshot["controls"]["action_budget"] == 5
