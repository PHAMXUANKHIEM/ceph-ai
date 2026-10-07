"""Remove hosts from a cephadm cluster (Dashboard "Gỡ node khỏi cụm", 07/10/2026).

Two lifecycle actions, both run by cluster_deploy.run():

* ``remove_cluster_nodes`` — safety preflight, ``ceph orch host drain`` (its
  OSDs are removed after their data has moved), then waits up to
  DRAIN_WAIT_SECONDS. If the host is empty by then it is removed with
  ``ceph orch host rm``; otherwise the action ends with the drain still
  running, because data migration can take hours and the Worker runs
  approved actions inline.
* ``finish_remove_cluster_nodes`` — checks that the drain finished (no OSD
  waiting for removal, no daemon left on the host) and removes the host.

Every Ceph command runs on a MON that is not being removed, with the
cluster's own SSH identity (the default cluster's comes from settings). After
the hosts are gone their IPs leave the cluster's node lists (.env for the
default cluster, the cluster row otherwise).
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import time
from typing import Any, Callable

from shared import db, env_config
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import Cluster
from worker.executor.cluster_deploy import DeployPhaseError
from worker.executor.ssh_executor import ExecutorError, execute_command

logger = logging.getLogger(__name__)

ACTION_START = "remove_cluster_nodes"
ACTION_FINISH = "finish_remove_cluster_nodes"
ACTION_IDS = frozenset({ACTION_START, ACTION_FINISH})
DRAIN_WAIT_SECONDS = 20 * 60
POLL_SECONDS = 30
CAPACITY_MARGIN = 1.2
NEARFULL_HEADROOM = 0.05
MIN_MONS_LEFT = 3
_NODE_FIELDS = ("ceph_mon_nodes", "ceph_mgr_nodes", "ceph_osd_nodes", "ceph_rgw_nodes")

clock: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep


# --- cluster access -----------------------------------------------------------------------

def _cluster(params: dict) -> Cluster | None:
    """The non-default target cluster row, or None for the default cluster."""
    cluster_id = params.get("_cluster_id")
    if not cluster_id:
        return None
    with db.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id)
        if cluster is None:
            raise DeployPhaseError("Cụm của đề xuất không còn tồn tại.")
        session.expunge(cluster)
    return None if cluster.is_default else cluster


def config_fingerprint(cluster: Cluster | None) -> str:
    """Node lists of the cluster as configured now (proposal vs. execution)."""
    if cluster is None:
        return env_config.current_cluster_config_fingerprint()
    canonical = {field: getattr(cluster, field) or "" for field in _NODE_FIELDS}
    canonical["exec_mode"] = cluster.ceph_exec_mode or ""
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def _targets(params: dict) -> list[str]:
    targets = [str(ip) for ip in params.get("targets") or []]
    if not targets:
        raise DeployPhaseError("Chưa chọn node nào để gỡ.")
    return targets


def _ceph(params: dict, command: str, *, as_json: bool = True) -> Any:
    from worker.executor.commands import wrap_ceph_runtime_command

    cluster = _cluster(params)
    targets = set(params.get("targets") or [])
    mons = [node["host"] for node in configured_nodes(cluster) if "MON" in node["roles"] and node["host"] not in targets]
    if not mons:
        raise DeployPhaseError("Không còn MON nào ngoài các node sắp gỡ để chạy lệnh Ceph.")
    user, key_path, exec_mode, container = resolve_ssh_creds(cluster)
    if exec_mode != "cephadm":
        raise DeployPhaseError(f"Gỡ node chỉ hỗ trợ cụm cephadm (cụm này: {exec_mode}).")
    full = wrap_ceph_runtime_command(command + (" --format json" if as_json else ""), exec_mode=exec_mode,
                                     container_name=container)
    try:
        if cluster is None:
            output = execute_command(mons[0], full)
        else:
            output = execute_command(mons[0], full, user=user, key_path=key_path)
    except ExecutorError as exc:
        raise DeployPhaseError(f"`{command}` thất bại: {exc}") from exc
    if not as_json:
        return output
    try:
        return json.loads(output or "null")
    except ValueError as exc:
        raise DeployPhaseError(f"`{command}` trả về dữ liệu không đọc được") from exc


def _hostnames(params: dict) -> dict[str, str]:
    """Target IP -> cephadm hostname (``ceph orch host ls``)."""
    hosts = _ceph(params, "ceph orch host ls") or []
    by_addr = {str(host.get("addr")): str(host.get("hostname")) for host in hosts}
    missing = [ip for ip in _targets(params) if ip not in by_addr]
    if missing:
        raise DeployPhaseError(f"{', '.join(missing)} không có trong `ceph orch host ls` của cụm này.")
    if len(hosts) - len(params["targets"]) < 1:
        raise DeployPhaseError("Không thể gỡ mọi host của cụm — dùng Delete Cluster.")
    return {ip: by_addr[ip] for ip in params["targets"]}


def _status(targets: dict[str, str], state: str, messages: dict[str, str] | None = None) -> list[dict]:
    return [{"host": ip, "status": state, "message": (messages or {}).get(ip, hostname)}
            for ip, hostname in targets.items()]


# --- preflight ------------------------------------------------------------------------------

def _check_fingerprint(params: dict) -> None:
    expected = params.get("_node_config_fingerprint")
    if expected and expected != config_fingerprint(_cluster(params)):
        raise DeployPhaseError("Cấu hình node của cụm đã thay đổi sau khi đề xuất; tạo đề xuất mới.")


def _check_mons_and_mgrs(params: dict, hostnames: set[str]) -> None:
    mons = (_ceph(params, "ceph mon dump") or {}).get("mons") or []
    removed = [mon["name"] for mon in mons if str(mon.get("name")).split(".")[0] in hostnames]
    if removed and len(mons) - len(removed) < MIN_MONS_LEFT:
        raise DeployPhaseError(
            f"Node sắp gỡ đang chạy MON ({', '.join(removed)}); sau khi gỡ chỉ còn {len(mons) - len(removed)} MON "
            f"(< {MIN_MONS_LEFT}). Chuyển MON sang node khác trước.")
    mgr = _ceph(params, "ceph mgr dump") or {}
    names = [mgr.get("active_name")] + [item.get("name") for item in mgr.get("standbys") or []]
    left = [name for name in names if name and str(name).split(".")[0] not in hostnames]
    if not left:
        raise DeployPhaseError("Gỡ các node này sẽ không còn MGR nào.")


def _osd_layout(params: dict, hostnames: set[str]) -> tuple[list[int], int]:
    """(OSD ids on the target hosts, number of other hosts that hold OSDs)."""
    tree = (_ceph(params, "ceph osd tree") or {}).get("nodes") or []
    hosts = [node for node in tree if node.get("type") == "host"]
    target_osds = sorted(child for node in hosts if node.get("name") in hostnames for child in node.get("children") or [])
    other_hosts = sum(1 for node in hosts if node.get("name") not in hostnames and node.get("children"))
    return target_osds, other_hosts


def _check_redundancy_and_capacity(params: dict, target_osds: list[int], other_hosts: int) -> None:
    pools = _ceph(params, "ceph osd pool ls detail") or []
    needed = max((int(pool.get("size") or 0) for pool in pools), default=0)
    if target_osds and other_hosts < needed:
        raise DeployPhaseError(
            f"Sau khi gỡ chỉ còn {other_hosts} host có OSD, ít hơn số bản sao lớn nhất của pool ({needed}).")
    if not target_osds:
        return
    nodes = (_ceph(params, "ceph osd df") or {}).get("nodes") or []
    moving = sum(int(node.get("kb_used") or 0) for node in nodes if node.get("id") in target_osds)
    rest = [node for node in nodes if node.get("id") not in target_osds and float(node.get("reweight") or 0) > 0]
    free = sum(int(node.get("kb_avail") or 0) for node in rest)
    total = sum(int(node.get("kb") or 0) for node in rest)
    used_after = (sum(int(node.get("kb_used") or 0) for node in rest) + moving) / total if total else 1.0
    nearfull = float((_ceph(params, "ceph osd dump") or {}).get("nearfull_ratio") or 0.85)
    if moving * CAPACITY_MARGIN > free or used_after > nearfull - NEARFULL_HEADROOM:
        raise DeployPhaseError(
            f"Không đủ chỗ: cần dời {moving / 1048576:.1f} GiB, các OSD còn lại trống {free / 1048576:.1f} GiB, "
            f"sau khi dời sẽ dùng {used_after:.0%} (ngưỡng nearfull {nearfull:.0%}).")


def phase_preflight(nodes: list[dict], params: dict, on_host_update) -> None:
    _check_fingerprint(params)
    targets = _hostnames(params)
    hostnames = set(targets.values())
    on_host_update(_status(targets, "running"))
    if str((_ceph(params, "ceph health") or {}).get("status")) == "HEALTH_ERR":
        raise DeployPhaseError("Cụm đang HEALTH_ERR — xử lý lỗi trước khi gỡ node.")
    _check_mons_and_mgrs(params, hostnames)
    target_osds, other_hosts = _osd_layout(params, hostnames)
    _check_redundancy_and_capacity(params, target_osds, other_hosts)
    params["_hostnames"] = targets
    params["_osd_ids"] = target_osds
    on_host_update(_status(targets, "done", {ip: f"{name}: an toàn để gỡ, {len(target_osds)} OSD sẽ được dời"
                                              for ip, name in targets.items()}))


# --- drain and removal -----------------------------------------------------------------------

def phase_drain(nodes: list[dict], params: dict, on_host_update) -> None:
    targets = params["_hostnames"]
    zap = " --zap-osd-devices" if params.get("zap_devices") else ""
    for hostname in targets.values():
        _ceph(params, f"ceph orch host drain {shlex.quote(hostname)}{zap}", as_json=False)
    on_host_update(_status(targets, "done", {ip: f"{name}: đã bắt đầu drain" for ip, name in targets.items()}))


def _remaining(params: dict) -> tuple[int, dict[str, int]]:
    """(OSDs still waiting for removal, daemons left on each target host)."""
    try:
        pending = _ceph(params, "ceph orch osd rm status") or []
    except DeployPhaseError:
        pending = []  # older releases print a sentence instead of JSON when the queue is empty
    pending_ids = {int(item.get("osd_id", -1)) for item in pending if isinstance(item, dict)}
    left = len(pending_ids & set(params.get("_osd_ids") or []))
    daemons = {ip: len(_ceph(params, f"ceph orch ps {shlex.quote(name)}") or [])
               for ip, name in params["_hostnames"].items()}
    return left, daemons


def _drain_messages(params: dict, left: int, daemons: dict[str, int]) -> dict[str, str]:
    return {ip: f"{name}: còn {daemons[ip]} daemon; {left} OSD đang chờ dời dữ liệu"
            for ip, name in params["_hostnames"].items()}


def phase_wait(nodes: list[dict], params: dict, on_host_update) -> None:
    deadline = clock() + DRAIN_WAIT_SECONDS
    while True:
        left, daemons = _remaining(params)
        messages = _drain_messages(params, left, daemons)
        if not left and not any(daemons.values()):
            on_host_update(_status(params["_hostnames"], "done", {ip: "đã dời xong" for ip in daemons}))
            return
        if clock() >= deadline:
            params["_drain_pending"] = True
            on_host_update(_status(params["_hostnames"], "done", {
                ip: message + " — vẫn đang dời; bấm \"Hoàn tất gỡ node\" khi xong" for ip, message in messages.items()}))
            return
        on_host_update(_status(params["_hostnames"], "running", messages))
        sleep(POLL_SECONDS)


def phase_check_drained(nodes: list[dict], params: dict, on_host_update) -> None:
    _check_fingerprint(params)
    params["_hostnames"] = _hostnames(params)
    params["_osd_ids"] = _osd_layout(params, set(params["_hostnames"].values()))[0] or params.get("_osd_ids") or []
    left, daemons = _remaining(params)
    if left or any(daemons.values()):
        on_host_update(_status(params["_hostnames"], "failed", _drain_messages(params, left, daemons)))
        raise DeployPhaseError("Drain chưa xong — dữ liệu vẫn đang được dời; thử lại sau.")
    on_host_update(_status(params["_hostnames"], "done", {ip: "đã dời xong" for ip in daemons}))


def phase_remove_hosts(nodes: list[dict], params: dict, on_host_update) -> None:
    targets = params["_hostnames"]
    if params.get("_drain_pending"):
        on_host_update(_status(targets, "done", {ip: "chưa gỡ: chờ drain xong" for ip in targets}))
        return
    for hostname in targets.values():
        _ceph(params, f"ceph orch host rm {shlex.quote(hostname)}", as_json=False)
    still = {str(host.get("hostname")) for host in _ceph(params, "ceph orch host ls") or []} & set(targets.values())
    if still:
        raise DeployPhaseError(f"{', '.join(sorted(still))} vẫn còn trong cụm sau `ceph orch host rm`.")
    params["_removed_ips"] = list(targets)
    on_host_update(_status(targets, "done", {ip: f"{name}: đã gỡ khỏi cụm" for ip, name in targets.items()}))


PHASES: dict[str, list[tuple[str, str, int, Callable]]] = {
    ACTION_START: [
        ("rm_preflight", "Kiểm tra an toàn (MON/MGR, số bản sao, dung lượng)", 15, phase_preflight),
        ("rm_drain", "Drain host (dời daemon và dữ liệu OSD)", 35, phase_drain),
        ("rm_wait", "Chờ dời dữ liệu (tối đa 20 phút)", 80, phase_wait),
        ("rm_host", "Gỡ host khỏi cụm (ceph orch host rm)", 95, phase_remove_hosts),
    ],
    ACTION_FINISH: [
        ("rm_check", "Kiểm tra drain đã xong", 40, phase_check_drained),
        ("rm_host", "Gỡ host khỏi cụm (ceph orch host rm)", 95, phase_remove_hosts),
    ],
}


# --- config -------------------------------------------------------------------------------

def _without(raw: str | None, removed: set[str]) -> str:
    return ",".join(item.strip() for item in str(raw or "").split(",") if item.strip() and item.strip() not in removed)


def apply_config(params: dict) -> None:
    """Drop removed IPs from the cluster's node lists; nothing if the drain is still running."""
    removed = set(params.get("_removed_ips") or [])
    if not removed:
        return
    cluster = _cluster(params)
    if cluster is None:
        from shared.clusters import sync_default_cluster_from_env

        names = [env_config.CLUSTER_ENV_NAMES[field] for field in _NODE_FIELDS]
        current = env_config.read_env_values(names)
        env_config.update_env_file_batch({name: _without(current.get(name), removed) for name in names})
        with db.SessionLocal() as session:
            sync_default_cluster_from_env(session)
        return
    with db.SessionLocal() as session:
        row = session.get(Cluster, cluster.id)
        for field in _NODE_FIELDS:
            setattr(row, field, _without(getattr(row, field), removed))
        session.commit()
