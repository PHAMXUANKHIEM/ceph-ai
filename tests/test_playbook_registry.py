from dataclasses import replace

from worker.policy.playbook_registry import (
    PLAYBOOKS, POSTCHECK_HOOKS, PREFLIGHT_HOOKS, describe_contract,
    evaluate_auto_execution, get_contract, registry_coverage,
    registry_status_rows, resolve_case_postcheck, run_postcheck, validate_contract,
)


def test_every_incident_ai_playbook_has_a_structurally_valid_contract():
    assert PLAYBOOKS
    for action_id, contract in PLAYBOOKS.items():
        assert contract.action_id == action_id
        assert validate_contract(contract) == ()


def test_complete_safe_playbook_can_reach_l3():
    decision = evaluate_auto_execution(
        "resync_ntp", "SAFE", target_nodes=["mon-a"], command_builder_available=True,
    )
    assert decision.allowed is True
    assert decision.contract.version == "3"
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
        decision = evaluate_auto_execution(
            "resync_ntp", "SAFE", target_nodes=["mon-a"], command_builder_available=True,
        )
        assert decision.allowed is False
        assert decision.effective_max_autonomy == "L2"
        assert "lacks" in decision.reason
    finally:
        PLAYBOOKS["resync_ntp"] = original


def test_runtime_classification_and_contract_ceiling_both_gate_execution():
    decision = evaluate_auto_execution(
        "finalize_osd_release", "SAFE", target_nodes=["mon-a"], command_builder_available=True,
    )
    assert decision.allowed is False
    assert decision.effective_max_autonomy == "L2"
    assert "ceiling" in decision.reason

    decision = evaluate_auto_execution(
        "restart_osd_daemon", "RISKY", target_nodes=["mon-a"],
        action_params={"cephadm_osd_ids": [1]}, command_builder_available=True,
    )
    assert decision.allowed is False
    assert "not auto-executable" in decision.reason


def test_runtime_target_and_builder_fail_closed_before_l3():
    missing_builder = evaluate_auto_execution(
        "resync_ntp", "SAFE", target_nodes=["mon-a"], command_builder_available=False,
    )
    assert missing_builder.allowed is False
    assert "builder" in missing_builder.reason

    too_many_hosts = evaluate_auto_execution(
        "resync_ntp", "SAFE", target_nodes=["mon-a", "mon-b", "mon-c"],
        command_builder_available=True,
    )
    assert too_many_hosts.allowed is False
    assert "blast-radius" in too_many_hosts.reason

    malformed = evaluate_auto_execution(
        "resync_ntp", "SAFE", target_nodes=None, command_builder_available=True,
    )
    assert malformed.allowed is False
    assert malformed.hard_failure is True


def test_osd_schema_allows_any_number_of_deterministic_osd_ids():
    allowed = evaluate_auto_execution(
        "restart_osd_daemon", "SAFE", target_nodes=["osd-host"],
        action_params={"cephadm_osd_ids": [4]}, command_builder_available=True,
    )
    assert allowed.allowed is True

    bounded = evaluate_auto_execution(
        "restart_osd_daemon", "SAFE", target_nodes=["osd-host"],
        action_params={"cephadm_osd_ids": [4, 5, 6]}, command_builder_available=True,
    )
    assert bounded.allowed is True

    all_verified = evaluate_auto_execution(
        "restart_osd_daemon", "SAFE", target_nodes=["osd-host-a", "osd-host-b"],
        action_params={"cephadm_osd_ids": [4, 5, 6, 7]}, command_builder_available=True,
    )
    assert all_verified.allowed is True


def test_admin_description_explains_static_eligibility_without_runtime_target():
    ready = describe_contract(get_contract("resync_ntp"), command_builder_available=True)
    assert ready["eligibility_status"] == "L3_READY"
    assert ready["policy_classification"] == "SAFE"

    conditional = describe_contract(
        get_contract("restart_osd_daemon"), command_builder_available=True,
    )
    assert conditional["eligibility_status"] == "CONDITIONAL"
    assert "BlueStore" in conditional["eligibility_reason"]

    manual = describe_contract(
        get_contract("investigate_manually"), command_builder_available=False,
    )
    assert manual["eligibility_status"] == "L2_ONLY"


def test_registry_status_rows_are_stable_and_sorted():
    rows = registry_status_rows(command_available=lambda action_id: action_id != "pg_repair_force")
    assert [row["action_id"] for row in rows] == sorted(PLAYBOOKS)
    assert all(len(row["contract_checksum"]) == 64 for row in rows)


def test_hook_names_must_resolve_in_closed_server_catalogue():
    original = get_contract("resync_ntp")
    invalid_preflight = replace(original, preflight="model_invented_preflight")
    invalid_postcheck = replace(original, postcheck="model_invented_postcheck")
    assert "unregistered preflight" in " ".join(validate_contract(invalid_preflight))
    assert "unregistered postcheck" in " ".join(validate_contract(invalid_postcheck))
    assert original.preflight in PREFLIGHT_HOOKS
    assert original.postcheck in POSTCHECK_HOOKS


def test_unregistered_hook_fails_closed_to_l2():
    original = PLAYBOOKS["resync_ntp"]
    try:
        PLAYBOOKS["resync_ntp"] = replace(original, postcheck="unknown_hook")
        decision = evaluate_auto_execution(
            "resync_ntp", "SAFE", target_nodes=["mon-a"], command_builder_available=True,
        )
        assert decision.allowed is False
        assert decision.effective_max_autonomy == "L2"
        assert "unregistered postcheck" in decision.reason
    finally:
        PLAYBOOKS["resync_ntp"] = original


def test_registry_coverage_reports_missing_actions_sorted():
    from worker.llm.router_client import AI_EXECUTABLE_ACTION_IDS

    assert registry_coverage(AI_EXECUTABLE_ACTION_IDS) == ()
    assert registry_coverage({"resync_ntp", "missing_z", "missing_a"}) == (
        "missing_a", "missing_z",
    )


def test_case_postcheck_resolution_uses_frozen_contract_and_fails_closed():
    snapshot = {"registry": get_contract("resync_ntp").snapshot(), "registered": True}
    hook_id, error = resolve_case_postcheck(
        action_id="resync_ntp", playbook_version="3", contract_snapshot=snapshot,
    )
    assert error is None and hook_id == "fresh_health_telemetry"
    assert run_postcheck(hook_id, fault_present=False, health={}).outcome == "PASSED"
    assert run_postcheck(hook_id, fault_present=True, health={}).outcome == "FAILED"

    hook_id, error = resolve_case_postcheck(
        action_id="resync_ntp", playbook_version="1", contract_snapshot=snapshot,
    )
    assert hook_id is None and "version" in error
