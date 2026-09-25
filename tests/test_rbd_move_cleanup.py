import json

import pytest

from worker.executor import rbd_move_cleanup
from worker.executor.action_contract import (
    ActionContractError,
    RbdMoveCleanupPartialParams,
    validate_typed_action_params,
)
from worker.executor.commands import get_command
from worker.executor.rbd_reconciliation import reconcile, reconciliation_command
from worker.executor.ssh_executor import ExecutorError


def _params():
    return {
        "pool_name": "vms",
        "image": "vm-old",
        "dest_pool": "images",
        "dest_image": "vm-new",
        "size_bytes": 1024,
        "delete_destination": True,
        "move_token": "move-token-20260925-01",
        "checksum": "rbd-export-diff-sha256",
        "cleanup_of_action_id": "move-action-1",
    }


def test_cleanup_command_only_deletes_token_owned_destination():
    command = get_command("rbd_move_cleanup_partial", params=_params())

    assert "rbd image-meta get images/vm-new" in command
    assert "ceph.ai.move_token" in command
    assert "rbd rm images/vm-new" in command
    assert "rbd rm vms/vm-old" not in command
    with pytest.raises(ExecutorError, match="explicit destination-delete"):
        get_command("rbd_move_cleanup_partial", params={**_params(), "delete_destination": False})
    with pytest.raises(ExecutorError, match="token"):
        get_command("rbd_move_cleanup_partial", params={**_params(), "move_token": "wrong"})


def test_cleanup_reconciliation_requires_source_and_verified_destination_ownership():
    params = _params()
    reconcile(
        "rbd_move_cleanup_partial",
        params,
        '{"name":"vm-new","size":1024,"source_present":true,"destination_deleted":true,"marker_verified":true}',
    )
    with pytest.raises(ExecutorError, match="source preservation"):
        reconcile(
            "rbd_move_cleanup_partial",
            params,
            '{"name":"vm-new","size":1024,"source_present":false,"destination_deleted":true,"marker_verified":true}',
        )
    with pytest.raises(ExecutorError, match="destination deletion"):
        reconcile(
            "rbd_move_cleanup_partial",
            params,
            '{"name":"vm-new","size":1024,"source_present":true,"destination_deleted":false,"marker_verified":true}',
        )
    recovery = reconciliation_command("rbd_move_cleanup_partial", params)
    assert "rbd rm" not in recovery
    assert "destination still exists" in recovery


def test_cleanup_executor_persists_phases(monkeypatch):
    writes = []
    monkeypatch.setattr(
        rbd_move_cleanup,
        "execute_command",
        lambda *args, **kwargs: json.dumps({
            "name": "vm-new",
            "size": 1024,
            "source_present": True,
            "destination_deleted": True,
            "marker_verified": True,
        }),
    )

    succeeded, command, output = rbd_move_cleanup.run(
        "cleanup-1", _params(), "10.0.0.1", "ceph", "/key", "none", "",
        lambda action_pk, progress: writes.append((action_pk, json.loads(json.dumps(progress)))),
    )

    assert succeeded is True
    assert command
    assert json.loads(output)["destination_deleted"] is True
    assert len(writes) == 4
    assert writes[0][1][0]["status"] == "running"
    assert writes[-1][1][-1]["status"] == "done"


def test_cleanup_contract_is_strict_and_approval_only():
    RbdMoveCleanupPartialParams.model_validate(_params())
    validate_typed_action_params("rbd_move_cleanup_partial", _params())
    with pytest.raises(ActionContractError):
        validate_typed_action_params("rbd_move_cleanup_partial", {**_params(), "delete_destination": False})
    with pytest.raises(ActionContractError):
        validate_typed_action_params("rbd_move_cleanup_partial", {**_params(), "unexpected": True})
