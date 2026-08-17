"""Execution-time post-checks for approval-gated Cinder storage actions."""

import json

from worker.executor.ssh_executor import ExecutorError


CINDER_ATTACHMENT_ACTION_IDS = frozenset({"cinder_attach_volume", "cinder_detach_volume"})


def reconcile(action_id: str, params: dict, command_output: str) -> None:
    if action_id not in CINDER_ATTACHMENT_ACTION_IDS and action_id != "cinder_create_snapshot":
        return
    try:
        payload = json.loads(command_output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecutorError("Cinder post-check không trả về JSON hợp lệ") from exc
    if action_id == "cinder_create_snapshot":
        if not isinstance(payload, list):
            raise ExecutorError("Cinder snapshot post-check không trả về danh sách")
        expected_name = str(params.get("snapshot_name") or "")
        match = next((
            row for row in payload
            if isinstance(row, dict)
            and str(row.get("name") or row.get("Name") or "") == expected_name
        ), None)
        if match is None:
            raise ExecutorError("Cinder snapshot post-check không tìm thấy snapshot vừa tạo")
        status = str(match.get("status") or match.get("Status") or "").lower()
        if status in {"error", "error_deleting"}:
            raise ExecutorError(f"Cinder snapshot post-check có trạng thái lỗi: {status}")
        return
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
