from dataclasses import replace

from worker.policy.playbook_registry import (
    PLAYBOOKS, evaluate_auto_execution, get_contract, validate_contract,
)


def test_every_incident_ai_playbook_has_a_structurally_valid_contract():
    assert PLAYBOOKS
    for action_id, contract in PLAYBOOKS.items():
        assert contract.action_id == action_id
        assert validate_contract(contract) == ()


def test_complete_safe_playbook_can_reach_l3():
    decision = evaluate_auto_execution("resync_ntp", "SAFE")
    assert decision.allowed is True
    assert decision.contract.version == "1"
    assert decision.effective_max_autonomy == "L3"


def test_unknown_playbook_fails_closed_at_l2():
    decision = evaluate_auto_execution("model_invented_action", "SAFE")
    assert decision.allowed is False
    assert decision.contract is None
    assert decision.effective_max_autonomy == "L2"


def test_missing_contract_hooks_are_capped_at_l2():
    contract = get_contract("resync_ntp")
    original = PLAYBOOKS["resync_ntp"]
    try:
        PLAYBOOKS["resync_ntp"] = replace(contract, postcheck=None)
        decision = evaluate_auto_execution("resync_ntp", "SAFE")
        assert decision.allowed is False
        assert decision.effective_max_autonomy == "L2"
        assert "lacks" in decision.reason
    finally:
        PLAYBOOKS["resync_ntp"] = original


def test_runtime_classification_and_contract_ceiling_both_gate_execution():
    decision = evaluate_auto_execution("finalize_osd_release", "SAFE")
    assert decision.allowed is False
    assert decision.effective_max_autonomy == "L2"
    assert "ceiling" in decision.reason

    decision = evaluate_auto_execution("restart_osd_daemon", "RISKY")
    assert decision.allowed is False
    assert "not auto-executable" in decision.reason
