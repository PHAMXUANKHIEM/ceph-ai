import json

import pytest

from worker.executor.rbd_reconciliation import reconcile, reconciliation_command
from worker.executor.ssh_executor import ExecutorError


def test_reconcile_create_and_resize_require_exact_image_and_size():
    output = json.dumps({"name": "vm-01", "size": 10 * 1024 * 1024})

    reconcile("rbd_create_volume", {"image": "vm-01", "size_mib": 10}, output)
    reconcile("rbd_resize_volume", {"image": "vm-01", "size_mib": 10}, output)

    with pytest.raises(ExecutorError, match="size mismatch"):
        reconcile("rbd_resize_volume", {"image": "vm-01", "size_mib": 11}, output)


def test_reconcile_rename_and_restore_require_destination_name():
    reconcile("rbd_rename_volume", {"new_image": "vm-new"}, '{"name":"vm-new"}')
    reconcile("rbd_trash_restore_volume", {"image": "vm-restored"}, '{"name":"vm-restored"}')

    with pytest.raises(ExecutorError, match="destination"):
        reconcile("rbd_rename_volume", {"new_image": "other"}, '{"name":"vm-new"}')


def test_reconcile_clone_and_flatten_require_expected_destination():
    reconcile("rbd_clone_volume", {"dest_image": "vm-copy", "size_bytes": 1024},
              '{"name":"vm-copy", "size":1024}')
    reconcile("rbd_flatten_volume", {"image": "vm-copy"}, '{"name":"vm-copy"}')
    with pytest.raises(ExecutorError, match="destination"):
        reconcile("rbd_clone_volume", {"dest_image": "other"}, '{"name":"vm-copy"}')


def test_reconcile_copy_requires_exact_destination_and_size():
    params = {"pool_name": "vms", "image": "vm", "snapshot": "gold",
              "dest_pool": "images", "dest_image": "vm-copy", "size_bytes": 1024}
    reconcile("rbd_copy_volume", params, '{"name":"vm-copy","size":1024}')
    assert reconciliation_command("rbd_copy_volume", params) == "rbd info images/vm-copy --format json"
    with pytest.raises(ExecutorError, match="size mismatch"):
        reconcile("rbd_copy_volume", params, '{"name":"vm-copy","size":512}')
    with pytest.raises(ExecutorError, match="missing approved size"):
        reconcile("rbd_copy_volume", {**params, "size_bytes": 0}, '{"name":"vm-copy","size":0}')


def test_reconcile_move_requires_verified_copy_and_source_deletion():
    params = {
        "pool_name": "vms", "image": "vm-old", "dest_pool": "images",
        "dest_image": "vm-new", "size_bytes": 1024, "delete_source": True,
        "move_token": "move-token-20260925-01",
        "checksum": "rbd-export-diff-sha256",
    }
    reconcile(
        "rbd_move_volume", params,
        '{"name":"vm-new","size":1024,"source_deleted":true,"checksum_verified":true}',
    )
    recovery = reconciliation_command("rbd_move_volume", params)
    assert "rbd info images/vm-new --format json" in recovery
    assert "rbd info vms/vm-old --format json" in recovery
    assert "rbd cp" not in recovery and "rbd rm" not in recovery
    with pytest.raises(ExecutorError, match="source deletion"):
        reconcile("rbd_move_volume", params, '{"name":"vm-new","size":1024}')
    with pytest.raises(ExecutorError, match="checksum"):
        reconcile(
            "rbd_move_volume", params,
            '{"name":"vm-new","size":1024,"source_deleted":true,"checksum_verified":false}',
        )


def test_reconcile_template_requires_protected_snapshot():
    reconcile("rbd_template_mark", {"snapshot": "gold"},
              '[{"name":"gold","protected":true}]')
    with pytest.raises(ExecutorError, match="protected snapshot"):
        reconcile("rbd_template_mark", {"snapshot": "gold"},
                  '[{"name":"gold","protected":false}]')


def test_reconcile_qos_requires_all_approved_values_and_read_only_recovery_command():
    params = {
        "pool_name": "vms", "image": "vm-01", "rbd_qos_iops_limit": 500,
        "rbd_qos_bps_limit": 0, "rbd_qos_iops_burst": 600, "rbd_qos_bps_burst": 0,
    }
    reconcile("rbd_qos_set", params, json.dumps({"options": {
        "rbd_qos_iops_limit": "500", "rbd_qos_iops_burst": "600",
    }}))
    with pytest.raises(ExecutorError, match="QoS post-check mismatch"):
        reconcile("rbd_qos_set", params, '{"options":{"rbd_qos_iops_limit":"400"}}')
    assert reconciliation_command("rbd_qos_set", params) == "rbd config image list vms/vm-01 --format json"


def test_reconcile_trash_move_and_purge_verify_membership():
    trash = json.dumps([{"id": "id-1", "name": "vm-old"}, {"id": "keep", "name": "other"}])

    reconcile("rbd_trash_move_volume", {"image": "vm-old"}, trash)
    reconcile("rbd_trash_purge_all", {"trash_ids": ["gone-1", "gone-2"]}, trash)

    with pytest.raises(ExecutorError, match="still contains"):
        reconcile("rbd_trash_purge_all", {"trash_ids": ["id-1"]}, trash)


def test_reconcile_rejects_invalid_json_but_ignores_unrelated_actions():
    reconcile("scrub_pool", {}, "not-json")
    with pytest.raises(ExecutorError, match="valid JSON"):
        reconcile("rbd_create_volume", {"image": "vm", "size_mib": 1}, "not-json")


def test_reconciliation_command_is_read_only_and_validated():
    command = reconciliation_command(
        "rbd_resize_volume", {"pool_name": "vms", "image": "vm-01", "size_mib": 10}
    )
    trash_command = reconciliation_command(
        "rbd_trash_purge_all", {"pool_name": "vms", "trash_ids": ["id-1"]}
    )
    clone_command = reconciliation_command(
        "rbd_clone_volume", {"pool_name": "vms", "dest_pool": "images", "dest_image": "vm-copy",
                              "image": "vm", "snapshot": "gold"}
    )
    template_command = reconciliation_command(
        "rbd_template_mark", {"pool_name": "vms", "image": "vm", "snapshot": "gold",
                               "template_name": "gold", "description": ""}
    )

    assert command == "rbd info vms/vm-01 --format json"
    assert trash_command == "rbd trash ls vms --format json"
    assert clone_command == "rbd info images/vm-copy --format json"
    assert template_command == "rbd snap ls vms/vm --format json"
    assert "resize" not in command
    assert " rm " not in trash_command
