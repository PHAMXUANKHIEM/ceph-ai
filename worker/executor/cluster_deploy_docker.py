"""Docker-manual backend for the additional Deploy Cluster B profile.

All paths, ports, image and cluster identity come from the approved action
profile. The backend deliberately has no cleanup path: preflight blocks on
existing namespace resources and failures preserve remote state for review.
"""

import base64
from contextvars import ContextVar
import hashlib
import json
import logging
import re
import shlex
import time
from datetime import datetime

from shared import env_config
from worker.executor.ssh_executor import ExecutorError, execute_command as _default_execute_command

logger = logging.getLogger(__name__)
_ACTION_PARAMS: ContextVar[dict | None] = ContextVar("docker_deploy_action_params", default=None)


def execute_command(host: str, command: str) -> str:
    """Use the deployment incident's cluster-scoped SSH identity when set."""
    params = _ACTION_PARAMS.get()
    if params is None:
        return _default_execute_command(host, command)
    from worker.executor.cluster_deploy import _gate_execute

    return _gate_execute(host, command, params)


class DockerDeployError(Exception):
    """A safe, operator-facing failure in the Docker deployment workflow."""


def _q(value) -> str:
    return shlex.quote(str(value))


def _profile(params: dict) -> dict:
    profile = {
        "name": str(params.get("cluster_name", "")),
        "config_dir": str(params.get("config_dir", "")),
        "data_dir": str(params.get("data_dir", "")),
        "image": str(params.get("image_reference", "")),
        "fsid": str(params.get("fsid", "")),
        "v1": int(params.get("mon_v1_port", 0)),
        "v2": int(params.get("mon_v2_port", 0)),
    }
    if (
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", profile["name"])
        or not re.fullmatch(r"/etc/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", profile["config_dir"])
        or profile["config_dir"] == "/etc/ceph"
        or not re.fullmatch(r"/var/lib/[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", profile["data_dir"])
        or profile["data_dir"] == "/var/lib/ceph"
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}", profile["image"])
        or not re.fullmatch(r"[0-9a-fA-F-]{36}", profile["fsid"])
        or not 1 <= profile["v1"] <= 65535
        or not 1 <= profile["v2"] <= 65535
        or profile["v1"] == profile["v2"]
    ):
        raise DockerDeployError("Hồ sơ Docker manual thiếu hoặc chứa thông số không hợp lệ")
    return profile


def _mon_nodes(nodes: list[dict]) -> list[dict]:
    return [node for node in nodes if "mon" in (node.get("roles") or [])]


def _container_name(profile: dict, role: str, hostname: str, suffix: str = "") -> str:
    parts = ["ceph", profile["name"], role, hostname]
    if suffix:
        parts.append(suffix)
    return "-".join(re.sub(r"[^A-Za-z0-9_.-]", "-", part) for part in parts).lower()[:120]


def _mounts(profile: dict, *, privileged: bool = False) -> str:
    mounts = [
        f"-v {_q(profile['config_dir'])}:/etc/ceph",
        f"-v {_q(profile['data_dir'])}:/var/lib/ceph",
    ]
    if privileged:
        mounts.extend(("-v /dev:/dev", "-v /sys:/sys", "-v /run/lvm:/run/lvm", "-v /run/udev:/run/udev"))
    return " ".join(mounts)


def _docker_run(host: str, profile: dict, args: str, *, privileged: bool = False) -> str:
    privilege = "--privileged " if privileged else ""
    command = (
        f"docker run --rm --network host {privilege}{_mounts(profile, privileged=privileged)} "
        f"{_q(profile['image'])} {args}"
    )
    return execute_command(host, command)


def _write_remote(host: str, path: str, value: str, *, mode: str = "600") -> None:
    payload = base64.b64encode(value.encode("utf-8")).decode("ascii")
    command = (
        f"mkdir -p {_q(path.rsplit('/', 1)[0])} && printf %s {_q(payload)} | base64 -d > {_q(path)} "
        f"&& chmod {mode} {_q(path)}"
    )
    execute_command(host, command)


def _read_remote(host: str, path: str) -> str:
    return execute_command(host, f"base64 {_q(path)}").strip()


def _copy_remote_b64(host: str, path: str, payload: str) -> None:
    mode = "644" if path.endswith("/monmap") else "600"
    command = (
        f"mkdir -p {_q(path.rsplit('/', 1)[0])} && printf %s {_q(payload)} | base64 -d > {_q(path)} "
        f"&& chmod {mode} {_q(path)}"
    )
    execute_command(host, command)


def _ceph_conf(params: dict, profile: dict, mons: list[dict], hostnames: dict[str, str]) -> str:
    mon_members = ",".join(hostnames[node["ip"]] for node in mons)
    mon_addresses = ",".join(
        f"[v2:{node['ip']}:{profile['v2']}/0,v1:{node['ip']}:{profile['v1']}/0]"
        for node in mons
    )
    public_network = str(params.get("public_network") or "")
    cluster_network = str(params.get("cluster_network") or public_network)
    return "\n".join(
        [
            "[global]",
            f"fsid = {profile['fsid']}",
            f"mon initial members = {mon_members}",
            f"mon host = {mon_addresses}",
            f"public network = {public_network}",
            f"cluster network = {cluster_network}",
            "auth cluster required = cephx",
            "auth service required = cephx",
            "auth client required = cephx",
            "ms bind msgr1 = true",
            "ms bind msgr2 = true",
            "mon data = /var/lib/ceph/mon/ceph-$name",
            f"osd pool default size = {int(params.get('osd_pool_default_size', 3))}",
            f"osd pool default min size = {int(params.get('osd_pool_default_min_size', 2))}",
            "",
        ]
    )


def _assert_namespace_free(host: str, profile: dict, *, check_mon_ports: bool = True) -> None:
    for directory in (profile["config_dir"], profile["data_dir"]):
        exists = execute_command(
            host,
            f"if [ -e {_q(directory)} ] || [ -L {_q(directory)} ]; then echo EXISTS; fi",
        ).strip()
        if exists == "EXISTS":
            raise DockerDeployError(
                f"{host}: {directory} đã tồn tại; không ghi đè namespace hoặc state đang có"
            )

    if check_mon_ports:
        listeners = execute_command(host, "ss -H -ltn | awk '{print $4}'")
        for address in listeners.splitlines():
            match = re.search(r"(?:^|:)(\d+)$", address.strip())
            if match and int(match.group(1)) in (profile["v1"], profile["v2"]):
                raise DockerDeployError(
                    f"{host}: cổng MON {match.group(1)} đang có listener ({address.strip()})"
                )

    prefix = f"ceph-{profile['name'].lower()}-"
    existing = execute_command(
        host,
        "docker ps -a --format '{{.Names}}' | "
        f"grep -F {_q(prefix)} || true",
    ).strip()
    if existing:
        raise DockerDeployError(f"{host}: container thuộc namespace đã tồn tại: {existing}")


def _assert_osd_disk_safe(host: str, device: str) -> None:
    quoted = _q(device)
    result = execute_command(
        host,
        f"test -b {quoted} || {{ echo MISSING; exit 1; }}; "
        f"for dev in $(lsblk -nrpo NAME {quoted}); do "
        "if wipefs -n \"$dev\" 2>/dev/null | tail -n +2 | grep -q .; then echo SIGNATURE:$dev; fi; "
        "if lsblk -dnro MOUNTPOINT \"$dev\" | grep -q .; then echo MOUNTED:$dev; fi; "
        "if pvs --noheadings -o pv_name 2>/dev/null | sed 's/^[[:space:]]*//' | grep -Fxq \"$dev\"; "
        "then echo LVM:$dev; fi; done",
    )
    if any(marker in result for marker in ("MISSING", "SIGNATURE", "MOUNTED", "LVM")):
        raise DockerDeployError(f"{host}: {device} thiếu hoặc có filesystem, mount, LVM; từ chối ghi lên disk")


def _preflight_node(node: dict, profile: dict) -> tuple[str, str]:
    """Read-only validation for one host, including only its assigned OSD disks."""
    host = node["ip"]
    hostname = execute_command(host, "hostname -s").strip()
    if not hostname:
        raise DockerDeployError(f"{host}: không xác định được hostname")
    owned_ips = execute_command(
        host,
        "ip -o -4 addr show | awk '{print $4}' | cut -d/ -f1",
    ).split()
    if host not in owned_ips:
        raise DockerDeployError(f"{host}: IP này không được gán trên node (NIC/IP không khớp)")
    docker_info = execute_command(host, "docker info --format '{{.ServerVersion}}'").strip()
    if not docker_info:
        raise DockerDeployError(f"{host}: Docker daemon chưa sẵn sàng")

    inventory = execute_command(
        host,
        "{ cephadm ls 2>/dev/null || true; "
        "systemctl list-units --type=service --no-legend 'ceph*' 2>/dev/null || true; "
        "docker ps -a --format '{{.Names}} {{.Mounts}}' 2>/dev/null | grep -Ei 'ceph|vitastor' || true; }",
    )
    _assert_namespace_free(host, profile, check_mon_ports="mon" in (node.get("roles") or []))
    if "osd" in (node.get("roles") or []):
        lvm_tools = execute_command(
            host,
            "command -v lvm >/dev/null 2>&1 && command -v dmsetup >/dev/null 2>&1 "
            "&& command -v pvs >/dev/null 2>&1 && command -v wipefs >/dev/null 2>&1 "
            "&& command -v lsblk >/dev/null 2>&1 && test -d /run/lvm && echo READY || true",
        ).strip()
        if lvm_tools != "READY":
            raise DockerDeployError(
                f"{host}: thiếu lvm2/dmsetup hoặc /run/lvm; cần chuẩn bị LVM trước khi tạo OSD"
            )
        for device in node.get("osd_disks") or []:
            _assert_osd_disk_safe(host, device)
    return hostname, inventory[-3000:]


def _preflight(nodes: list[dict], params: dict, update) -> None:
    profile = _profile(params)
    expected_fingerprint = params.get("_cluster_config_fingerprint")
    if expected_fingerprint and expected_fingerprint != env_config.current_cluster_config_fingerprint():
        raise DockerDeployError(
            "Cấu hình cụm đã thay đổi kể từ lúc đề xuất; tạo proposal mới rồi duyệt lại"
        )
    statuses = [{"host": node["ip"], "status": "pending"} for node in nodes]
    update(statuses)
    hostnames: dict[str, str] = {}
    for index, node in enumerate(nodes):
        host = node["ip"]
        statuses[index]["status"] = "running"
        update(statuses)
        try:
            hostname, inventory = _preflight_node(node, profile)
            hostnames[host] = hostname
            params.setdefault("_docker_inventory", {})[host] = inventory
        except (ExecutorError, DockerDeployError) as exc:
            statuses[index]["status"] = "failed"
            statuses[index]["message"] = str(exc)
            update(statuses)
            raise DockerDeployError(str(exc)) from exc
        statuses[index]["status"] = "done"
        existing_ceph = params.get("_docker_inventory", {}).get(host, "").strip()
        statuses[index]["message"] = (
            f"Docker sẵn sàng; hostname {hostname}; namespace và port của profile chưa bị chiếm"
            + ("; đã ghi nhận service/container Ceph hiện có trên node dùng chung" if existing_ceph else "")
        )
        update(statuses)
    mon_hostnames = [hostnames[node["ip"]] for node in _mon_nodes(nodes)]
    if len(mon_hostnames) != len(set(mon_hostnames)):
        raise DockerDeployError("Hai node MON trả cùng hostname; không thể tạo monmap không nhập nhằng")
    params["_docker_hostnames"] = hostnames


def _pull_image(nodes: list[dict], params: dict, update) -> None:
    profile = _profile(params)
    statuses = [{"host": node["ip"], "status": "pending"} for node in nodes]
    update(statuses)
    for index, node in enumerate(nodes):
        statuses[index]["status"] = "running"
        update(statuses)
        try:
            execute_command(node["ip"], f"docker pull {_q(profile['image'])}")
        except ExecutorError as exc:
            statuses[index].update(status="failed", message=str(exc))
            update(statuses)
            raise DockerDeployError(f"{node['ip']}: không tải được image {profile['image']}: {exc}") from exc
        statuses[index]["status"] = "done"
        update(statuses)


def _verify_image_version(nodes: list[dict], params: dict, update) -> None:
    profile = _profile(params)
    expected = str(params.get("version", "")).strip()
    statuses = [{"host": node["ip"], "status": "pending"} for node in nodes]
    update(statuses)
    for index, node in enumerate(nodes):
        host = node["ip"]
        statuses[index].update(status="running", message="Đối chiếu version Ceph trong image")
        update(statuses)
        try:
            output = execute_command(host, f"docker run --rm {_q(profile['image'])} ceph --version 2>&1")
        except ExecutorError as exc:
            statuses[index].update(status="failed", message=str(exc))
            update(statuses)
            raise DockerDeployError(f"{host}: không đọc được version từ image {profile['image']}: {exc}") from exc
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", output)
        actual = match.group(1) if match else "không xác định"
        if actual != expected:
            statuses[index].update(status="failed", message=f"Yêu cầu {expected}, image có {actual}")
            update(statuses)
            raise DockerDeployError(
                f"{host}: version image không khớp, yêu cầu Ceph {expected}, image chứa {actual}"
            )
        statuses[index].update(status="done", message=f"Image xác nhận Ceph {actual}")
        update(statuses)


def _prepare_namespace(nodes: list[dict], params: dict, update) -> None:
    profile = _profile(params)
    mons = _mon_nodes(nodes)
    hostnames = params.get("_docker_hostnames") or {}
    if not mons or any(node["ip"] not in hostnames for node in mons):
        raise DockerDeployError("Thiếu MON hoặc hostname từ kết quả preflight")

    # Recheck all target hosts immediately before the first namespace write;
    # image pulls and approval delays may have allowed another operator to
    # claim a path, port or container name after the initial preflight.
    for node in nodes:
        _assert_namespace_free(
            node["ip"], profile, check_mon_ports="mon" in (node.get("roles") or [])
        )

    mon_members = ",".join(hostnames[node["ip"]] for node in mons)
    conf = _ceph_conf(params, profile, mons, hostnames)
    manifest = {
        "deployment_id": str(params.get("deployment_id", "")),
        "cluster_name": profile["name"],
        "fsid": profile["fsid"],
        "version": str(params.get("version", "")),
        "image_reference": profile["image"],
        "config_dir": profile["config_dir"],
        "data_dir": profile["data_dir"],
        "mon_v1_port": profile["v1"],
        "mon_v2_port": profile["v2"],
        "nodes": nodes,
        "public_network": params.get("public_network", ""),
        "cluster_network": params.get("cluster_network", ""),
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest_sha256 = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
    statuses = [{"host": node["ip"], "status": "pending"} for node in nodes]
    update(statuses)
    for index, node in enumerate(nodes):
        host = node["ip"]
        statuses[index]["status"] = "running"
        update(statuses)
        try:
            execute_command(host, f"mkdir -p {_q(profile['config_dir'])} {_q(profile['data_dir'])}")
            _write_remote(host, f"{profile['config_dir']}/ceph.conf", conf, mode="644")
            _write_remote(
                host,
                f"{profile['config_dir']}/.ceph-aiops-deploy.json",
                manifest_json,
                mode="600",
            )
        except ExecutorError as exc:
            statuses[index].update(status="failed", message=str(exc))
            update(statuses)
            raise DockerDeployError(f"{host}: không tạo được namespace/config: {exc}") from exc
        statuses[index]["status"] = "done"
        statuses[index]["message"] = f"Config riêng đã ghi; manifest SHA-256 {manifest_sha256}"
        update(statuses)

    first = mons[0]["ip"]
    try:
        for directory in ("mon", "mgr", "bootstrap-osd"):
            execute_command(first, f"mkdir -p {_q(profile['data_dir'] + '/' + directory)}")
        _docker_run(
            first,
            profile,
            "ceph-authtool --create-keyring /var/lib/ceph/mon.keyring --gen-key "
            "-n mon. --cap mon 'allow *'",
        )
        _docker_run(
            first,
            profile,
            "ceph-authtool --create-keyring /etc/ceph/ceph.client.admin.keyring --gen-key "
            "-n client.admin --cap mon 'allow *' --cap osd 'allow *' --cap mds 'allow *' --cap mgr 'allow *'",
        )
        _docker_run(
            first,
            profile,
            "ceph-authtool --create-keyring /var/lib/ceph/bootstrap-osd/ceph.keyring --gen-key "
            "-n client.bootstrap-osd --cap mon 'profile bootstrap-osd' --cap mgr 'allow r'",
        )
        _docker_run(
            first,
            profile,
            "ceph-authtool /var/lib/ceph/mon.keyring --import-keyring "
            "/etc/ceph/ceph.client.admin.keyring",
        )
        _docker_run(
            first,
            profile,
            "ceph-authtool /var/lib/ceph/mon.keyring --import-keyring "
            "/var/lib/ceph/bootstrap-osd/ceph.keyring",
        )
        addv = " ".join(
            f"--addv {_q(hostnames[node['ip']])} {_q('[v2:' + node['ip'] + ':' + str(profile['v2']) + '/0,v1:' + node['ip'] + ':' + str(profile['v1']) + '/0]')}"
            for node in mons
        )
        _docker_run(
            first,
            profile,
            f"monmaptool --create --fsid {_q(profile['fsid'])} {addv} "
            "/var/lib/ceph/monmap",
        )
        artifacts = {
            "/var/lib/ceph/mon.keyring": _read_remote(first, f"{profile['data_dir']}/mon.keyring"),
            "/etc/ceph/ceph.client.admin.keyring": _read_remote(
                first, f"{profile['config_dir']}/ceph.client.admin.keyring"
            ),
            "/var/lib/ceph/bootstrap-osd/ceph.keyring": _read_remote(
                first, f"{profile['data_dir']}/bootstrap-osd/ceph.keyring"
            ),
            "/var/lib/ceph/monmap": _read_remote(first, f"{profile['data_dir']}/monmap"),
        }
        for node in nodes:
            if node["ip"] == first:
                continue
            for container_path, encoded in artifacts.items():
                host_path = (
                    profile["config_dir"] + "/ceph.client.admin.keyring"
                    if container_path == "/etc/ceph/ceph.client.admin.keyring"
                    else profile["data_dir"] + container_path.removeprefix("/var/lib/ceph")
                )
                _copy_remote_b64(node["ip"], host_path, encoded)
    except (ExecutorError, DockerDeployError) as exc:
        raise DockerDeployError(f"Không tạo được keyring/monmap cho cluster {profile['name']}: {exc}") from exc

    params["_docker_mon_members"] = mon_members
    params["_docker_artifacts_ready"] = True
    params["_docker_manifest_sha256"] = manifest_sha256


def _start_monitors(nodes: list[dict], params: dict, update) -> None:
    profile = _profile(params)
    mons = _mon_nodes(nodes)
    hostnames = params.get("_docker_hostnames") or {}
    statuses = [{"host": node["ip"], "status": "pending"} for node in mons]
    update(statuses)
    for index, node in enumerate(mons):
        host = node["ip"]
        name = hostnames[host]
        cname = _container_name(profile, "mon", name)
        statuses[index]["status"] = "running"
        update(statuses)
        try:
            _docker_run(
                host,
                profile,
                f"ceph-mon --mkfs -i {_q(name)} --monmap /var/lib/ceph/monmap "
                "--keyring /var/lib/ceph/mon.keyring",
            )
            _docker_run(host, profile, f"chown -R ceph:ceph /var/lib/ceph/mon/ceph-{_q(name)}")
            run = (
                f"docker run -d --name {_q(cname)} --network host --restart always "
                f"--label ceph-aiops.deployment={_q(params.get('deployment_id', ''))} "
                f"--label ceph-aiops.fsid={_q(profile['fsid'])} "
                f"{_mounts(profile)} {_q(profile['image'])} ceph-mon -i {_q(name)} -f"
            )
            execute_command(host, run)
        except ExecutorError as exc:
            statuses[index].update(status="failed", message=str(exc))
            update(statuses)
            raise DockerDeployError(f"{host}: khởi tạo MON {name} thất bại: {exc}") from exc
        statuses[index]["status"] = "done"
        update(statuses)


def _wait_quorum(nodes: list[dict], params: dict, update) -> None:
    profile = _profile(params)
    mons = _mon_nodes(nodes)
    hostnames = params.get("_docker_hostnames") or {}
    first = mons[0]["ip"]
    cname = _container_name(profile, "mon", hostnames[first])
    update([{"host": first, "status": "running", "message": "Đang chờ quorum tối đa 180 giây"}])
    deadline = time.monotonic() + 180
    last_error = ""
    while time.monotonic() < deadline:
        try:
            output = execute_command(first, f"docker exec {_q(cname)} ceph quorum_status --format json")
            data = json.loads(output)
            quorum = data.get("quorum_names") or []
            if len(quorum) >= len(mons):
                update([{"host": first, "status": "done", "message": f"MON quorum {len(quorum)}/{len(mons)}"}])
                return
            last_error = f"quorum {len(quorum)}/{len(mons)}"
        except (ExecutorError, ValueError) as exc:
            last_error = str(exc)
        time.sleep(5)
    update([{"host": first, "status": "failed", "message": last_error}])
    raise DockerDeployError(f"MON không đạt quorum sau 180 giây: {last_error}")


def _create_managers(nodes: list[dict], params: dict, update) -> None:
    profile = _profile(params)
    mons = _mon_nodes(nodes)
    hostnames = params.get("_docker_hostnames") or {}
    first = mons[0]["ip"]
    mon_container = _container_name(profile, "mon", hostnames[first])
    mgrs = [node for node in nodes if "mgr" in (node.get("roles") or [])]
    statuses = [{"host": node["ip"], "status": "pending"} for node in mgrs]
    update(statuses)
    for index, node in enumerate(mgrs):
        host = node["ip"]
        name = hostnames.get(host)
        if not name:
            name = execute_command(host, "hostname -s").strip()
            hostnames[host] = name
        mgr_container = _container_name(profile, "mgr", name)
        mgr_data = f"{profile['data_dir']}/mgr/ceph-{name}"
        statuses[index]["status"] = "running"
        update(statuses)
        try:
            execute_command(host, f"mkdir -p {_q(mgr_data)}")
            mgr_keyring = execute_command(
                first,
                f"docker exec {_q(mon_container)} ceph auth get-or-create mgr.{_q(name)} "
                "mon 'allow profile mgr' osd 'allow *' mds 'allow *'",
            )
            _write_remote(host, f"{mgr_data}/keyring", mgr_keyring)
            _docker_run(host, profile, f"chown -R ceph:ceph /var/lib/ceph/mgr/ceph-{_q(name)}")
            execute_command(
                host,
                f"docker run -d --name {_q(mgr_container)} --network host --restart always "
                f"--label ceph-aiops.deployment={_q(params.get('deployment_id', ''))} "
                f"--label ceph-aiops.fsid={_q(profile['fsid'])} "
                f"{_mounts(profile)} {_q(profile['image'])} ceph-mgr -i {_q(name)} -f",
            )
        except ExecutorError as exc:
            statuses[index].update(status="failed", message=str(exc))
            update(statuses)
            raise DockerDeployError(f"{host}: tạo MGR {name} thất bại: {exc}") from exc
        statuses[index]["status"] = "done"
        update(statuses)


def _create_osds(nodes: list[dict], params: dict, update) -> None:
    profile = _profile(params)
    hostnames = params.get("_docker_hostnames") or {}
    tasks = [
        (node, disk)
        for node in nodes
        if "osd" in (node.get("roles") or [])
        for disk in (node.get("osd_disks") or [])
    ]
    statuses = [{"host": node["ip"], "status": "pending", "message": disk} for node, disk in tasks]
    update(statuses)
    for index, (node, disk) in enumerate(tasks):
        host = node["ip"]
        statuses[index]["status"] = "running"
        update(statuses)
        try:
            _assert_osd_disk_safe(host, disk)
            output = _docker_run(
                host,
                profile,
                f"ceph-volume lvm create --data {_q(disk)} --no-systemd 2>&1",
                privileged=True,
            )
            id_match = re.search(r"OSD ID:\s*(\d+)", output)
            fsid_match = re.search(r"OSD UUID:\s*([0-9a-fA-F-]{36})", output)
            if not id_match or not fsid_match:
                raise DockerDeployError(
                    f"{host} {disk}: ceph-volume không trả OSD ID/UUID xác định; cần kiểm tra thủ công"
                )
            osd_id, osd_fsid = id_match.group(1), fsid_match.group(1)
            name = hostnames.get(host) or execute_command(host, "hostname -s").strip()
            cname = _container_name(profile, "osd", name, osd_id)
            command = (
                "ceph-volume lvm activate --bluestore --no-systemd "
                f"{_q(osd_id)} {_q(osd_fsid)} && exec ceph-osd -i {_q(osd_id)} -f"
            )
            execute_command(
                host,
                f"docker run -d --name {_q(cname)} --network host --privileged --restart always "
                f"--label ceph-aiops.deployment={_q(params.get('deployment_id', ''))} "
                f"--label ceph-aiops.fsid={_q(profile['fsid'])} "
                f"{_mounts(profile, privileged=True)} --entrypoint /bin/bash {_q(profile['image'])} "
                f"-lc {_q(command)}",
            )
            statuses[index]["message"] = f"{disk} → osd.{osd_id} ({osd_fsid})"
        except (ExecutorError, DockerDeployError) as exc:
            statuses[index].update(status="failed", message=str(exc))
            update(statuses)
            raise DockerDeployError(f"{host} {disk}: tạo/khởi động OSD thất bại: {exc}") from exc
        statuses[index]["status"] = "done"
        update(statuses)


def _verify(nodes: list[dict], params: dict, update) -> None:
    profile = _profile(params)
    mons = _mon_nodes(nodes)
    hostnames = params.get("_docker_hostnames") or {}
    host = mons[0]["ip"]
    cname = _container_name(profile, "mon", hostnames[host])
    try:
        fsid = execute_command(host, f"docker exec {_q(cname)} ceph fsid").strip()
        status = execute_command(host, f"docker exec {_q(cname)} ceph -s --format json")
        status_json = json.loads(status)
        health = (status_json.get("health") or {}).get("status", "UNKNOWN")
        if fsid.lower() != profile["fsid"].lower():
            raise DockerDeployError(f"FSID hậu kiểm không khớp: nhận {fsid}")
        if health == "HEALTH_ERR":
            raise DockerDeployError("Cluster trả HEALTH_ERR; giữ state để điều tra")
        tree = json.loads(
            execute_command(host, f"docker exec {_q(cname)} ceph osd tree --format json")
        )
        osds_up = sum(
            1
            for entry in tree.get("nodes", [])
            if entry.get("type") == "osd" and entry.get("status") == "up"
        )
        expected_osds = sum(len(node.get("osd_disks") or []) for node in nodes)
        if osds_up < expected_osds:
            raise DockerDeployError(f"Chỉ có {osds_up}/{expected_osds} OSD ở trạng thái up")
        quorum = json.loads(
            execute_command(host, f"docker exec {_q(cname)} ceph quorum_status --format json")
        )
        quorum_count = len(quorum.get("quorum_names") or [])
        if quorum_count < len(mons):
            raise DockerDeployError(f"MON quorum chỉ đạt {quorum_count}/{len(mons)}")
        expected_ports = (profile["v1"], profile["v2"])
        for mon in mons:
            mon_host = mon["ip"]
            listening = execute_command(mon_host, "ss -H -ltn | awk '{print $4}'")
            ports = {
                int(match.group(1))
                for address in listening.splitlines()
                if (match := re.search(r"(?:^|:)(\d+)$", address.strip()))
            }
            missing = sorted(set(expected_ports) - ports)
            if missing:
                raise DockerDeployError(
                    f"{mon_host} ({hostnames.get(mon_host, '?')}): MON chưa listen đủ port {missing}"
                )
        update(
            [
                {
                    "host": host,
                    "status": "done",
                    "message": f"FSID đúng; health {health}; quorum {quorum_count}/{len(mons)}; OSD up {osds_up}/{expected_osds}",
                }
            ]
        )
    except (ExecutorError, ValueError) as exc:
        update([{"host": host, "status": "failed", "message": str(exc)}])
        raise DockerDeployError(f"Hậu kiểm Ceph thất bại: {exc}") from exc


_PHASES = [
    ("preflight", "Kiểm tra Docker, namespace, cổng và OSD disk", 8, _preflight),
    ("image_pull", "Tải image Ceph lên các node", 18, _pull_image),
    ("image_version", "Xác nhận version Ceph trong image", 22, _verify_image_version),
    ("namespace", "Tạo config, keyring và monmap riêng", 38, _prepare_namespace),
    ("mon_start", "Khởi tạo và khởi động MON containers", 55, _start_monitors),
    ("mon_quorum", "Chờ MON quorum", 62, _wait_quorum),
    ("mgr_start", "Tạo và khởi động MGR containers", 70, _create_managers),
    ("osd_create", "Tạo OSD trên các disk đã khai báo", 88, _create_osds),
    ("verify", "Hậu kiểm FSID và cluster health", 100, _verify),
]


def _run_with_action_context(action_pk: str, params: dict, write_progress) -> bool:
    """Run the isolated Docker profile; preserve remote state on every error."""
    nodes = params.get("nodes") or []
    progress = [
        {"step": key, "label": label, "pct": pct, "status": "pending", "hosts": []}
        for key, label, pct, _ in _PHASES
    ]
    write_progress(action_pk, progress)
    for index, (key, _label, _pct, phase) in enumerate(_PHASES):
        progress[index]["status"] = "running"
        progress[index]["started_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)

        def update(hosts, current=index):
            progress[current]["hosts"] = hosts
            write_progress(action_pk, progress)

        try:
            phase(nodes, params, update)
        except Exception as exc:
            progress[index]["status"] = "failed"
            progress[index]["message"] = str(exc)
            progress[index]["finished_at"] = datetime.utcnow().isoformat()
            write_progress(action_pk, progress)
            logger.exception("docker cluster deploy phase %s failed for action %s", key, action_pk)
            return False
        progress[index]["status"] = "done"
        progress[index]["finished_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)
    return True


def run(action_pk: str, params: dict, write_progress) -> bool:
    token = _ACTION_PARAMS.set(params)
    try:
        return _run_with_action_context(action_pk, params, write_progress)
    finally:
        _ACTION_PARAMS.reset(token)
