"""Replace a failed OSD on a cephadm cluster, keeping its ID (Dashboard "Thay OSD hỏng", 10/10/2026).

Plan/in-progress/croit-parity-operations-plan-2026-10-10.md, C1. Two lifecycle
actions, both run by cluster_deploy.run() and both DESTRUCTIVE (typed
confirmation):

* ``replace_failed_osd`` — preflight, then ``ceph orch osd rm <id> --replace``:
  cephadm moves the data off, destroys the OSD and keeps its ID for the new
  disk. Waits up to WAIT_SECONDS for the OSD to become ``destroyed``; a longer
  data move is left running, as with node removal.
* ``finish_replace_osd`` — once the disk has been swapped (or the old one is
  to be reused): checks the OSD is ``destroyed``, zaps the device, creates the
  OSD again on that host and device (cephadm reuses the destroyed ID), and
  waits for it to come up and in.

A healthy OSD is only replaced when its data has somewhere to go: enough
other hosts for the largest pool size and enough free space. A failed (down)
OSD holds no data the cluster can still read from it, so it may be replaced
even when that leaves the cluster degraded until the new disk is in.
"""

from __future__ import annotations

import logging
import re
import shlex
import time
from typing import Any, Callable

from worker.executor import node_removal
from worker.executor.cluster_deploy import DeployPhaseError

logger = logging.getLogger(__name__)

ACTION_START = "replace_failed_osd"
ACTION_FINISH = "finish_replace_osd"
ACTION_IDS = frozenset({ACTION_START, ACTION_FINISH})
WAIT_SECONDS = 20 * 60
UP_WAIT_SECONDS = 15 * 60
POLL_SECONDS = 30
_DEVICE_RE = re.compile(r"^/dev/[A-Za-z0-9/_.-]+$")

clock: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep


def _ceph(params: dict, command: str, *, as_json: bool = True) -> Any:
    """A Ceph command on one of the cluster's MONs, with the cluster's own SSH identity."""
    return node_removal._ceph({**params, "targets": []}, command, as_json=as_json)


def _osd_id(params: dict) -> int:
    raw = params.get("osd_id")
    try:
        osd_id = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise DeployPhaseError("Thiếu hoặc sai ID OSD.") from exc
    if osd_id < 0:
        raise DeployPhaseError("ID OSD phải không âm.")
    return osd_id


def _osd_node(params: dict, osd_id: int) -> dict | None:
    tree = (_ceph(params, "ceph osd tree") or {}).get("nodes") or []
    return next((node for node in tree if node.get("type") == "osd" and node.get("id") == osd_id), None)


def _status(params: dict, state: str, message: str) -> list[dict]:
    return [{"host": f"osd.{params.get('osd_id')}", "status": state, "message": message}]


# --- preflight ----------------------------------------------------------------------------

def _check_redundancy(params: dict, osd_id: int, is_up: bool) -> None:
    """A healthy OSD needs another home for its data; a down one has nothing left to move."""
    tree = (_ceph(params, "ceph osd tree") or {}).get("nodes") or []
    hosts = [node for node in tree if node.get("type") == "host"]
    other_hosts = sum(1 for node in hosts if [child for child in node.get("children") or [] if child != osd_id])
    pools = _ceph(params, "ceph osd pool ls detail") or []
    needed = max((int(pool.get("size") or 0) for pool in pools), default=0)
    if is_up and other_hosts < needed:
        raise DeployPhaseError(
            f"osd.{osd_id} còn đang chạy nhưng chỉ {other_hosts} host khác có OSD, ít hơn số bản sao lớn nhất "
            f"của pool ({needed}): dữ liệu không có chỗ để dời. Chỉ thay khi OSD đã hỏng (down), hoặc thêm host trước.")
    if not is_up:
        return
    nodes = (_ceph(params, "ceph osd df") or {}).get("nodes") or []
    moving = sum(int(node.get("kb_used") or 0) for node in nodes if node.get("id") == osd_id)
    rest = [node for node in nodes if node.get("id") != osd_id and float(node.get("reweight") or 0) > 0]
    free = sum(int(node.get("kb_avail") or 0) for node in rest)
    total = sum(int(node.get("kb") or 0) for node in rest)
    used_after = (sum(int(node.get("kb_used") or 0) for node in rest) + moving) / total if total else 1.0
    nearfull = float((_ceph(params, "ceph osd dump") or {}).get("nearfull_ratio") or 0.85)
    if moving * node_removal.CAPACITY_MARGIN > free or used_after > nearfull - node_removal.NEARFULL_HEADROOM:
        raise DeployPhaseError(
            f"Không đủ chỗ: cần dời {moving / 1048576:.1f} GiB, các OSD còn lại trống {free / 1048576:.1f} GiB, "
            f"sau khi dời sẽ dùng {used_after:.0%} (ngưỡng nearfull {nearfull:.0%}).")


def phase_preflight(nodes: list[dict], params: dict, on_host_update) -> None:
    osd_id = _osd_id(params)
    on_host_update(_status(params, "running", "kiểm tra an toàn"))
    if str((_ceph(params, "ceph health") or {}).get("status")) == "HEALTH_ERR":
        raise DeployPhaseError("Cụm đang HEALTH_ERR — xử lý lỗi trước khi thay OSD.")
    node = _osd_node(params, osd_id)
    if node is None:
        raise DeployPhaseError(f"osd.{osd_id} không có trong `ceph osd tree` của cụm này.")
    if node.get("status") == "destroyed":
        raise DeployPhaseError(f"osd.{osd_id} đã ở trạng thái destroyed — dùng \"Hoàn tất thay OSD\".")
    metadata = _ceph(params, f"ceph osd metadata {osd_id}") or {}
    hostname = str(metadata.get("hostname") or "")
    devices = [item for item in str(metadata.get("devices") or "").split(",") if item]
    if not hostname or len(devices) != 1:
        raise DeployPhaseError(f"Không xác định được host và đúng một ổ của osd.{osd_id} (devices={devices}).")
    is_up = node.get("status") == "up"
    _check_redundancy(params, osd_id, is_up)
    params["_hostname"], params["_device"], params["_was_up"] = hostname, f"/dev/{devices[0]}", is_up
    # Recorded in the persisted progress: "Hoàn tất thay OSD" reads the host and disk from here.
    status = _status(params, "done", f"{hostname}:{params['_device']} — an toàn để thay "
                                     f"({'đang up, dữ liệu sẽ được dời' if is_up else 'đã down'})")
    status[0].update(osd_host=hostname, osd_device=params["_device"])
    on_host_update(status)


# --- removal ------------------------------------------------------------------------------

def phase_remove(nodes: list[dict], params: dict, on_host_update) -> None:
    osd_id = _osd_id(params)
    _ceph(params, f"ceph orch osd rm {osd_id} --replace", as_json=False)
    on_host_update(_status(params, "done", "đã yêu cầu `ceph orch osd rm --replace` (giữ ID)"))


def _destroyed(params: dict, osd_id: int) -> bool:
    node = _osd_node(params, osd_id)
    return node is not None and node.get("status") == "destroyed"


def phase_wait_destroyed(nodes: list[dict], params: dict, on_host_update) -> None:
    osd_id = _osd_id(params)
    deadline = clock() + WAIT_SECONDS
    while True:
        if _destroyed(params, osd_id):
            on_host_update(_status(params, "done", f"osd.{osd_id} đã destroyed, giữ ID; thay ổ "
                                                   f"{params.get('_device')} trên {params.get('_hostname')} rồi bấm "
                                                   "\"Hoàn tất thay OSD\""))
            return
        if clock() >= deadline:
            params["_removal_pending"] = True
            on_host_update(_status(params, "done", "vẫn đang dời dữ liệu; bấm \"Hoàn tất thay OSD\" khi OSD đã "
                                                   "destroyed (cephadm tiếp tục dời)"))
            return
        on_host_update(_status(params, "running", "đang dời dữ liệu khỏi OSD"))
        sleep(POLL_SECONDS)


# --- finish: new disk -----------------------------------------------------------------------

def phase_check_destroyed(nodes: list[dict], params: dict, on_host_update) -> None:
    osd_id = _osd_id(params)
    if not _destroyed(params, osd_id):
        raise DeployPhaseError(f"osd.{osd_id} chưa ở trạng thái destroyed — dữ liệu có thể vẫn đang được dời.")
    device = str(params.get("device") or params.get("_device") or "")
    if not _DEVICE_RE.match(device) or not params.get("_hostname"):
        raise DeployPhaseError("Thiếu host hoặc đường dẫn ổ hợp lệ cho OSD mới.")
    params["_device"] = device
    on_host_update(_status(params, "done", f"osd.{osd_id} destroyed; ổ mới {params['_hostname']}:{device}"))


def phase_zap(nodes: list[dict], params: dict, on_host_update) -> None:
    host, device = shlex.quote(params["_hostname"]), shlex.quote(params["_device"])
    _ceph(params, f"ceph orch device zap {host} {device} --force", as_json=False)
    on_host_update(_status(params, "done", f"đã xoá sạch {params['_device']}"))


def _osd_up_in(params: dict, osd_id: int) -> bool:
    node = _osd_node(params, osd_id)
    return node is not None and node.get("status") == "up" and float(node.get("reweight") or 0) > 0


def phase_create(nodes: list[dict], params: dict, on_host_update) -> None:
    """Create the OSD again; an existing all-available-devices spec may already have done it."""
    osd_id = _osd_id(params)
    sleep(POLL_SECONDS)
    if _osd_up_in(params, osd_id):
        on_host_update(_status(params, "done", "cephadm đã tự tạo lại OSD trên ổ trống"))
        return
    target = shlex.quote(f"{params['_hostname']}:{params['_device']}")
    _ceph(params, f"ceph orch daemon add osd {target}", as_json=False)
    on_host_update(_status(params, "done", f"đã yêu cầu tạo OSD trên {params['_hostname']}:{params['_device']}"))


def phase_wait_up(nodes: list[dict], params: dict, on_host_update) -> None:
    osd_id = _osd_id(params)
    deadline = clock() + UP_WAIT_SECONDS
    while not _osd_up_in(params, osd_id):
        if clock() >= deadline:
            raise DeployPhaseError(f"osd.{osd_id} chưa up/in sau {UP_WAIT_SECONDS // 60} phút; kiểm tra "
                                   "`ceph orch ps` và log cephadm trên host.")
        on_host_update(_status(params, "running", f"chờ osd.{osd_id} up/in"))
        sleep(POLL_SECONDS)
    on_host_update(_status(params, "done", f"osd.{osd_id} đã up/in trên ổ mới; cụm sẽ tự cân bằng lại dữ liệu"))


PHASES: dict[str, list[tuple[str, str, int, Callable]]] = {
    ACTION_START: [
        ("osd_preflight", "Kiểm tra an toàn (số bản sao, dung lượng, trạng thái OSD)", 15, phase_preflight),
        ("osd_remove", "ceph orch osd rm --replace (giữ ID)", 35, phase_remove),
        ("osd_wait", "Chờ dời dữ liệu và OSD destroyed (tối đa 20 phút)", 95, phase_wait_destroyed),
    ],
    ACTION_FINISH: [
        ("osd_check", "Kiểm tra OSD đã destroyed", 15, phase_check_destroyed),
        ("osd_zap", "Xoá sạch ổ mới (ceph orch device zap)", 35, phase_zap),
        ("osd_create", "Tạo lại OSD cùng ID trên ổ mới", 60, phase_create),
        ("osd_up", "Chờ OSD up/in (tối đa 15 phút)", 95, phase_wait_up),
    ],
}
