from shared.controlled_action_contract import allowed_domains, evaluate_controlled_action


def _ready(**overrides):
    values = {"target_count": 1, "approved": True, "capability_verified": True, "target_verified": True, "fresh_telemetry": True, "shadow_passed": True}
    values.update(overrides)
    return values


def test_controlled_action_requires_all_gates_and_never_returns_command():
    blocked = evaluate_controlled_action("rbd_resize_volume", **_ready(shadow_passed=False))
    assert not blocked.allowed and "shadow" in blocked.reason
    ready = evaluate_controlled_action("rbd_resize_volume", **_ready())
    assert ready.allowed and ready.execution_command is None and ready.audit_required


def test_unknown_and_destructive_actions_fail_closed():
    unknown = evaluate_controlled_action("model_invented_action", **_ready())
    assert not unknown.allowed
    destructive = evaluate_controlled_action("vitastor_snapshot_delete", **_ready(approved=False))
    assert not destructive.allowed and "approval" in destructive.reason
    assert "RGW" in allowed_domains() and "VITASTOR" in allowed_domains()
