"""Add hosts (and their OSDs) to a running cephadm cluster (Dashboard "Thêm node", 10/10/2026).

Plan/in-progress/croit-parity-operations-plan-2026-10-10.md, C3. Before this,
hosts and OSDs could only be added while deploying a new cluster. The action
``add_cluster_nodes`` (RISKY) reuses the deploy phases on the new hosts, with
an existing MON of the cluster in front of the node list so every
``ceph orch`` command runs where cephadm and its SSH key already are:

1. SSH, OS and empty-disk checks on the new hosts; none may be in the cluster;
2. dependencies (chrony, container runtime);
3. cephadm's SSH key on each new host, then ``ceph orch host add``;
4. ``ceph orch daemon add osd`` on the chosen disks;
5. wait until the new OSDs are up and in.

The new IPs then join the cluster's OSD node list in Ceph AI's configuration.
"""

from __future__ import annotations

import json
import logging
import shlex
import time
from typing import Any, Callable

from shared import db, env_config
from shared.models import Cluster
from worker.executor import cluster_deploy
from worker.executor.cluster_deploy import DeployPhaseError
from worker.executor.ssh_executor import ExecutorError, execute_command

logger = logging.getLogger(__name__)

ACTION_ID = "add_cluster_nodes"
ACTION_IDS = frozenset({ACTION_ID})
OSD_UP_SECONDS = 15 * 60
POLL_SECONDS = 30

clock: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep


def _new_nodes(params: dict) -> list[dict]:
    nodes = [node for node in params.get("nodes") or [] if isinstance(node, dict) and node.get("ip")]
    if not nodes:
        raise DeployPhaseError("Chưa có node mới nào.")
    return nodes


def _admin_mon(params: dict) -> dict:
    mon = str(params.get("_admin_mon") or "")
    if not mon:
        raise DeployPhaseError("Không có MON hiện có của cụm để chạy lệnh cephadm.")
    return {"ip": mon, "roles": ["mon"]}


def _ceph(params: dict, command: str) -> Any:
    output = execute_command(_admin_mon(params)["ip"], f"cephadm shell -- {command} --format json")
    try:
        return json.loads(output or "null")
    except ValueError as exc:
        raise DeployPhaseError(f"`{command}` trả về dữ liệu không đọc được") from exc


def phase_check(nodes: list[dict], params: dict, on_host_update) -> None:
    new = _new_nodes(params)
    try:
        hosts = _ceph(params, "ceph orch host ls") or []
    except ExecutorError as exc:
        raise DeployPhaseError(f"Không đọc được danh sách host của cụm qua {_admin_mon(params)['ip']}: {exc}") from exc
    existing = {str(host.get("addr")) for host in hosts if isinstance(host, dict)}
    clash = [node["ip"] for node in new if node["ip"] in existing]
    if clash:
        raise DeployPhaseError(f"{', '.join(clash)} đã có trong cụm.")
    cluster_deploy._phase_ssh_check(new, params, on_host_update)


def phase_dependencies(nodes: list[dict], params: dict, on_host_update) -> None:
    cluster_deploy._phase_ceph_deploy_dependencies_cephadm(_new_nodes(params), params, on_host_update)


def phase_host_add(nodes: list[dict], params: dict, on_host_update) -> None:
    cluster_deploy._phase_cephadm_orch_host_add([_admin_mon(params), *_new_nodes(params)], params, on_host_update)


def phase_osd(nodes: list[dict], params: dict, on_host_update) -> None:
    osd_nodes = [node for node in _new_nodes(params) if "osd" in (node.get("roles") or [])]
    if not osd_nodes:
        on_host_update([{"host": node["ip"], "status": "done", "message": "không thêm OSD"}
                        for node in _new_nodes(params)])
        return
    cluster_deploy._phase_cephadm_orch_apply_osd([_admin_mon(params), *osd_nodes], params, on_host_update)


def _new_osds_up(params: dict) -> dict[str, tuple[int, int]]:
    """hostname -> (OSDs up and in, OSDs) for the new hosts."""
    tree = (_ceph(params, "ceph osd tree") or {}).get("nodes") or []
    by_id = {node.get("id"): node for node in tree if node.get("type") == "osd"}
    names = set((params.get("_node_hostnames") or {}).values())
    counts = {}
    for host in (node for node in tree if node.get("type") == "host"):
        if host.get("name") in names or str(host.get("name")).split(".")[0] in {n.split(".")[0] for n in names}:
            osds = [by_id.get(child) or {} for child in host.get("children") or []]
            good = sum(1 for osd in osds if osd.get("status") == "up" and float(osd.get("reweight") or 0) > 0)
            counts[str(host.get("name"))] = (good, len(osds))
    return counts


def phase_wait_osds(nodes: list[dict], params: dict, on_host_update) -> None:
    wanted = {node["ip"]: len(node.get("osd_disks") or []) for node in _new_nodes(params)
              if "osd" in (node.get("roles") or [])}
    if not wanted:
        return
    hostnames = params.get("_node_hostnames") or {}
    deadline = clock() + OSD_UP_SECONDS
    while True:
        counts = _new_osds_up(params)
        status, missing = [], False
        for ip, disks in wanted.items():
            name = str(hostnames.get(ip, ip))
            good, total = next((value for key, value in counts.items() if key.split(".")[0] == name.split(".")[0]), (0, 0))
            missing = missing or good < disks
            status.append({"host": ip, "status": "done" if good >= disks else "running",
                           "message": f"{good}/{disks} OSD up/in"})
        on_host_update(status)
        if not missing:
            return
        if clock() >= deadline:
            raise DeployPhaseError(f"OSD trên node mới chưa up/in sau {OSD_UP_SECONDS // 60} phút; kiểm tra "
                                   "`ceph orch ps` và `ceph orch device ls`.")
        sleep(POLL_SECONDS)


PHASES: dict[str, list[tuple[str, str, int, Callable]]] = {
    ACTION_ID: [
        ("expand_check", "Kiểm tra node mới (SSH, OS, đĩa trống, chưa thuộc cụm)", 15, phase_check),
        ("expand_dependencies", "Cài phụ thuộc (chrony, podman/docker)", 35, phase_dependencies),
        ("expand_host_add", "Thêm host vào cụm (orch host add)", 55, phase_host_add),
        ("expand_osd", "Tạo OSD trên đĩa đã chọn", 75, phase_osd),
        ("expand_wait", "Chờ OSD mới up/in (tối đa 15 phút)", 95, phase_wait_osds),
    ],
}


# --- config -------------------------------------------------------------------------------

def _with(raw: str | None, added: list[str]) -> str:
    items = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    return ",".join(items + [ip for ip in added if ip not in items])


def apply_config(params: dict) -> None:
    """Add the new OSD hosts to the cluster's OSD node list."""
    added = [node["ip"] for node in params.get("nodes") or [] if "osd" in (node.get("roles") or [])]
    if not added:
        return
    cluster_id = params.get("_cluster_id")
    with db.SessionLocal() as session:
        row = session.get(Cluster, cluster_id) if cluster_id else None
        if row is not None and not row.is_default:
            row.ceph_osd_nodes = _with(row.ceph_osd_nodes, added)
            session.commit()
            return
    from shared.clusters import sync_default_cluster_from_env

    name = env_config.CLUSTER_ENV_NAMES["ceph_osd_nodes"]
    current = env_config.read_env_values([name])
    env_config.update_env_file_batch({name: _with(current.get(name), added)})
    with db.SessionLocal() as session:
        sync_default_cluster_from_env(session)


def preview(params: dict) -> str:
    rows = "; ".join(f"{node.get('ip')}: {', '.join(node.get('osd_disks') or []) or 'không thêm OSD'}"
                     for node in params.get("nodes") or [])
    return (f"# qua MON {params.get('_admin_mon')}: kiểm tra SSH/OS/đĩa trống ({rows}); cài chrony + podman; "
            "thêm khoá /etc/ceph/ceph.pub; ceph orch host add <hostname> <ip>; ceph orch daemon add osd <host>:<disk>; "
            f"chờ OSD up/in. {shlex.quote('ceph orch host ls')} được kiểm tra trước để không thêm trùng host")
