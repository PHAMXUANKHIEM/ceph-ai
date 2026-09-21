"""Bounded, read-only host path/session discovery for Block Storage."""

from __future__ import annotations

import json
import re

from dashboard.cinder_discovery import _execute_controller_command
from shared.cluster_nodes import resolve_ssh_creds
from worker.executor.ssh_executor import ExecutorError


MAX_COMPUTE_NODES = 16
COMMAND_TIMEOUT_NOTE = "Lệnh discovery chỉ đọc; reconnect/failover không được hỗ trợ từ endpoint."


def _configured_compute_nodes(cluster) -> list[str]:
    raw = str(getattr(cluster, "openstack_compute_nodes", "") or "")
    return list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))[:MAX_COMPUTE_NODES]


def _run_read_only(cluster, host: str, command: str) -> str:
    ssh_user, ssh_key_path, _exec_mode, _container = resolve_ssh_creds(cluster)
    return _execute_controller_command(host, command, user=ssh_user, key_path=ssh_key_path)


def _parse_multipath(text: str) -> dict:
    devices = []
    current = None
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        header = re.match(r"^(\S+) \(([^)]+)\) dm-\S+", stripped)
        if header:
            current = {"name": header.group(1), "wwid": header.group(2), "paths": [], "status": "unknown"}
            devices.append(current)
            continue
        path = re.search(r"\b(\d+:\d+:\d+:\d+)\s+(\S+)\s+\S+\s+(active|failed|faulty)\s+(ready|shaky|running|offline)", stripped, re.I)
        if current is not None and path:
            current["paths"].append({"address": path.group(1), "device": path.group(2), "path_state": path.group(3).lower(), "io_state": path.group(4).lower()})
    for device in devices:
        healthy = [path for path in device["paths"] if path["path_state"] == "active" and path["io_state"] in {"ready", "running"}]
        device["active_paths"] = len(healthy)
        device["status"] = "healthy" if healthy else "degraded" if device["paths"] else "unknown"
    return {"status": "observed" if devices else "empty", "devices": devices[:64]}


def _parse_nvme(text: str) -> dict:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return {"status": "unsupported", "subsystems": [], "reason": "nvme list-subsys không trả JSON hợp lệ."}
    raw = payload.get("Subsystems") if isinstance(payload, dict) else []
    rows = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        paths = []
        for path in item.get("Paths") or []:
            if isinstance(path, dict):
                paths.append({
                    "name": path.get("Name"),
                    "transport": path.get("Transport"),
                    "state": path.get("State"),
                    "address": path.get("Address"),
                })
        rows.append({"name": item.get("Name"), "nqn": item.get("NQN"), "paths": paths[:32]})
    return {"status": "observed" if rows else "empty", "subsystems": rows[:64]}


def _parse_iscsi(text: str) -> dict:
    sessions = []
    for line in str(text or "").splitlines():
        match = re.search(r"^tcp:\s*\[(\d+)\]\s+([^,\s]+),\d+\s+([^\s]+)", line.strip())
        if match:
            sessions.append({"sid": match.group(1), "endpoint": match.group(2), "target": match.group(3), "state": "logged_in"})
    return {"status": "observed" if sessions else "empty", "sessions": sessions[:64]}


def collect_host_path_evidence(cluster, host: str) -> dict:
    """Collect normalized transport evidence from one configured compute host."""
    commands = {
        "multipath": "multipath -ll -v2 2>/dev/null",
        "nvme": "nvme list-subsys -o json 2>/dev/null",
        "iscsi": "iscsiadm -m session -P 1 2>/dev/null",
    }
    raw = {}
    errors = []
    for name, command in commands.items():
        try:
            raw[name] = _run_read_only(cluster, host, command)
        except (ExecutorError, OSError, ValueError) as exc:
            errors.append(f"{name}:{type(exc).__name__}")
    return {
        "host": host,
        "multipath": _parse_multipath(raw.get("multipath", "")),
        "nvmeof": _parse_nvme(raw.get("nvme", "")),
        "iscsi": _parse_iscsi(raw.get("iscsi", "")),
        "errors": sorted(set(errors)),
        "read_only": True,
        "mutation_supported": False,
    }


def build_path_report(cluster, image: str, cinder: dict, hosts: list[dict]) -> dict:
    """Return a safe report; no path-to-volume claim is made without evidence."""
    cinder = cinder if isinstance(cinder, dict) else {}
    evidence = [row for row in hosts if isinstance(row, dict)][:MAX_COMPUTE_NODES]
    gaps = []
    if not evidence:
        gaps.append("Chưa cấu hình hoặc chưa đọc được OpenStack compute/gateway node.")
    if cinder.get("status") != "managed" or not cinder.get("verified"):
        gaps.append("Chưa xác minh volume thuộc Cinder; không map path/session vào volume.")
    if evidence and not any(not row.get("errors") for row in evidence):
        gaps.append("Tất cả host path probe đều lỗi; không kết luận health.")
    return {
        "status": "unsupported" if not evidence else "observed" if not gaps else "partial",
        "cluster_id": cluster.id,
        "image": image,
        "volume_id": cinder.get("volume_id"),
        "hosts": evidence,
        "evidence_gaps": gaps,
        "next_action": "Chỉ thực hiện reconnect/failover qua runbook và approval riêng; không có action từ API này.",
        "read_only": True,
        "mutation_supported": False,
    }


def configured_compute_nodes(cluster) -> list[str]:
    return _configured_compute_nodes(cluster)
