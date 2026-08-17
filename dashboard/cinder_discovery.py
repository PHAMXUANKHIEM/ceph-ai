"""Read-only mapping between Cinder's conventional RBD names and consumers."""

import json
import re
import shlex

from shared.cluster_nodes import resolve_ssh_creds
from worker.executor.ssh_executor import ExecutorError, execute_command


_CINDER_IMAGE_RE = re.compile(
    r"^volume-(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})$"
)


def _field(payload: dict, *names: str):
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _normalize_attachments(value) -> list[dict]:
    rows = value if isinstance(value, list) else []
    return [
        {
            "attachment_id": _field(row, "attachment_id", "id"),
            "instance_id": _field(row, "server_id", "instance", "instance_id"),
            "host": _field(row, "host_name", "host"),
            "device": _field(row, "device"),
        }
        for row in rows if isinstance(row, dict)
    ]


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def normalize_cinder_volume(payload: dict, expected_id: str) -> dict:
    volume_id = str(_field(payload, "id") or "")
    if volume_id.lower() != expected_id.lower():
        raise ValueError("Cinder trả về volume ID không khớp RBD image")
    attachments = _normalize_attachments(_field(payload, "attachments"))
    return {
        "status": "managed",
        "verified": True,
        "volume_id": volume_id,
        "name": _field(payload, "name"),
        "project_id": _field(payload, "project_id", "os-vol-tenant-attr:tenant_id"),
        "volume_status": _field(payload, "status"),
        "size_gib": _field(payload, "size"),
        "volume_type": _field(payload, "type"),
        "availability_zone": _field(payload, "availability_zone"),
        "backend_host": _field(payload, "os-vol-host-attr:host"),
        "bootable": _field(payload, "bootable"),
        "multiattach": _as_bool(_field(payload, "multiattach")),
        "attachments": attachments,
    }


def discover_cinder_volume(cluster, image: str) -> dict:
    match = _CINDER_IMAGE_RE.fullmatch(image)
    if not match:
        return {"status": "not_cinder", "verified": False}
    controllers = [item.strip() for item in cluster.openstack_controller_nodes.split(",") if item.strip()]
    openrc_path = (cluster.openstack_openrc_path or "").strip()
    if not controllers or not openrc_path:
        return {
            "status": "not_configured", "verified": False,
            "error": "Chưa cấu hình OpenStack Controller và openrc cho cluster.",
        }
    volume_id = match.group("id")
    command = (
        "sh -c " + shlex.quote(
            f". {shlex.quote(openrc_path)} >/dev/null 2>&1 && "
            f"openstack volume show {shlex.quote(volume_id)} -f json"
        )
    )
    ssh_user, ssh_key_path, _exec_mode, _container = resolve_ssh_creds(cluster)
    try:
        raw = execute_command(controllers[0], command, user=ssh_user, key_path=ssh_key_path)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Cinder CLI không trả về JSON object")
        return normalize_cinder_volume(payload, volume_id)
    except (ExecutorError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "error", "verified": False, "volume_id": volume_id, "error": str(exc)}
