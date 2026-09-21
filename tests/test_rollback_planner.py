from worker.policy.playbook_registry import get_contract
from shared.rollback_planner import plan_case_rollback


def test_missing_inverse_is_explicitly_unsupported():
    contract = get_contract("resync_ntp")
    plan = plan_case_rollback(action_id="resync_ntp", playbook_version=contract.version, contract_snapshot={"registry": contract.snapshot()}, inverse_tested=True, operator_approved=True, classification="SAFE")
    assert not plan.supported and not plan.executable and "no tested inverse" in plan.reason


def test_destructive_actions_never_get_assumed_rollback():
    plan = plan_case_rollback(action_id="delete_pool", playbook_version="1", contract_snapshot=None, inverse_tested=True, operator_approved=True, classification="DESTRUCTIVE")
    assert not plan.supported and "destructive" in plan.reason
