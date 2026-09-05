"""Bounded SSH workflows for Vitastor cluster lifecycle operations."""

from __future__ import annotations

import base64
import json
import os
import posixpath
import shlex
from collections.abc import Callable

import paramiko

from vitastor.client import CONNECT_TIMEOUT_SECONDS, KNOWN_HOSTS_PATH
from vitastor.client import normalize_status, query_status

COMMAND_TIMEOUT_SECONDS = 1800


class VitastorOperationError(RuntimeError):
    pass


def _run(host: str, ssh_user: str, ssh_key_path: str, command: str) -> str:
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(hostname=host, username=ssh_user, key_filename=ssh_key_path, timeout=CONNECT_TIMEOUT_SECONDS)
        client.save_host_keys(KNOWN_HOSTS_PATH)
        _stdin, stdout, stderr = client.exec_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
        output, error = stdout.read().decode(), stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        if code:
            raise VitastorOperationError(f"{host}: exit {code}: {(error or output).strip()[-1200:]}")
        return output.strip()
    except (OSError, paramiko.SSHException) as exc:
        raise VitastorOperationError(f"Không SSH được tới {host}: {exc}") from exc
    finally:
        client.close()


def _config_command(config: dict) -> str:
    encoded = base64.b64encode(json.dumps(config, indent=2).encode()).decode()
    return f"install -d -m 0755 /etc/vitastor && echo {shlex.quote(encoded)} | base64 -d > /etc/vitastor/vitastor.conf && chmod 0600 /etc/vitastor/vitastor.conf"


def deploy(params: dict, progress: Callable[[str, str, str], None]) -> None:
    nodes = params["nodes"]
    ssh_user, ssh_key = params["ssh_user"], params["ssh_key_path"]
    monitors = [n["host"] for n in nodes if "mon" in n["roles"]]
    etcd_addresses = [f"{host}:2379" for host in monitors]
    config = {"etcd_address": etcd_addresses, "etcd_prefix": params["etcd_prefix"], "osd_network": params["osd_network"]}

    progress("preflight", "running", "Kiểm tra SSH và thiết bị")
    for node in nodes:
        disks = node.get("disks", [])
        checks = ["test -r /etc/os-release"]
        for disk in disks:
            q = shlex.quote(disk)
            checks.append(f"test -b {q} && test -z \"$(lsblk -no MOUNTPOINT {q} | tr -d ' ')\"")
        _run(node["host"], ssh_user, ssh_key, "set -eu; " + "; ".join(checks))
    progress("preflight", "done", "Kiểm tra an toàn hoàn tất")

    progress("packages", "running", "Kiểm tra/cài gói Vitastor và Etcd")
    for node in nodes:
        if params.get("install_packages"):
            _run(node["host"], ssh_user, ssh_key, "set -eu; if command -v apt-get >/dev/null; then DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y vitastor etcd; elif command -v dnf >/dev/null; then dnf install -y vitastor etcd; elif command -v yum >/dev/null; then yum install -y vitastor etcd; else echo 'Không hỗ trợ package manager trên node' >&2; exit 2; fi")
        _run(node["host"], ssh_user, ssh_key, "command -v vitastor-cli >/dev/null && test -f /usr/lib/vitastor/mon/make-etcd")
    progress("packages", "done", "Binary Vitastor sẵn sàng")

    progress("config", "running", "Ghi cấu hình Vitastor đồng nhất")
    for node in nodes:
        _run(node["host"], ssh_user, ssh_key, "set -eu; " + _config_command(config))
    progress("config", "done", "Đã ghi /etc/vitastor/vitastor.conf")

    progress("monitors", "running", "Khởi tạo Etcd và monitor")
    for host in monitors:
        _run(host, ssh_user, ssh_key, "set -eu; /usr/lib/vitastor/mon/make-etcd; systemctl enable --now vitastor-etcd vitastor-mon")
    progress("monitors", "done", "Etcd và monitor đã khởi động")

    progress("osds", "running", "Chuẩn bị thiết bị OSD — thao tác ghi dữ liệu")
    for node in nodes:
        disks = node.get("disks", [])
        if not disks:
            continue
        quoted = " ".join(shlex.quote(disk) for disk in disks)
        _run(node["host"], ssh_user, ssh_key, f"set -eu; vitastor-disk prepare --dry-run {quoted}; vitastor-disk prepare {quoted}; systemctl start vitastor.target")
    progress("osds", "done", "OSD đã được khởi tạo")

    progress("verify", "running", "Kiểm tra trạng thái cụm")
    _run(monitors[0], ssh_user, ssh_key, "vitastor-cli --json --no-color status")
    progress("verify", "done", "Cụm phản hồi vitastor-cli status")


def delete(params: dict, progress: Callable[[str, str, str], None]) -> None:
    nodes = params["nodes"]
    ssh_user, ssh_key = params["ssh_user"], params["ssh_key_path"]
    wipe = params["wipe_disks"]
    progress("preflight", "running", "Kiểm tra SSH tới toàn bộ node")
    for node in nodes:
        _run(node["host"], ssh_user, ssh_key, "true")
    progress("preflight", "done", "SSH hoạt động")

    if wipe:
        progress("purge", "running", "Purge OSD — dữ liệu bị xoá vĩnh viễn")
        for node in nodes:
            disks = node.get("disks", [])
            if disks:
                quoted = " ".join(shlex.quote(disk) for disk in disks)
                _run(node["host"], ssh_user, ssh_key, f"vitastor-disk purge --force --allow-data-loss {quoted}")
        progress("purge", "done", "Đã purge các thiết bị OSD đã xác nhận")

    # Purge needs a live monitor/Etcd endpoint to remove OSD metadata, so
    # cluster control services are deliberately stopped only afterwards.
    progress("stop", "running", "Dừng service Vitastor")
    for node in nodes:
        _run(
            node["host"], ssh_user, ssh_key,
            "set -eu; command -v systemctl >/dev/null; "
            "osd_units=\"$(systemctl list-unit-files --type=service --no-legend 'vitastor-osd@*.service' | awk '{print $1}')\"; "
            "for unit in vitastor.target vitastor-mon vitastor-etcd $osd_units; do "
            "if systemctl is-active --quiet \"$unit\" 2>/dev/null; then systemctl stop \"$unit\"; fi; "
            "if systemctl is-active --quiet \"$unit\" 2>/dev/null; then "
            "echo \"$unit vẫn đang active sau khi stop\" >&2; exit 1; fi; "
            "done",
        )
    progress("stop", "done", "Đã dừng service")

    progress("cleanup", "running", "Vô hiệu hoá service và xoá cấu hình")
    for node in nodes:
        _run(
            node["host"], ssh_user, ssh_key,
            "set -eu; command -v systemctl >/dev/null; "
            "osd_units=\"$(systemctl list-unit-files --type=service --no-legend 'vitastor-osd@*.service' | awk '{print $1}')\"; "
            "for unit in vitastor.target vitastor-mon vitastor-etcd $osd_units; do "
            "if systemctl is-enabled --quiet \"$unit\" 2>/dev/null; then systemctl disable \"$unit\"; fi; "
            "done; rm -f /etc/vitastor/vitastor.conf",
        )
    progress("cleanup", "done", "Đã xoá cấu hình cụm")


def _assert_healthy(params: dict) -> None:
    status = query_status(
        params["management_host"], params["ssh_user"], params["ssh_key_path"],
        params.get("etcd_address", ""), params.get("etcd_prefix", "/vitastor"),
        params.get("config_path", ""), params.get("exec_mode", "none"),
        params.get("container_name", ""),
    )
    health = normalize_status(status)["health"]
    if health != "HEALTHY":
        raise VitastorOperationError(
            f"Cụm đang ở trạng thái {health}; dừng upgrade để tránh làm giảm khả năng chịu lỗi"
        )


def _upgrade_command(version: str) -> str:
    version_q = shlex.quote(version)
    return (
        "set -eu; "
        "if command -v apt-get >/dev/null; then "
        "DEBIAN_FRONTEND=noninteractive apt-get update; "
        f"DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades vitastor={version_q}; "
        "elif command -v dnf >/dev/null; then "
        f"dnf install -y vitastor-{version_q}; "
        "elif command -v yum >/dev/null; then "
        f"yum install -y vitastor-{version_q}; "
        "else echo 'Không hỗ trợ package manager trên node' >&2; exit 2; fi; "
        "systemctl restart vitastor.target; "
        "systemctl is-active --quiet vitastor.target"
    )


def upgrade(params: dict, progress: Callable[[str, str, str], None]) -> None:
    """Upgrade package-based nodes one at a time and verify cluster health after each node."""
    nodes = params["nodes"]
    ssh_user, ssh_key = params["ssh_user"], params["ssh_key_path"]
    target = params["target_version"]

    progress("preflight", "running", "Kiểm tra sức khoẻ cụm, SSH và phiên bản hiện tại")
    _assert_healthy(params)
    for host in nodes:
        _run(host, ssh_user, ssh_key, "command -v vitastor-cli >/dev/null; vitastor-cli --version")
    progress("preflight", "done", "Cụm HEALTHY và toàn bộ node sẵn sàng")

    total = len(nodes)
    for index, host in enumerate(nodes, 1):
        step = f"node-{index}"
        progress(step, "running", f"[{index}/{total}] Nâng cấp {host} lên {target}")
        _run(host, ssh_user, ssh_key, _upgrade_command(target))
        _assert_healthy(params)
        progress(step, "done", f"{host} đã nâng cấp; cụm vẫn HEALTHY")

    progress("verify", "running", "Xác minh phiên bản và sức khoẻ cuối cùng")
    versions = [_run(host, ssh_user, ssh_key, "vitastor-cli --version") for host in nodes]
    _assert_healthy(params)
    progress("verify", "done", "Upgrade hoàn tất: " + "; ".join(versions))


def _vitastor_cli(params: dict) -> str:
    args = ["vitastor-cli", "--no-color"]
    if params.get("config_path"):
        args += ["--config_path", params["config_path"]]
    else:
        args += ["--etcd_address", params["etcd_address"], "--etcd_prefix", params.get("etcd_prefix", "/vitastor")]
    command = " ".join(shlex.quote(str(arg)) for arg in args)
    mode = params.get("exec_mode", "none")
    if mode in {"docker", "podman"}:
        command = f"{mode} exec {shlex.quote(params['container_name'])} {command}"
    return command


def _qemu_uri(params: dict, image: str, skip_parents: bool = False) -> str:
    uri = f"vitastor:image={image}"
    if params.get("config_path"):
        uri += f":config_path={params['config_path']}"
    else:
        uri += (
            f":etcd_host={params['etcd_address']}"
            f":etcd_prefix={params.get('etcd_prefix', '/vitastor')}"
        )
    if skip_parents:
        uri += ":skip-parents=1"
    return shlex.quote(uri)


def backup(params: dict, progress: Callable[[str, str, str], None]) -> None:
    """Create native snapshots or export image/metadata backups on the management host."""
    host, user, key = params["management_host"], params["ssh_user"], params["ssh_key_path"]
    method = params["method"]
    cli = _vitastor_cli(params)

    progress("preflight", "running", "Kiểm tra cụm và công cụ backup")
    _assert_healthy(params)
    tools = "command -v vitastor-cli >/dev/null"
    if method in {"full_qcow2", "incremental_qcow2"}:
        tools += "; command -v qemu-img >/dev/null"
    elif method == "metadata_etcd":
        tools += "; command -v etcdctl >/dev/null"
    elif method == "metadata_antietcd":
        tools += "; command -v npm >/dev/null"
    if method != "snapshot":
        destination = shlex.quote(params["destination"])
        parent = shlex.quote(posixpath.dirname(params["destination"]) or "/")
        tools += f"; test -d {parent}; test -w {parent}; test ! -e {destination}"
    if method == "incremental_qcow2":
        tools += f"; test -s {shlex.quote(params['backing_file'])}"
    _run(host, user, key, tools)
    progress("preflight", "done", "Cụm HEALTHY và công cụ backup sẵn sàng")

    if method in {"snapshot", "full_qcow2", "incremental_qcow2", "raw"}:
        image, snapshot = params["image"], params["snapshot"]
        snapshot_image = f"{image}@{snapshot}"
        progress("snapshot", "running", f"Tạo snapshot nhất quán {snapshot_image}")
        _run(host, user, key, f"{cli} snap-create {shlex.quote(snapshot_image)}")
        progress("snapshot", "done", f"Đã tạo {snapshot_image}")
        if method == "snapshot":
            return
        destination = shlex.quote(params["destination"])
        progress("export", "running", f"Export backup tới {params['destination']}")
        if method == "full_qcow2":
            command = f"qemu-img convert -p -f raw {_qemu_uri(params, snapshot_image)} -O qcow2 {destination}"
        elif method == "incremental_qcow2":
            backing = shlex.quote(params["backing_file"])
            command = f"qemu-img convert -p -f raw {_qemu_uri(params, snapshot_image, True)} -O qcow2 -o cluster_size=4k -B {backing} {destination}"
        else:
            command = f"{cli} dd iimg={shlex.quote(snapshot_image)} of={destination} bs=1M status=progress"
        try:
            _run(host, user, key, command)
            progress("export", "done", "Đã ghi và đồng bộ file backup")
            progress("verify", "running", "Xác minh file backup")
            _run(host, user, key, f"test -s {shlex.quote(params['destination'])}")
            progress("verify", "done", "File backup tồn tại và có dữ liệu")
        except Exception:
            # The snapshot is temporary for exported backups. Best-effort
            # cleanup covers both export and post-export verification errors;
            # preserve the original error if cleanup also fails.
            progress("cleanup", "running", f"Dọn snapshot tạm {snapshot_image}")
            try:
                _run(host, user, key, f"{cli} rm {shlex.quote(snapshot_image)}")
            except Exception as cleanup_exc:
                progress("cleanup", "failed", f"Không dọn được snapshot tạm: {cleanup_exc}")
            else:
                progress("cleanup", "done", f"Đã dọn snapshot tạm {snapshot_image}")
            raise
    elif method == "metadata_etcd":
        destination = shlex.quote(params["destination"])
        endpoint = shlex.quote(params["etcd_address"].split(",")[0])
        progress("export", "running", "Chụp snapshot toàn bộ metadata etcd")
        _run(host, user, key, f"ETCDCTL_API=3 etcdctl --endpoints={endpoint} snapshot save {destination}")
        progress("export", "done", "Đã lưu snapshot metadata etcd")
    elif method == "metadata_antietcd":
        destination = shlex.quote(params["destination"])
        endpoint = shlex.quote(params["etcd_address"].split(",")[0])
        progress("export", "running", "Dump metadata antietcd")
        _run(host, user, key, f"npm exec --yes anticli -- -e {endpoint} get --prefix '' --no-temp > {destination}")
        progress("export", "done", "Đã lưu dump metadata antietcd")
    else:
        raise VitastorOperationError(f"Phương thức backup không được hỗ trợ: {method}")

    if method not in {"full_qcow2", "incremental_qcow2", "raw"}:
        progress("verify", "running", "Xác minh file backup")
        _run(host, user, key, f"test -s {shlex.quote(params['destination'])}")
        progress("verify", "done", "File backup tồn tại và có dữ liệu")
