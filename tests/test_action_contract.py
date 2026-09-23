from datetime import datetime, timedelta, timezone

import pytest

from worker.executor.action_contract import (
    ActionContractError,
    TargetScope,
    TypedActionGateway,
    TypedActionRequest,
    execution_fingerprint,
)


NOW = datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)


def _request(**overrides):
    payload = {
        "action_id": "rbd_trash_move_volume",
        "cluster_id": "cluster-1",
        "capability": "rbd.trash.move",
        "target_type": "volume",
        "target_id": "vol-1",
        "params": {"pool": "volumes", "image": "image-1"},
        "evidence_fingerprint": "a" * 64,
        "expires_at": NOW + timedelta(minutes=10),
        "actor": "operator:admin",
    }
    payload.update(overrides)
    return payload


def _gateway(*, approval_required=()):
    return TypedActionGateway(
        allowed_action_ids={"rbd_trash_move_volume"},
        allowed_capabilities={"rbd.trash.move"},
        target_scope=TargetScope(cluster_id="cluster-1", volumes={"vol-1"}),
        approval_required=set(approval_required),
    )


def test_valid_typed_action_is_authorized():
    result = _gateway().authorize(_request(), now=NOW)
    assert result.action_id == "rbd_trash_move_volume"


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("action_id", "free_shell", "allowlist"),
        ("capability", "admin.root", "capability"),
        ("cluster_id", "cluster-2", "cluster_id"),
        ("target_id", "vol-outside", "target"),
        ("expires_at", NOW - timedelta(seconds=1), "hết hạn"),
    ],
)
def test_gateway_rejects_out_of_contract_values(field, value, expected):
    with pytest.raises(ActionContractError, match=expected):
        _gateway().authorize(_request(**{field: value}), now=NOW)


def test_gateway_rejects_free_form_command_params():
    with pytest.raises(ValueError, match="free-form command"):
        TypedActionRequest.model_validate(_request(params={"command": "rm -rf /"}))


def test_approval_requires_server_owned_matching_fingerprint():
    request = TypedActionRequest.model_validate(_request())
    fingerprint = execution_fingerprint(request)
    request = request.model_copy(update={"approval_fingerprint": fingerprint})

    with pytest.raises(ActionContractError, match="approval server-side"):
        _gateway(approval_required={request.action_id}).authorize(request, now=NOW)

    assert _gateway(approval_required={request.action_id}).authorize(
        request, approved_fingerprint=fingerprint, now=NOW,
    ).approval_fingerprint == fingerprint


def test_capability_matrix_callback_is_fail_closed():
    gateway = TypedActionGateway(
        allowed_action_ids={"rbd_trash_move_volume"},
        allowed_capabilities={"rbd.trash.move"},
        target_scope=TargetScope(cluster_id="cluster-1", volumes={"vol-1"}),
        capability_check=lambda _request: False,
    )
    with pytest.raises(ActionContractError, match="capability matrix"):
        gateway.authorize(_request(), now=NOW)


def test_capability_matrix_error_is_fail_closed():
    def unavailable(_request):
        raise RuntimeError("inventory unavailable")

    gateway = TypedActionGateway(
        allowed_action_ids={"rbd_trash_move_volume"},
        allowed_capabilities={"rbd.trash.move"},
        target_scope=TargetScope(cluster_id="cluster-1", volumes={"vol-1"}),
        capability_check=unavailable,
    )
    with pytest.raises(ActionContractError, match="không khả dụng"):
        gateway.authorize(_request(), now=NOW)


def test_extra_fields_are_rejected():
    with pytest.raises(ValueError):
        TypedActionRequest.model_validate(_request(untrusted="yes"))
