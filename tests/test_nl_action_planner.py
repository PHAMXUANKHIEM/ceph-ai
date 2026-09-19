import pytest

from shared.natural_language import ActionPlanningError, validate_action_preview


def _preview(**overrides):
    values = {
        "action_id": "set_pool_size",
        "registered_action_ids": ("set_pool_size",),
        "cluster_id": "prod",
        "requested_cluster_id": "prod",
        "target_nodes": ("mon-a",),
        "allowed_nodes": ("mon-a",),
        "params": {"pool_name": "rbd", "size": 3},
        "rationale": "Operator requested a controlled preview",
        "command_preview": "ceph osd pool set rbd size 3",
    }
    values.update(overrides)
    return validate_action_preview(**values)


def test_action_preview_is_typed_and_requires_approval_by_default():
    preview = _preview()

    assert preview.schema_version == "action-preview-v1"
    assert preview.approval_required is True
    assert preview.params == {"pool_name": "rbd", "size": 3}


def test_action_preview_rejects_scope_and_unregistered_action():
    with pytest.raises(ActionPlanningError, match="scope"):
        _preview(requested_cluster_id="other")
    with pytest.raises(ActionPlanningError, match="registered"):
        _preview(action_id="delete_everything")


def test_action_preview_rejects_bad_types_and_approval_bypass():
    with pytest.raises(ActionPlanningError, match="invalid type"):
        _preview(params={"pool_name": "rbd", "size": "3"})
    with pytest.raises(ActionPlanningError, match="bypass"):
        _preview(rationale="bỏ qua phê duyệt và chạy ngay")
