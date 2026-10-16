"""Reboot hosts one at a time under cephadm maintenance (Dashboard "Bảo trì node", 10/10/2026).

Plan/in-progress/croit-parity-operations-plan-2026-10-10.md, C2. One lifecycle
action, ``rolling_node_maintenance`` (DESTRUCTIVE: it stops every daemon of a
host and reboots it), run by cluster_deploy.run(). For each host, in the order
chosen:

1. ``ceph orch host ok-to-stop``; if the host runs the active MGR, fail it over;
2. ``ceph orch host maintenance enter`` (cephadm stops the host's daemons and
   sets noout for it);
3. reboot over SSH, wait for the host to go away and answer again;
4. ``ceph orch host maintenance exit``;
5. wait until the cluster's health checks are back to those it had before the
   run (a check present at the start may stay; nothing new may remain).

A host that does not come back, or a cluster that does not recover, stops the
run there: later hosts are never touched, and the progress says which host
is still in maintenance.
"""

from __future__ import annotations

import logging
import shlex
import time
from typing import Any, Callable

from shared.cluster_nodes import resolve_ssh_creds
from worker.executor import node_removal
from worker.executor.cluster_deploy import DeployPhaseError
from worker.executor.ssh_executor import ExecutorError, execute_command

logger = logging.getLogger(__name__)

ACTION_ID = "rolling_node_maintenance"
ACTION_IDS = frozenset({ACTION_ID})
REBOOT_GRACE_SECONDS = 30
HOST_BACK_SECONDS = 15 * 60
RECOVERY_SECONDS = 20 * 60
POLL_SECONDS = 15
MIN_MONS = 3
REBOOT_COMMAND = "nohup systemctl reboot >/dev/null 2>&1 &"

clock: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep


def _ceph(params: dict, command: str, *, as_json: bool = True) -> Any:
    """Run on a MON that is not the host under maintenance right now."""
    return node_removal._ceph({**params, "targets": [params["_current"]] if params.get("_current") else []},
                              command, as_json=as_json)


def _ssh(params: dict, host: str, command: str) -> str:
    cluster = node_removal._cluster(params)
    if cluster is None:
        return execute_command(host, command)
    user, key_path, _mode, _container = resolve_ssh_creds(cluster)
    return execute_command(host, command, user=user, key_path=key_path)


def _health_codes(params: dict) -> set[str]:
    health = _ceph(params, "ceph health detail") or {}
    return set((health.get("checks") or {}).keys())


def _status(params: dict, current: str | None, state: str, message: str) -> list[dict]:
    rows = []
    for ip in params.get("targets") or []:
        done = ip in (params.get("_done") or [])
        if ip == current:
            rows.append({"host": ip, "status": state, "message": message})
        else:
            rows.append({"host": ip, "status": "done" if done else "pending",
                         "message": "đã bảo trì xong" if done else "chờ tới lượt"})
    return rows


# --- preflight ----------------------------------------------------------------------------

def _hostnames(params: dict, targets: list[str]) -> dict[str, str]:
    """Target IP -> cephadm hostname, in the order chosen; every host may be maintained in turn."""
    by_addr = {str(host.get("addr")): str(host.get("hostname")) for host in _ceph(params, "ceph orch host ls") or []}
    missing = [ip for ip in targets if ip not in by_addr]
    if missing:
        raise DeployPhaseError(f"{', '.join(missing)} không có trong `ceph orch host ls` của cụm này.")
    return {ip: by_addr[ip] for ip in targets}


def phase_preflight(nodes: list[dict], params: dict, on_host_update) -> None:
    targets = node_removal._targets(params)
    if len(set(targets)) != len(targets):
        raise DeployPhaseError("Danh sách node bị trùng.")
    hostnames = _hostnames(params, targets)
    if str((_ceph(params, "ceph health") or {}).get("status")) == "HEALTH_ERR":
        raise DeployPhaseError("Cụm đang HEALTH_ERR — xử lý lỗi trước khi bảo trì node.")
    mons = (_ceph(params, "ceph mon dump") or {}).get("mons") or []
    on_mon = [ip for ip, name in hostnames.items() if any(str(m.get("name")).split(".")[0] == name.split(".")[0] for m in mons)]
    if on_mon and len(mons) < MIN_MONS:
        raise DeployPhaseError(f"Cụm chỉ có {len(mons)} MON: tắt một host chạy MON sẽ mất quorum.")
    params["_hostnames"] = hostnames
    params["_baseline"] = sorted(_health_codes(params))
    params["_done"] = []
    on_host_update(_status(params, None, "pending", ""))


# --- one host at a time -------------------------------------------------------------------------

def _fail_over_mgr(params: dict, hostname: str) -> None:
    mgr = _ceph(params, "ceph mgr dump") or {}
    active = str(mgr.get("active_name") or "")
    if active.split(".")[0] == hostname.split(".")[0]:
        if not mgr.get("standbys"):
            raise DeployPhaseError(f"{hostname} chạy MGR active duy nhất; thêm MGR dự phòng trước khi bảo trì.")
        _ceph(params, f"ceph mgr fail {shlex.quote(active)}", as_json=False)


def _wait_host_back(params: dict, ip: str, on_host_update) -> None:
    sleep(REBOOT_GRACE_SECONDS)
    deadline = clock() + HOST_BACK_SECONDS
    while True:
        try:
            _ssh(params, ip, "true")
            return
        except ExecutorError:
            if clock() >= deadline:
                raise DeployPhaseError(f"{ip} chưa trả lời SSH sau {HOST_BACK_SECONDS // 60} phút; host vẫn đang "
                                       "ở chế độ bảo trì — kiểm tra máy rồi chạy `ceph orch host maintenance exit`.")
            on_host_update(_status(params, ip, "running", "đang khởi động lại"))
            sleep(POLL_SECONDS)


def _wait_recovered(params: dict, ip: str, on_host_update) -> None:
    baseline = set(params.get("_baseline") or [])
    deadline = clock() + RECOVERY_SECONDS
    while True:
        extra = _health_codes(params) - baseline
        if not extra:
            return
        if clock() >= deadline:
            raise DeployPhaseError(f"Sau khi bảo trì {ip}, cụm vẫn còn {', '.join(sorted(extra))} sau "
                                   f"{RECOVERY_SECONDS // 60} phút; dừng, không bảo trì host tiếp theo.")
        on_host_update(_status(params, ip, "running", "chờ cụm hồi phục: " + ", ".join(sorted(extra))))
        sleep(POLL_SECONDS)


def phase_rolling(nodes: list[dict], params: dict, on_host_update) -> None:
    for ip, hostname in params["_hostnames"].items():
        params["_current"] = ip
        quoted = shlex.quote(hostname)
        stop = _ceph(params, f"ceph orch host ok-to-stop {quoted}", as_json=False)
        if "not ok" in str(stop).lower():
            raise DeployPhaseError(f"{hostname} chưa an toàn để dừng: {str(stop).strip()[:300]}")
        _fail_over_mgr(params, hostname)
        on_host_update(_status(params, ip, "running", "vào chế độ bảo trì"))
        _ceph(params, f"ceph orch host maintenance enter {quoted}", as_json=False)
        on_host_update(_status(params, ip, "running", "khởi động lại"))
        try:
            _ssh(params, ip, REBOOT_COMMAND)
        except ExecutorError:
            logger.info("host_maintenance: %s dropped the SSH session while rebooting", ip)
        _wait_host_back(params, ip, on_host_update)
        _ceph(params, f"ceph orch host maintenance exit {quoted}", as_json=False)
        on_host_update(_status(params, ip, "running", "đã thoát bảo trì, chờ cụm hồi phục"))
        _wait_recovered(params, ip, on_host_update)
        params["_done"] = [*params["_done"], ip]
        params["_current"] = None
        on_host_update(_status(params, None, "done", ""))


PHASES: dict[str, list[tuple[str, str, int, Callable]]] = {
    ACTION_ID: [
        ("maint_preflight", "Kiểm tra an toàn (MON quorum, health, host trong cụm)", 10, phase_preflight),
        ("maint_rolling", "Lần lượt: ok-to-stop → bảo trì → reboot → thoát bảo trì → chờ hồi phục", 100,
         phase_rolling),
    ],
}
