"""Approval-gated cleanup for an interrupted cross-pool RBD move."""

from __future__ import annotations

from typing import Callable

from worker.executor import commands, rbd_reconciliation
from worker.executor.ssh_executor import ExecutorError, execute_command


RBD_MOVE_CLEANUP_ACTION_ID = "rbd_move_cleanup_partial"


def _step(key: str, label: str) -> dict:
    return {"phase": key, "label": label, "status": "pending"}


def run(
    action_pk: str,
    params: dict,
    host: str,
    ssh_user: str | None,
    ssh_key_path: str | None,
    exec_mode: str | None,
    container_name: str,
    write_progress: Callable[[str, list[dict]], None],
) -> tuple[bool, str, str | None]:
    """Delete only a token-owned incomplete destination while source remains.

    The command is intentionally separate from the move command.  Cleanup is
    never inferred from a timeout: an operator must explicitly propose and
    approve it after the failed move has been inspected.
    """
    progress = [
        _step("preflight", "Kiểm tra source còn tồn tại và giữ nguyên kích thước"),
        _step("verify_ownership", "Xác minh destination thuộc đúng move token"),
        _step("delete_destination", "Xóa bản copy dở dang, không xóa source"),
        _step("post_check", "Xác nhận source còn nguyên và destination đã biến mất"),
    ]
    progress[0].update(status="running")
    write_progress(action_pk, progress)
    try:
        command = commands.get_command(
            RBD_MOVE_CLEANUP_ACTION_ID,
            host,
            params,
            exec_mode=exec_mode,
            container_name=container_name,
        )
    except ExecutorError as exc:
        progress[0].update(status="failed", error=str(exc))
        write_progress(action_pk, progress)
        return False, "", None
    progress[0].update(status="done")
    progress[1].update(status="running", command=command)
    write_progress(action_pk, progress)
    try:
        output = execute_command(host, command, user=ssh_user, key_path=ssh_key_path)
    except ExecutorError as exc:
        progress[1].update(status="failed", error=str(exc))
        write_progress(action_pk, progress)
        return False, command, None
    progress[1].update(status="done")
    progress[2].update(status="done")
    progress[3].update(status="running")
    write_progress(action_pk, progress)
    try:
        rbd_reconciliation.reconcile(RBD_MOVE_CLEANUP_ACTION_ID, params, output)
    except ExecutorError as exc:
        progress[3].update(status="failed", error=str(exc))
        write_progress(action_pk, progress)
        return False, command, output
    progress[3].update(status="done", output=output[-4000:])
    write_progress(action_pk, progress)
    return True, command, output
