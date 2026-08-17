"""Execution-time post-checks for approval-gated Cinder attachment actions."""

import json

from worker.executor.ssh_executor import ExecutorError


CINDER_ATTACHMENT_ACTION_IDS = frozenset({"cinder_attach_volume", "cinder_detach_volume"})


def reconcile(action_id: str, params: dict, command_output: str) -> None:
    if action_id not in CINDER_ATTACHMENT_ACTION_IDS:
        return
    try:
        payload = json.loads(command_output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecutorError("Cinder post-check không trả về JSON hợp lệ") from exc
    if not isinstance(payload, dict):
        raise ExecutorError("Cinder post-check không trả về volume object")
    lowered = {str(key).lower(): value for key, value in payload.items()}
    if str(lowered.get("id") or "").lower() != str(params.get("volume_id") or "").lower():
        raise ExecutorError("Cinder post-check trả về volume ID không khớp")
    attachments = lowered.get("attachments")
    rows = attachments if isinstance(attachments, list) else []
    server_id = str(params.get("server_id") or "").lower()
    attached = any(
        str(row.get("server_id") or row.get("instance_id") or row.get("instance") or "").lower()
        == server_id
        for row in rows if isinstance(row, dict)
    )
    expected = action_id == "cinder_attach_volume"
    if attached != expected:
        state = "có" if attached else "không có"
        raise ExecutorError(f"Cinder post-check lệch trạng thái: server {state} attachment")
