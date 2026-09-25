"""Progress-aware executor for approval-gated RBD moves.

The mutation command itself remains server-built and fail-closed in
``commands.py``.  This wrapper makes the durable Action progress useful to the
Dashboard and preserves the command output for the normal RBD post-check.
"""

from __future__ import annotations

from typing import Callable

from worker.executor import commands, rbd_reconciliation
from worker.executor.ssh_executor import ExecutorError, execute_command


RBD_MOVE_ACTION_ID = "rbd_move_volume"


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
    """Run one move with durable phase transitions and exact post-checks.

    The copy/verify/delete shell command is intentionally one atomic approval
    unit: it never deletes the source before its own checksum and size checks
    pass.  If SSH/Worker dies, the stale-action reconciler remains read-only;
    the persisted phase tells the operator whether the interruption happened
    before or during the mutation.
    """
    progress = [
        _step("preflight", "Kiểm tra lại intent và command schema"),
        _step("copy_verify_delete", "Copy, kiểm tra checksum rồi xóa nguồn"),
        _step("post_check", "Xác nhận destination và source state"),
    ]
    progress[0].update(status="running")
    write_progress(action_pk, progress)
    try:
        command = commands.get_command(
            RBD_MOVE_ACTION_ID,
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
    progress[2].update(status="running")
    write_progress(action_pk, progress)
    try:
        rbd_reconciliation.reconcile(RBD_MOVE_ACTION_ID, params, output)
    except ExecutorError as exc:
        progress[2].update(status="failed", error=str(exc))
        write_progress(action_pk, progress)
        return False, command, output
    progress[2].update(status="done", output=output[-4000:])
    write_progress(action_pk, progress)
    return True, command, output
