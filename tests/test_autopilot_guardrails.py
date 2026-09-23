from shared.autopilot_guardrails import (
    AutopilotMode, GuardrailContext, effective_runtime_mode, evaluate_guardrails,
    cluster_configured_mode, operator_snapshot, resolve_mode,
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


def test_explicit_runtime_mode_can_only_reduce_autonomous_rights():
    assert effective_runtime_mode(global_enabled=True, cluster_enabled=True) == AutopilotMode.LIMITED_AUTOPILOT
    assert effective_runtime_mode(global_enabled=True, cluster_enabled=True, configured_mode="ADVISORY") == AutopilotMode.ADVISORY
    assert effective_runtime_mode(global_enabled=True, cluster_enabled=True, configured_mode="APPROVAL_REQUIRED") == AutopilotMode.APPROVAL_REQUIRED
    assert effective_runtime_mode(global_enabled=True, cluster_enabled=True, configured_mode="typo") == AutopilotMode.ADVISORY
    assert effective_runtime_mode(global_enabled=False, cluster_enabled=True, configured_mode="LIMITED_AUTOPILOT") != AutopilotMode.LIMITED_AUTOPILOT
    assert effective_runtime_mode(global_enabled=True, cluster_enabled=False, configured_mode="LIMITED_AUTOPILOT") != AutopilotMode.LIMITED_AUTOPILOT


def test_cluster_mode_mapping_is_scoped_and_fails_closed():
    raw = '{"cluster-a":"LIMITED_AUTOPILOT", "cluster-b":"ADVISORY"}'
    assert cluster_configured_mode(cluster_id="cluster-a", raw_mapping=raw) == "LIMITED_AUTOPILOT"
    assert cluster_configured_mode(cluster_id="cluster-b", raw_mapping=raw) == "ADVISORY"
    assert cluster_configured_mode(cluster_id="cluster-new", raw_mapping=raw) == "APPROVAL_REQUIRED"
    assert cluster_configured_mode(cluster_id="cluster-a", raw_mapping="not-json") == "APPROVAL_REQUIRED"
    assert cluster_configured_mode(cluster_id="cluster-a", raw_mapping="[]") == "APPROVAL_REQUIRED"


def test_cluster_mode_mapping_keeps_legacy_fallback_when_unconfigured():
    assert cluster_configured_mode(
        cluster_id="cluster-a", raw_mapping="", fallback_mode="ADVISORY",
    ) == "ADVISORY"
