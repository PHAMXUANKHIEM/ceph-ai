import json

import pytest

from worker.executor.rbd_reconciliation import reconcile
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
