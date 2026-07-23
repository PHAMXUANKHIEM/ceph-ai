from shared.models import ActionClassification
from worker.policy.gate import classify_action


def test_classify_safe_action_id_returns_safe():
    assert classify_action("resync_ntp") == ActionClassification.SAFE


def test_classify_risky_action_id_returns_risky():
    assert classify_action("restart_osd_daemon") == ActionClassification.RISKY
    assert classify_action("pg_repair_force") == ActionClassification.RISKY


def test_classify_action_absent_from_both_lists_returns_risky():
    # investigate_manually is a real action_id (in action_ids:) but
    # deliberately absent from both safe: and risky: — must still be RISKY.
    assert classify_action("investigate_manually") == ActionClassification.RISKY


def test_classify_completely_unknown_action_id_returns_risky():
    assert classify_action("this_action_id_does_not_exist_anywhere") == ActionClassification.RISKY


def test_classify_action_id_listed_in_both_safe_and_risky_is_conservative_risky(monkeypatch):
    import worker.policy.gate as gate

    monkeypatch.setattr(gate, "SAFE_ACTION_IDS", frozenset({"conflicted_action"}))
    monkeypatch.setattr(gate, "RISKY_ACTION_IDS", frozenset({"conflicted_action"}))

    assert gate.classify_action("conflicted_action") == ActionClassification.RISKY


def test_current_policy_yaml_has_no_safe_risky_conflicts():
    # Regression guard: the real action_policy.yaml should never list the
    # same action_id as both safe and risky (would trigger gate.py's
    # startup warning and rely on the conservative-override fallback).
    import worker.policy.gate as gate

    assert gate.SAFE_ACTION_IDS.isdisjoint(gate.RISKY_ACTION_IDS)


def test_risky_action_ids_loaded_from_policy_yaml():
    import worker.policy.gate as gate

    assert "restart_osd_daemon" in gate.RISKY_ACTION_IDS
    assert "pg_repair_force" in gate.RISKY_ACTION_IDS


def test_management_action_ids_loaded_from_policy_yaml():
    import worker.policy.gate as gate

    assert gate.VALID_MANAGEMENT_ACTION_IDS == {
        "create_pool",
        "delete_pool",
        "set_pool_size",
        "set_pool_pg_num",
        "mark_osd_out",
        "mark_osd_in",
        "mark_osd_down",
        "enable_pool_application",
    }


def test_management_action_ids_are_classified_safe():
    for action_id in [
        "create_pool",
        "delete_pool",
        "set_pool_size",
        "set_pool_pg_num",
        "mark_osd_out",
        "mark_osd_in",
        "mark_osd_down",
        "enable_pool_application",
    ]:
        assert classify_action(action_id) == ActionClassification.SAFE


def test_management_action_ids_disjoint_from_incident_diagnosis_action_ids():
    # Guards the deliberate separation (action_policy.yaml's own comment):
    # management_action_ids must never leak into worker/llm/router_client.py's
    # VALID_ACTION_IDS (the Incident-diagnosis tool schema enum).
    import worker.policy.gate as gate
    from worker.llm.router_client import VALID_ACTION_IDS

    assert gate.VALID_MANAGEMENT_ACTION_IDS.isdisjoint(VALID_ACTION_IDS)
