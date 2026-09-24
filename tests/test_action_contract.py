from datetime import datetime, timedelta, timezone

import pytest

from worker.executor.action_contract import (
    ActionContractError,
    RBD_COPY_VOLUME_CONTRACT,
    RbdCopyVolumeParams,
    TargetScope,
    TypedActionGateway,
    TypedActionRequest,
    execution_fingerprint,
    validate_typed_action_params,
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


def _rbd_copy_request(**overrides):
    payload = {
        "action_id": "rbd_copy_volume",
        "cluster_id": "cluster-1",
        "capability": RBD_COPY_VOLUME_CONTRACT["capability"],
        "target_type": "volume",
        "target_id": "images/vm-source",
        "params": {
            "pool_name": "images",
            "image": "vm-source",
            "snapshot": "snap-20260924",
            "dest_pool": "vms",
            "dest_image": "vm-copy",
            "size_bytes": 1024,
            "idempotency_key": "copy-key-01",
            "requested_by": "admin",
        },
        "evidence_fingerprint": "b" * 64,
        "expires_at": NOW + timedelta(minutes=10),
        "actor": "operator:admin",
    }
    payload.update(overrides)
    return payload


def test_rbd_copy_has_strict_typed_params_and_requires_approval():
    params = RbdCopyVolumeParams.model_validate(_rbd_copy_request()["params"])
    assert params.dest_pool == "vms"
    assert params.size_bytes == 1024
    gateway = TypedActionGateway(
        allowed_action_ids={"rbd_copy_volume"},
        allowed_capabilities={RBD_COPY_VOLUME_CONTRACT["capability"]},
        target_scope=TargetScope(cluster_id="cluster-1", volumes={"images/vm-source"}),
        approval_required={"rbd_copy_volume"},
    )
    request = TypedActionRequest.model_validate(_rbd_copy_request())
    fingerprint = execution_fingerprint(request)
    request = request.model_copy(update={"approval_fingerprint": fingerprint})

    with pytest.raises(ActionContractError, match="approval server-side"):
        gateway.authorize(request, now=NOW)
    assert gateway.authorize(
        request, approved_fingerprint=fingerprint, now=NOW,
    ).action_id == "rbd_copy_volume"


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"pool_name": "images", "image": "vm-source", "snapshot": "snap", "dest_pool": "images", "dest_image": "copy", "size_bytes": 1}, "pool"),
        ({"pool_name": "images", "image": "volume-123", "snapshot": "snap", "dest_pool": "vms", "dest_image": "copy", "size_bytes": 1}, "Cinder"),
        ({"pool_name": "images", "image": "vm-source", "snapshot": "snap", "dest_pool": "vms", "dest_image": "copy", "size_bytes": 0}, "greater than 0"),
    ],
)
def test_rbd_copy_rejects_invalid_typed_params(params, expected):
    with pytest.raises((ActionContractError, ValueError), match=expected):
        validate_typed_action_params("rbd_copy_volume", params)


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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_gateway_rejects_nonfinite_numbers_at_any_depth(value):
    with pytest.raises(ValueError, match="NaN hoặc Infinity"):
        TypedActionRequest.model_validate(_request(params={"limits": [{"iops": value}]}))


def test_gateway_accepts_finite_number():
    request = TypedActionRequest.model_validate(_request(params={"iops": 100.5}))
    assert request.params["iops"] == 100.5


def test_multi_action_gateway_requires_action_capability_binding():
    options = dict(
        allowed_action_ids={"rbd_trash_move_volume", "rbd_resize_volume"},
        allowed_capabilities={"rbd.trash.move", "rbd.resize"},
        target_scope=TargetScope(cluster_id="cluster-1", volumes={"vol-1"}),
    )
    with pytest.raises(ActionContractError, match="mapping action-capability"):
        TypedActionGateway(**options).authorize(_request(), now=NOW)

    gateway = TypedActionGateway(
        **options,
        capabilities_by_action={
            "rbd_trash_move_volume": {"rbd.trash.move"},
            "rbd_resize_volume": {"rbd.resize"},
        },
    )
    with pytest.raises(ActionContractError, match="không khớp action_id"):
        gateway.authorize(_request(capability="rbd.resize"), now=NOW)
    assert gateway.authorize(_request(), now=NOW).action_id == "rbd_trash_move_volume"


def test_gateway_revalidates_preconstructed_model_instances():
    valid = TypedActionRequest.model_validate(_request())
    forged = valid.model_copy(update={"params": {"nested": {"command": "arbitrary shell"}}})
    with pytest.raises(ActionContractError, match="free-form command"):
        _gateway().authorize(forged, now=NOW)


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


def test_capability_matrix_rejects_truthy_non_boolean_result():
    gateway = TypedActionGateway(
        allowed_action_ids={"rbd_trash_move_volume"},
        allowed_capabilities={"rbd.trash.move"},
        target_scope=TargetScope(cluster_id="cluster-1", volumes={"vol-1"}),
        capability_check=lambda _request: "UNKNOWN",
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
