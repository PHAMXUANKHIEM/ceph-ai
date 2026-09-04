"""Read-only mapping between Cinder's conventional RBD names and consumers."""

import json
import re
import shlex
import socket
import subprocess

from shared.cluster_nodes import resolve_ssh_creds
from worker.executor.ssh_executor import ExecutorError, execute_command


_CINDER_IMAGE_RE = re.compile(
    r"^volume-(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})$"
)
_OPENSTACK_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


def _is_local_host(host: str) -> bool:
    """Avoid an unnecessary SSH loopback when Cinder is on this host."""
    candidates = {"localhost", "127.0.0.1", "::1", socket.gethostname(), socket.getfqdn()}
    try:
        candidates.update(
            info[4][0]
            for info in socket.getaddrinfo(socket.gethostname(), None)
            if info[4]
        )
        local_ips = subprocess.run(
            ["hostname", "-I"], capture_output=True, timeout=3, check=False, text=True
        ).stdout.split()
        candidates.update(local_ips)
    except OSError:
        pass
    return host.strip().casefold() in {item.casefold() for item in candidates if item}


def _execute_controller_command(host: str, command: str, user: str, key_path: str) -> str:
    if not _is_local_host(host):
        return execute_command(host, command, user=user, key_path=key_path)
    completed = subprocess.run(
        ["sh", "-c", command], capture_output=True, timeout=1800, check=False
    )
    if completed.returncode:
        error = completed.stderr.decode(errors="replace")
        raise ExecutorError(f"{host}: command exited {completed.returncode}: {error}")
    return completed.stdout.decode()


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


def reconcile_cinder_attachment(cinder: dict, watchers: list, locks: list) -> dict:
    """Compare stable Cinder state with Ceph evidence without mutating either side."""
    cinder_status = cinder.get("status")
    watcher_count = len(watchers) if isinstance(watchers, list) else 0
    lock_count = len(locks) if isinstance(locks, list) else 0
    observed = watcher_count > 0 or lock_count > 0
    evidence = {
        "cinder_attachment_count": 0,
        "ceph_watcher_count": watcher_count,
        "ceph_lock_count": lock_count,
    }
    if cinder_status == "not_cinder":
        return {"status": "not_applicable", "safe": False, "evidence": evidence}
    if cinder_status == "not_found":
        return {
            "status": "orphan", "safe": False,
            "reason": "RBD image theo chuẩn Cinder nhưng volume không còn trong Cinder.",
            "evidence": evidence,
        }
    if cinder_status != "managed" or not cinder.get("verified"):
        return {
            "status": "unknown", "safe": False,
            "reason": cinder.get("error") or "Không xác minh được trạng thái Cinder.",
            "evidence": evidence,
        }

    attachments = cinder.get("attachments") if isinstance(cinder.get("attachments"), list) else []
    attachment_count = len(attachments)
    evidence["cinder_attachment_count"] = attachment_count
    volume_status = str(cinder.get("volume_status") or "").lower()
    evidence["cinder_volume_status"] = volume_status

    if attachment_count > 1 and not cinder.get("multiattach"):
        return {
            "status": "mismatch", "safe": False,
            "reason": "Cinder có nhiều attachment nhưng volume không bật multiattach.",
            "evidence": evidence,
        }
    if volume_status not in {"available", "in-use"}:
        return {
            "status": "unknown", "safe": False,
            "reason": f"Cinder volume đang ở trạng thái chuyển tiếp hoặc lỗi: {volume_status or 'unknown'}.",
            "evidence": evidence,
        }
    if attachment_count and not observed:
        return {
            "status": "mismatch", "safe": False,
            "reason": "Cinder báo attached nhưng Ceph không có watcher/lock.",
            "evidence": evidence,
        }
    if not attachment_count and observed:
        return {
            "status": "stale_attachment", "safe": False,
            "reason": "Cinder không có attachment nhưng Ceph vẫn còn watcher/lock.",
            "evidence": evidence,
        }
    if (volume_status == "available") != (attachment_count == 0):
        return {
            "status": "mismatch", "safe": False,
            "reason": "Cinder status không khớp danh sách attachment.",
            "evidence": evidence,
        }
    return {"status": "healthy", "safe": True, "evidence": evidence}


def _is_not_found_error(message: str) -> bool:
    lowered = message.lower()
    return "no volume with a name or id" in lowered or "could not find resource" in lowered


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
        raw = _execute_controller_command(controllers[0], command, user=ssh_user, key_path=ssh_key_path)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Cinder CLI không trả về JSON object")
        return normalize_cinder_volume(payload, volume_id)
    except (ExecutorError, json.JSONDecodeError, ValueError) as exc:
        if _is_not_found_error(str(exc)):
            return {"status": "not_found", "verified": True, "volume_id": volume_id}
        return {"status": "error", "verified": False, "volume_id": volume_id, "error": str(exc)}


def discover_cinder_snapshots(cluster, volume_id: str) -> dict:
    """List snapshots through Cinder; never inspect Cinder-owned RBD snapshots directly."""
    if not _OPENSTACK_UUID_RE.fullmatch(volume_id):
        return {"status": "error", "items": [], "error": "Cinder volume ID không hợp lệ."}
    controllers = [item.strip() for item in cluster.openstack_controller_nodes.split(",") if item.strip()]
    openrc_path = (cluster.openstack_openrc_path or "").strip()
    if not controllers or not openrc_path:
        return {
            "status": "not_configured", "items": [],
            "error": "Chưa cấu hình OpenStack Controller và openrc cho cluster.",
        }
    command = "sh -c " + shlex.quote(
        f". {shlex.quote(openrc_path)} >/dev/null 2>&1 && "
        f"openstack volume snapshot list --volume {shlex.quote(volume_id)} -f json"
    )
    ssh_user, ssh_key_path, _exec_mode, _container = resolve_ssh_creds(cluster)
    try:
        payload = json.loads(
            _execute_controller_command(controllers[0], command, user=ssh_user, key_path=ssh_key_path)
        )
        if not isinstance(payload, list):
            raise ValueError("Cinder snapshot CLI không trả về JSON array")
        items = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            items.append({
                "snapshot_id": _field(row, "id"),
                "name": _field(row, "name"),
                "status": _field(row, "status"),
                "size_gib": _field(row, "size"),
                "created_at": _field(row, "created_at", "created at"),
            })
        return {"status": "ok", "items": items, "count": len(items)}
    except (ExecutorError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "error", "items": [], "error": str(exc)}


def _cinder_backup_source(volume: dict) -> tuple[str, str]:
    """Classify a Cinder volume for display in the Ceph/Vitastor UIs.

    Cinder's backup API only returns the source volume ID.  The volume list
    gives us the volume type, which is the stable user-facing discriminator
    already used by this lab (``vitastor-*`` and ``ceph*``).  Unknown types are
    kept visible instead of being silently dropped.
    """
    volume_type = str(_field(volume, "type", "volume_type") or "").strip()
    lowered = volume_type.casefold()
    if "vitastor" in lowered:
        return "vitastor", "Vitastor"
    if "ceph" in lowered or "rbd" in lowered:
        return "ceph", "Ceph"
    return "unknown", "Không xác định"


def discover_cinder_volume_backups(cluster, limit: int = 100) -> dict:
    """Read Cinder volume backups and enrich them with source volume info.

    This is deliberately read-only.  The command runs on the configured
    OpenStack controller using its openrc, so the dashboard can display the
    same objects as ``openstack volume backup list`` without importing
    OpenStack SDK dependencies into the dashboard process.
    """
    controllers = [item.strip() for item in (cluster.openstack_controller_nodes or "").split(",") if item.strip()]
    openrc_path = (cluster.openstack_openrc_path or "").strip()
    if not controllers or not openrc_path:
        return {
            "status": "not_configured", "items": [],
            "error": "Chưa cấu hình OpenStack Controller và openrc cho cluster.",
        }
    limit = max(1, min(int(limit), 500))
    command = "sh -c " + shlex.quote(
        f". {shlex.quote(openrc_path)} >/dev/null 2>&1 && "
        f"backups=$(openstack volume backup list --long --limit {limit} -f json) && "
        f"volumes=$(openstack volume list --all-projects --long -f json) && "
        "printf '%s\\n' \"{\\\"backups\\\":$backups,\\\"volumes\\\":$volumes}\""
    )
    ssh_user, ssh_key_path, _exec_mode, _container = resolve_ssh_creds(cluster)
    try:
        raw = _execute_controller_command(controllers[0], command, user=ssh_user, key_path=ssh_key_path)
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Cinder CLI không trả về JSON object")
        backups = payload.get("backups")
        volumes = payload.get("volumes")
        if not isinstance(backups, list) or not isinstance(volumes, list):
            raise ValueError("Cinder CLI trả về payload backup/volume không hợp lệ")
        volume_by_id = {
            str(_field(row, "id") or "").lower(): row
            for row in volumes if isinstance(row, dict) and _field(row, "id")
        }
        items = []
        for row in backups:
            if not isinstance(row, dict):
                continue
            volume_id = str(_field(row, "volume") or "").strip()
            volume = volume_by_id.get(volume_id.lower(), {})
            source, source_label = _cinder_backup_source(volume)
            items.append({
                "id": _field(row, "id"),
                "name": _field(row, "name") or "—",
                "status": _field(row, "status") or "unknown",
                "size_gib": _field(row, "size") or 0,
                "volume_id": volume_id or "—",
                "volume_name": _field(volume, "name") or "—",
                "volume_type": _field(volume, "type", "volume_type") or "—",
                "source": source,
                "source_label": source_label,
                "container": _field(row, "container") or "—",
            })
        return {"status": "ok", "items": items, "count": len(items)}
    except (ExecutorError, json.JSONDecodeError, ValueError) as exc:
        return {"status": "error", "items": [], "error": str(exc)}


def delete_cinder_volume_backup(cluster, backup_id: str, confirmation: str) -> dict:
    """Delete one Cinder volume backup after an explicit ``OK`` confirmation."""
    if str(confirmation or "").strip() != "OK":
        return {"status": "error", "error": "Phải nhập chính xác OK để xóa backup."}
    if not _OPENSTACK_UUID_RE.fullmatch(str(backup_id or "")):
        return {"status": "error", "error": "Backup ID không hợp lệ."}

    controllers = [item.strip() for item in (cluster.openstack_controller_nodes or "").split(",") if item.strip()]
    openrc_path = (cluster.openstack_openrc_path or "").strip()
    if not controllers or not openrc_path:
        return {
            "status": "error",
            "error": "Chưa cấu hình OpenStack Controller và openrc cho cluster.",
        }
    command = "sh -c " + shlex.quote(
        f". {shlex.quote(openrc_path)} >/dev/null 2>&1 && "
        f"openstack volume backup delete {shlex.quote(str(backup_id))}"
    )
    ssh_user, ssh_key_path, _exec_mode, _container = resolve_ssh_creds(cluster)
    try:
        _execute_controller_command(controllers[0], command, user=ssh_user, key_path=ssh_key_path)
        return {"status": "ok", "backup_id": str(backup_id)}
    except ExecutorError as exc:
        return {"status": "error", "error": str(exc)}
