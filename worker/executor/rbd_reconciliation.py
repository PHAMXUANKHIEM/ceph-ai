"""Deterministic post-checks for approval-gated RBD lifecycle actions."""

import json

from worker.executor.ssh_executor import ExecutorError


RBD_RECONCILED_ACTION_IDS = frozenset({
    "rbd_create_volume",
    "rbd_resize_volume",
    "rbd_rename_volume",
    "rbd_trash_move_volume",
    "rbd_trash_restore_volume",
    "rbd_trash_purge_all",
})


def _json_output(output: str):
    try:
        return json.loads(output.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ExecutorError("RBD post-check did not return valid JSON") from exc


def reconcile(action_id: str, params: dict, output: str) -> None:
    """Raise when Ceph's post-check output disagrees with approved intent."""
    if action_id not in RBD_RECONCILED_ACTION_IDS:
        return
    payload = _json_output(output)

    if action_id in {"rbd_create_volume", "rbd_resize_volume"}:
        if not isinstance(payload, dict) or payload.get("name") != params.get("image"):
            raise ExecutorError("RBD post-check returned a different image")
        size_mib = params.get("size_mib")
        if isinstance(size_mib, bool) or not isinstance(size_mib, int):
            raise ExecutorError("RBD post-check is missing approved size_mib")
        expected_size = size_mib * 1024 * 1024
        try:
            actual_size = int(payload.get("size"))
        except (TypeError, ValueError):
            actual_size = -1
        if actual_size != expected_size:
            raise ExecutorError(
                f"RBD post-check size mismatch: expected {expected_size}, got {actual_size}"
            )
        return

    if action_id in {"rbd_rename_volume", "rbd_trash_restore_volume"}:
        expected_name = params.get("new_image") if action_id == "rbd_rename_volume" else params.get("image")
        if not isinstance(payload, dict) or payload.get("name") != expected_name:
            raise ExecutorError("RBD post-check did not find the expected destination image")
        return

    if not isinstance(payload, list):
        raise ExecutorError("RBD trash post-check returned an unexpected payload")
    if action_id == "rbd_trash_move_volume":
        if not any(isinstance(row, dict) and row.get("name") == params.get("image") for row in payload):
            raise ExecutorError("RBD post-check did not find the image in trash")
        return

    remaining_ids = {
        str(row.get("id")) for row in payload if isinstance(row, dict) and row.get("id") is not None
    }
    expected_removed = {str(value) for value in params.get("trash_ids", [])}
    still_present = sorted(expected_removed.intersection(remaining_ids))
    if still_present:
        raise ExecutorError("RBD purge post-check still contains trash IDs: " + ", ".join(still_present))
