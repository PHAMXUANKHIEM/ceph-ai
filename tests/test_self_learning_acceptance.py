from shared.self_learning_acceptance import evaluate_acceptance


def _ready(**overrides):
    values = {
        "lifecycle_states": ["CANDIDATE", "SHADOW", "ACTIVE", "BLOCKED", "RETIRED", "ROLLBACK"],
        "scopes": [{"cluster_id": "c", "entity_id": "h", "metric": "cpu", "horizon_hours": 24}],
        "replay_days": 14,
        "canary_hours": 72,
        "resource_budget_ok": True,
        "rollback_verified": True,
        "immutable_artifact": True,
        "operator_signoff": True,
        "security_signoff": True,
        "operations_signoff": True,
        "verified_outcomes": 20,
    }
    values.update(overrides)
    return values


def test_acceptance_gate_blocks_incomplete_evidence():
    decision = evaluate_acceptance(**_ready(replay_days=13, scopes=[{"cluster_id": "c"}]))
    assert decision.allowed is False
    assert "replay_14_days" in decision.failed_checks
    assert "scope_dimensions" in decision.failed_checks


def test_acceptance_gate_allows_only_complete_operator_review_package():
    decision = evaluate_acceptance(**_ready())
    assert decision.allowed is True
    assert decision.status == "READY_FOR_OPERATOR_REVIEW"


def test_acceptance_keeps_production_fail_closed_without_verified_outcomes():
    decision = evaluate_acceptance(**_ready(verified_outcomes=0, production_mode="ACTIVE"))
    assert decision.allowed is False
    assert "production_shadow_only_without_outcomes" in decision.failed_checks
