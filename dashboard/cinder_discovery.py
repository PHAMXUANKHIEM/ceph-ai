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


def _normalize_image_metadata(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        "image_id": _field(value, "image_id", "id"),
        "name": _field(value, "image_name", "name"),
        "status": _field(value, "status"),
        "size_bytes": _field(value, "image_size", "size"),
        "checksum": _field(value, "checksum"),
    }


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
        "image_metadata": _normalize_image_metadata(_field(payload, "volume_image_metadata", "image")),
        "multiattach": _as_bool(_field(payload, "multiattach")),
        "attachments": attachments,
    }


def build_cinder_mapping_row(image: str, inventory: dict, cinder: dict) -> dict:
    """Build one bounded RBD→Cinder mapping row without authorizing mutation."""
    cinder = cinder if isinstance(cinder, dict) else {}
    status = str(cinder.get("status") or "unknown")
    if status == "managed" and cinder.get("verified"):
        mapping_status = "managed"
    elif status == "not_found" and cinder.get("verified"):
        mapping_status = "orphan"
    elif status == "not_cinder":
        mapping_status = "unmanaged"
    elif status in {"not_configured", "error"}:
        mapping_status = "insufficient_evidence"
    else:
        mapping_status = "unknown"
    return {
        "image": image,
        "image_id": inventory.get("image_id"),
        "pool": inventory.get("pool"),
        "provisioned_size": inventory.get("provisioned_size"),
        "used_size": inventory.get("used_size"),
        "mapping_status": mapping_status,
        "management_source": "openstack_cinder" if mapping_status == "managed" else "none",
        "cinder": {
            "volume_id": cinder.get("volume_id"),
            "name": cinder.get("name"),
            "project_id": cinder.get("project_id"),
            "volume_status": cinder.get("volume_status"),
            "volume_type": cinder.get("volume_type"),
            "availability_zone": cinder.get("availability_zone"),
            "bootable": cinder.get("bootable"),
            "multiattach": cinder.get("multiattach"),
            "attachments": cinder.get("attachments") if isinstance(cinder.get("attachments"), list) else [],
        },
        "evidence_gap": cinder.get("error") if mapping_status == "insufficient_evidence" else None,
        "read_only": True,
        "mutation_supported": False,
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


def build_attachment_remediation(
    cinder: dict, watchers: list, locks: list, reconciliation: dict,
) -> dict:
    """Return a read-only remediation posture for stale watcher/lock evidence.

    This intentionally never recommends ``rbd lock rm`` or direct force-detach
    as an automatic action.  A watcher/lock has no reliable age in the RBD
    response, so ``stale_attachment`` means a Cinder/Ceph state mismatch, not
    proof that the client process is dead.
    """
    status = str(reconciliation.get("status") or "unknown")
    evidence = reconciliation.get("evidence") if isinstance(reconciliation.get("evidence"), dict) else {}
    if status == "healthy":
        posture, severity, recommendation = (
            "NO_REMEDIATION", "info", "Cinder và Ceph attachment evidence đang khớp.",
        )
    elif status == "stale_attachment":
        posture, severity, recommendation = (
            "REVIEW_BEFORE_DETACH", "high",
            "Xác minh instance/host và Cinder attachment trước khi detach có kiểm soát; không tự xóa lock.",
        )
    elif status == "orphan":
        posture, severity, recommendation = (
            "ORPHAN_REVIEW", "high",
            "RBD có evidence watcher/lock nhưng không còn volume Cinder; cần xác minh owner và backup trước remediation.",
        )
    elif status == "mismatch":
        posture, severity, recommendation = (
            "RECONCILE_CONTROL_PLANE", "high",
            "Dừng thao tác destructive và đối soát Cinder với client/host trước khi xử lý watcher hoặc lock.",
        )
    else:
        posture, severity, recommendation = (
            "INSUFFICIENT_EVIDENCE", "unknown",
            "Chưa đủ bằng chứng để remediation an toàn; giữ nguyên lock/watcher và thu thập lại evidence.",
        )
    return {
        "posture": posture,
        "severity": severity,
        "reconciliation_status": status,
        "recommendation": recommendation,
        "automatic_remediation": False,
        "direct_lock_removal_supported": False,
        "stale_age_available": False,
        "evidence": {
            "watcher_count": len(watchers) if isinstance(watchers, list) else 0,
            "lock_count": len(locks) if isinstance(locks, list) else 0,
            "cinder_attachment_count": len(cinder.get("attachments") or []) if isinstance(cinder, dict) else 0,
            **evidence,
        },
        "read_only": True,
    }


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


def _controller_context(cluster) -> tuple[list[str], str]:
    controllers = [item.strip() for item in (cluster.openstack_controller_nodes or "").split(",") if item.strip()]
    return controllers, (cluster.openstack_openrc_path or "").strip()


def _run_openstack_json(cluster, command: str) -> dict | list:
    controllers, openrc_path = _controller_context(cluster)
    if not controllers or not openrc_path:
        raise ValueError("OpenStack Controller/openrc chưa được cấu hình")
    wrapped = "sh -c " + shlex.quote(
        f". {shlex.quote(openrc_path)} >/dev/null 2>&1 && {command}"
    )
    ssh_user, ssh_key_path, _exec_mode, _container = resolve_ssh_creds(cluster)
    raw = _execute_controller_command(controllers[0], wrapped, user=ssh_user, key_path=ssh_key_path)
    payload = json.loads(raw)
    if not isinstance(payload, (dict, list)):
        raise ValueError("OpenStack CLI không trả về JSON object/array")
    return payload


def _normalize_server_image(value) -> dict:
    if isinstance(value, dict):
        return {
            "image_id": _field(value, "id", "image_id"),
            "name": _field(value, "name", "image_name"),
        }
    text = str(value or "").strip()
    return {"image_id": text or None, "name": None}


def normalize_cinder_server(payload: dict, expected_id: str) -> dict:
    server_id = str(_field(payload, "id") or "")
    if server_id.lower() != expected_id.lower():
        raise ValueError("Nova trả về server ID không khớp attachment")
    image = _normalize_server_image(_field(payload, "image"))
    attached = _field(payload, "volumes_attached", "volumes attached")
    attachments = []
    for row in attached if isinstance(attached, list) else []:
        if isinstance(row, dict):
            attachments.append({
                "volume_id": _field(row, "id", "volume_id"),
                "device": _field(row, "device"),
            })
    return {
        "status": "ok",
        "server_id": server_id,
        "name": _field(payload, "name"),
        "project_id": _field(payload, "project_id", "tenant_id"),
        "server_status": _field(payload, "status"),
        "image": image,
        "volumes_attached": attachments[:32],
    }


def discover_cinder_server(cluster, server_id: str) -> dict:
    """Read one Nova server for boot-source evidence; never mutates Nova."""
    if not _OPENSTACK_UUID_RE.fullmatch(str(server_id or "")):
        return {"status": "error", "verified": False, "error": "Nova server ID không hợp lệ"}
    try:
        payload = _run_openstack_json(
            cluster, f"openstack server show {shlex.quote(server_id)} -f json"
        )
        if not isinstance(payload, dict):
            raise ValueError("Nova server show không trả về JSON object")
        return normalize_cinder_server(payload, server_id)
    except (ExecutorError, json.JSONDecodeError, ValueError) as exc:
        if _is_not_found_error(str(exc)):
            return {"status": "not_found", "verified": True, "server_id": server_id}
        return {"status": "error", "verified": False, "server_id": server_id, "error": str(exc)}


def discover_glance_image(cluster, image_id: str) -> dict:
    """Read a Glance image referenced by Cinder metadata, if available."""
    if not _OPENSTACK_UUID_RE.fullmatch(str(image_id or "")):
        return {"status": "not_available", "verified": False}
    try:
        payload = _run_openstack_json(
            cluster, f"openstack image show {shlex.quote(image_id)} -f json"
        )
        if not isinstance(payload, dict):
            raise ValueError("Glance image show không trả về JSON object")
        returned_id = str(_field(payload, "id") or "")
        if returned_id.lower() != image_id.lower():
            raise ValueError("Glance trả về image ID không khớp")
        return {
            "status": "ok",
            "verified": True,
            "image_id": returned_id,
            "name": _field(payload, "name"),
            "status_value": _field(payload, "status"),
            "visibility": _field(payload, "visibility"),
            "size_bytes": _field(payload, "size"),
        }
    except (ExecutorError, json.JSONDecodeError, ValueError) as exc:
        if _is_not_found_error(str(exc)):
            return {"status": "not_found", "verified": True, "image_id": image_id}
        return {"status": "error", "verified": False, "image_id": image_id, "error": str(exc)}


def build_boot_dependency_report(
    cinder: dict,
    snapshots: dict,
    servers: list[dict],
    glance: dict | None,
) -> dict:
    """Build bounded boot-volume evidence and deletion guards."""
    cinder = cinder if isinstance(cinder, dict) else {}
    if cinder.get("status") != "managed" or not cinder.get("verified"):
        return {
            "status": "not_applicable" if cinder.get("status") == "not_cinder" else "insufficient_evidence",
            "boot_volume": {"bootable": None, "volume_id": cinder.get("volume_id")},
            "servers": [], "image_service": {"status": "not_available"}, "snapshots": [],
            "guards": {"protect_boot_volume": False, "snapshot_delete_requires_review": True},
            "evidence_gaps": ["Chưa xác minh được volume thuộc Cinder; không suy luận boot dependency."],
            "read_only": True, "mutation_supported": False,
        }
    snapshots = snapshots if isinstance(snapshots, dict) else {}
    server_rows = [row for row in servers if isinstance(row, dict)][:32]
    bootable = _as_bool(cinder.get("bootable"))
    image_metadata = cinder.get("image_metadata") if isinstance(cinder.get("image_metadata"), dict) else {}
    image_id = image_metadata.get("image_id")
    volume_id = str(cinder.get("volume_id") or "")
    boot_from_volume_servers = [
        row for row in server_rows
        if row.get("status") == "ok" and not (row.get("image") or {}).get("image_id")
        and any(str(item.get("volume_id") or "").lower() == volume_id.lower() for item in row.get("volumes_attached") or [])
    ]
    gaps = []
    if not server_rows and cinder.get("attachments"):
        gaps.append("Không đọc được Nova server cho attachment; boot source chưa được xác minh.")
    if image_id and not glance:
        gaps.append("Cinder có image metadata nhưng chưa đọc được Glance image.")
    if snapshots.get("status") != "ok":
        gaps.append("Chưa đọc được đầy đủ Cinder snapshots; không đánh dấu snapshot nào đang được dùng.")
    snapshot_rows = []
    for row in snapshots.get("items") or []:
        if not isinstance(row, dict):
            continue
        snapshot_rows.append({
            "snapshot_id": row.get("snapshot_id"),
            "name": row.get("name"),
            "status": row.get("status"),
            "created_at": row.get("created_at"),
            "delete_guard": "review_boot_dependency" if bootable or boot_from_volume_servers else "standard_dependency_check",
        })
    source = "boot_from_volume" if boot_from_volume_servers else "bootable_volume" if bootable else "not_observed"
    if not bootable and not boot_from_volume_servers and image_id:
        source = "image_metadata_only"
    return {
        "status": "observed" if not gaps else "partial",
        "boot_volume": {
            "volume_id": volume_id,
            "bootable": bootable,
            "source": source,
            "project_id": cinder.get("project_id"),
        },
        "servers": server_rows,
        "image_service": glance or {
            "status": "not_available",
            "image_id": image_id,
        },
        "snapshots": snapshot_rows[:100],
        "guards": {
            "protect_boot_volume": bool(bootable or boot_from_volume_servers),
            "snapshot_delete_requires_review": True,
            "direct_delete_supported": False,
        },
        "evidence_gaps": gaps,
        "read_only": True,
        "mutation_supported": False,
    }


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


def discover_cinder_volume_backups(cluster, limit: int | None = 100) -> dict:
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
    limit_arg = ""
    if limit is not None:
        limit = max(1, min(int(limit), 500))
        limit_arg = f" --limit {limit}"
    command = "sh -c " + shlex.quote(
        f". {shlex.quote(openrc_path)} >/dev/null 2>&1 && "
        f"backups=$(openstack volume backup list --long{limit_arg} -f json) && "
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
                "created_at": _field(row, "created_at", "created at", "created") or "—",
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
    except (ExecutorError, OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": str(exc)}
