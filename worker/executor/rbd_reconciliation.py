"""Deterministic post-checks for approval-gated RBD lifecycle actions."""

import json
import shlex

from worker.executor.ssh_executor import ExecutorError


RBD_RECONCILED_ACTION_IDS = frozenset({
    "rbd_create_volume",
    "rbd_resize_volume",
    "rbd_rename_volume",
    "rbd_clone_volume",
    "rbd_copy_volume",
    "rbd_move_volume",
    "rbd_move_cleanup_partial",
    "rbd_flatten_volume",
    "rbd_template_mark",
    "rbd_qos_set",
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

    if action_id == "rbd_move_cleanup_partial":
        if not isinstance(payload, dict) or payload.get("name") != params.get("dest_image"):
            raise ExecutorError("RBD partial cleanup post-check returned a different destination")
        if payload.get("source_present") is not True:
            raise ExecutorError("RBD partial cleanup post-check did not confirm source preservation")
        if payload.get("destination_deleted") is not True:
            raise ExecutorError("RBD partial cleanup post-check did not confirm destination deletion")
        if payload.get("marker_verified") is not True:
            if not (payload.get("recovered") is True and payload.get("source_present") is True):
                raise ExecutorError("RBD partial cleanup post-check did not confirm token ownership")
        return

    if action_id in {"rbd_rename_volume", "rbd_clone_volume", "rbd_copy_volume", "rbd_move_volume", "rbd_flatten_volume", "rbd_trash_restore_volume"}:
        expected_name = (
            params.get("new_image") if action_id == "rbd_rename_volume"
            else params.get("dest_image") if action_id in {"rbd_clone_volume", "rbd_copy_volume", "rbd_move_volume"}
            else params.get("image")
        )
        if not isinstance(payload, dict) or payload.get("name") != expected_name:
            raise ExecutorError("RBD post-check did not find the expected destination image")
        if action_id in {"rbd_copy_volume", "rbd_move_volume"} or (action_id == "rbd_clone_volume" and params.get("size_bytes") is not None):
            if action_id in {"rbd_copy_volume", "rbd_move_volume"} and (not isinstance(params.get("size_bytes"), int) or params["size_bytes"] <= 0):
                raise ExecutorError("RBD copy/clone post-check is missing approved size")
            try:
                actual_size = int(payload.get("size"))
            except (TypeError, ValueError):
                actual_size = -1
            if actual_size != int(params["size_bytes"]):
                raise ExecutorError("RBD copy/clone post-check size mismatch")
        if action_id == "rbd_move_volume":
            if payload.get("source_deleted") is not True:
                raise ExecutorError("RBD move post-check did not confirm source deletion")
            if payload.get("checksum_verified") is not True:
                if not (payload.get("recovered") is True and payload.get("source_deleted") is True):
                    raise ExecutorError("RBD move post-check did not confirm checksum verification")
        return

    if action_id == "rbd_template_mark":
        if not isinstance(payload, list):
            raise ExecutorError("RBD template post-check returned an unexpected snapshot payload")
        expected = params.get("snapshot")
        row = next((item for item in payload if isinstance(item, dict) and item.get("name") == expected), None)
        protected = row.get("protected") if isinstance(row, dict) else None
        if row is None or protected not in (True, "true", "True", 1, "1"):
            raise ExecutorError("RBD template post-check did not confirm a protected snapshot")
        return

    if action_id == "rbd_qos_set":
        rows = payload.get("options") if isinstance(payload, dict) else payload
        values = {}
        if isinstance(rows, dict):
            values = rows
        elif isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = row.get("name") or row.get("key") or row.get("option")
                if key:
                    values[str(key)] = row.get("value")
        for option in (
            "rbd_qos_iops_limit", "rbd_qos_bps_limit", "rbd_qos_iops_burst", "rbd_qos_bps_burst",
            "rbd_qos_read_iops_limit", "rbd_qos_read_bps_limit",
            "rbd_qos_write_iops_limit", "rbd_qos_write_bps_limit",
        ):
            expected = int(params.get(option, 0))
            try:
                actual = int(values.get(option, 0))
            except (TypeError, ValueError):
                actual = -1
            if actual != expected:
                raise ExecutorError(f"RBD QoS post-check mismatch for {option}: expected {expected}, got {actual}")
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


def reconciliation_command(
    action_id: str,
    params: dict,
    *,
    exec_mode: str | None = None,
    container_name: str = "",
) -> str:
    """Build a validated read-only command for recovery after Worker restart."""
    if action_id not in RBD_RECONCILED_ACTION_IDS:
        raise ExecutorError(f"action {action_id!r} does not support RBD reconciliation")
    # Reuse the mutation builder only as a closed-schema validator. It is
    # never executed here; the returned command below is read-only.
    from worker.executor import commands
    commands.get_command(action_id, params=params)
    pool = shlex.quote(params["pool_name"])
    if action_id == "rbd_rename_volume":
        command = f"rbd info {pool}/{shlex.quote(params['new_image'])} --format json"
    elif action_id in {"rbd_clone_volume", "rbd_copy_volume"}:
        command = f"rbd info {shlex.quote(params['dest_pool'])}/{shlex.quote(params['dest_image'])} --format json"
    elif action_id == "rbd_move_volume":
        source = f"{pool}/{shlex.quote(params['image'])}"
        destination = f"{shlex.quote(params['dest_pool'])}/{shlex.quote(params['dest_image'])}"
        # A stale move is reconciled only when the source is absent and the
        # destination still has the approved size. No mutation is replayed.
        inner = (
            f"set -eu; dest_info=$(rbd info {destination} --format json); "
            f"if rbd info {source} --format json >/dev/null 2>&1; then "
            f"echo 'RBD move is inconclusive: source still exists' >&2; exit 42; fi; "
            "printf '%s' \"$dest_info\" | python3 -c "
            "'import json,sys; p=json.load(sys.stdin); p.update(source_deleted=True, recovered=True, checksum_verified=False); print(json.dumps(p))'"
        )
        command = "sh -c " + shlex.quote(inner)
    elif action_id == "rbd_move_cleanup_partial":
        source = f"{pool}/{shlex.quote(params['image'])}"
        destination = f"{shlex.quote(params['dest_pool'])}/{shlex.quote(params['dest_image'])}"
        inner = (
            f"set -eu; source_info=$(rbd info {source} --format json); "
            f"if rbd info {destination} --format json >/dev/null 2>&1; then "
            "echo 'RBD partial cleanup is inconclusive: destination still exists' >&2; exit 43; fi; "
            "printf '%s' \"$source_info\" | python3 -c "
            f"'import json,sys; p=json.load(sys.stdin); p.update(name=\"{params['dest_image']}\", source_present=True, destination_deleted=True, marker_verified=False, recovered=True); print(json.dumps(p))'"
        )
        command = "sh -c " + shlex.quote(inner)
    elif action_id == "rbd_template_mark":
        command = f"rbd snap ls {pool}/{shlex.quote(params['image'])} --format json"
    elif action_id == "rbd_qos_set":
        command = f"rbd config image list {pool}/{shlex.quote(params['image'])} --format json"
    elif action_id in {"rbd_create_volume", "rbd_resize_volume", "rbd_flatten_volume", "rbd_trash_restore_volume"}:
        command = f"rbd info {pool}/{shlex.quote(params['image'])} --format json"
    else:
        command = f"rbd trash ls {pool} --format json"
    if exec_mode is None:
        return command
    return commands.wrap_ceph_runtime_command(
        command, exec_mode=exec_mode, container_name=container_name
    )
