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
from vitastor.recovery_ai import summarize_deploy_recovery

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
    return (
        f"install -d -m 0755 /etc/vitastor && echo {shlex.quote(encoded)} | "
        "base64 -d > /etc/vitastor/vitastor.conf && "
        "chown vitastor:vitastor /etc/vitastor/vitastor.conf && "
        "chmod 0640 /etc/vitastor/vitastor.conf"
    )


def _deploy_config(params: dict) -> dict:
    """Build the URL-shaped config required by make-etcd and Vitastor CLI."""
    monitors = [node["host"] for node in params["nodes"] if "mon" in node["roles"]]
    return {
        "etcd_address": [f"http://{host}:2379" for host in monitors],
        "etcd_prefix": params["etcd_prefix"],
        "osd_network": params["osd_network"],
    }


def _config_matches_command(config: dict) -> str:
    """Read-only check for the fields owned by the deploy workflow."""
    encoded = base64.b64encode(json.dumps(config, separators=(",", ":")).encode()).decode()
    return (
        "set -eu; test -s /etc/vitastor/vitastor.conf; "
        "python3 -c "
        + shlex.quote(
            "import base64,json,sys; "
            "actual=json.load(open('/etc/vitastor/vitastor.conf')); "
            "expected=json.loads(base64.b64decode(sys.argv[1])); "
            "sys.exit(0 if all(actual.get(k)==v for k,v in expected.items()) else 1)"
        )
        + " "
        + shlex.quote(encoded)
    )


def _preflight_command(disks: list[str]) -> str:
    """Device safety gate that must run before anything can write to a disk.

    `vitastor-disk prepare` wipes its target. A path that is not a block
    device, or that is mounted, means the name no longer points at the disk
    the operator chose — device names shift across reboots, and a disk can be
    in use for something else entirely — so the whole step is refused.
    """
    checks = ["test -r /etc/os-release"]
    for disk in disks:
        q = shlex.quote(disk)
        checks.append(f"test -b {q} && test -z \"$(lsblk -no MOUNTPOINT {q} | tr -d ' ')\"")
    return "set -eu; " + "; ".join(checks) + "; echo preflight-ok"


def _monitor_setup_command(*, reuse_existing_etcd: bool) -> str:
    """Bring up etcd and the monitor.

    `reuse_existing_etcd` belongs to `resume_deploy` ALONE: there, an already
    generated etcd.conf is the state a failed deploy left behind and rerunning
    make-etcd would undo it. A fresh `deploy` must never reuse it — `delete()`
    is not the only way a cluster goes away, so leftover etcd.conf on a node
    can still carry a previous cluster's membership and prefix while the new
    vitastor.conf points somewhere else entirely.
    """
    if reuse_existing_etcd:
        bootstrap = (
            "if test -s /etc/vitastor/etcd.conf && "
            "test -f /etc/systemd/system/vitastor-etcd.service; then "
            "systemctl daemon-reload; "
            "else /usr/lib/vitastor/mon/make-etcd --copy no; fi; "
        )
    else:
        bootstrap = "/usr/lib/vitastor/mon/make-etcd --copy no; "
    return (
        "set -eu; "
        + bootstrap
        + "systemctl enable --now vitastor-etcd vitastor-mon; "
        "test \"$(systemctl show -p ActiveState --value vitastor-etcd)\" = active; "
        "test \"$(systemctl show -p SubState --value vitastor-etcd)\" = running; "
        "test \"$(systemctl show -p ActiveState --value vitastor-mon)\" = active; "
        "test \"$(systemctl show -p SubState --value vitastor-mon)\" = running"
    )


def _package_ready_command(version: str = "") -> str:
    """Verify binaries and, when requested, the exact installed release."""
    version = str(version or "").strip()
    version_check = ""
    if version:
        version_check = (
            " vitastor-disk --help 2>&1 | head -1 | "
            + "grep -F -- "
            + shlex.quote(" " + version)
            + " >/dev/null;"
        )
    return (
        "set -eu; command -v vitastor-cli >/dev/null; "
        "test -f /usr/lib/vitastor/mon/make-etcd;"
        + version_check
        + " echo packages-ready"
    )


def _osd_reconcile_command(disk: str) -> str:
    """Skip prepare only when the target already has a valid Vitastor SB."""
    q = shlex.quote(disk)
    return (
        "set -eu; found=0; "
        f"for candidate in {q} $(lsblk -nrpo NAME {q} 2>/dev/null || true); do "
        "if vitastor-disk read-sb --force \"$candidate\" >/dev/null 2>&1; then "
        "echo \"OSD superblock found on $candidate; skipping prepare\"; found=1; break; fi; "
        "done; "
        "if test \"$found\" = 0; then vitastor-disk prepare --dry-run " + q + "; "
        "vitastor-disk prepare " + q + "; fi; "
        "systemctl start vitastor.target; "
        "test \"$(systemctl show -p ActiveState --value vitastor.target)\" = active; "
        "test \"$(systemctl show -p SubState --value vitastor.target)\" = active; "
        "systemctl list-units --type=service --state=running --no-legend 'vitastor-osd@*.service' | grep -q ."
    )


def _inspection_line(host: str, label: str, result: str) -> str:
    return f"{host}: {label} — {result.strip()[-500:]}"


def _recovery_snapshot_command(nodes: list[dict]) -> str:
    """Collect a bounded, read-only state snapshot before any resume writes."""
    disks = [disk for node in nodes for disk in node.get("disks", [])]
    disk_checks = []
    for disk in disks:
        q = shlex.quote(disk)
        disk_checks.append(
            f"found=0; for candidate in {q} $(lsblk -nrpo NAME {q} 2>/dev/null || true); do "
            f"if vitastor-disk read-sb --force \"$candidate\" >/dev/null 2>&1; then "
            f"echo osd-superblock=\"$candidate\"; found=1; break; fi; done; "
            "test \"$found\" = 1 || echo osd-superblock=none"
        )
    return (
        "set +e; "
        "if command -v vitastor-cli >/dev/null && test -f /usr/lib/vitastor/mon/make-etcd; "
        "then echo packages=ready; else echo packages=missing; fi; "
        "if test -s /etc/vitastor/vitastor.conf; then echo config=present; else echo config=missing; fi; "
        "systemctl is-active --quiet vitastor-etcd && echo etcd=active || echo etcd=inactive; "
        "systemctl is-active --quiet vitastor-mon && echo monitor=active || echo monitor=inactive; "
        + ("; ".join(disk_checks) if disk_checks else "echo osd-superblock=not-applicable")
    )


def resume_deploy(params: dict, progress: Callable[[str, str, str], None]) -> None:
    """Reconcile a failed deploy and continue only from verified state.

    Every check is idempotent and read-only until the exact missing step is
    identified. OSD preparation is the sole write-sensitive step and is
    skipped when its superblock is already readable.
    """
    nodes = params["nodes"]
    ssh_user, ssh_key = params["ssh_user"], params["ssh_key_path"]
    monitors = [node["host"] for node in nodes if "mon" in node["roles"]]
    config = _deploy_config(params)
    inspection: list[str] = []
    prior_progress = params.get("resume_progress") or []
    if isinstance(prior_progress, list):
        completed = [
            str(item.get("id"))
            for item in prior_progress
            if isinstance(item, dict) and item.get("status") == "done"
        ]
        if completed:
            inspection.append("Lịch sử operation ghi nhận đã xong: " + ", ".join(completed))

    progress("reconcile", "running", "Đối chiếu trạng thái deploy hiện tại và nhờ AI phân tích lỗi")
    for node in nodes:
        snapshot = _run(node["host"], ssh_user, ssh_key, _recovery_snapshot_command([node]))
        inspection.append(_inspection_line(node["host"], "snapshot chỉ đọc", snapshot))
    ai_summary = summarize_deploy_recovery(
        str(params.get("resume_error") or "Không có lỗi được lưu"),
        "\n".join(inspection),
    )
    progress("recovery-ai", "done", "AI phân tích trước khi tiếp tục: " + ai_summary)
    for node in nodes:
        host = node["host"]
        try:
            output = _run(host, ssh_user, ssh_key, _preflight_command(node.get("disks", [])))
            inspection.append(_inspection_line(host, "preflight", "đã hoàn tất; " + output))
        except Exception as exc:
            inspection.append(_inspection_line(host, "preflight", f"chưa đạt: {exc}"))
            raise

    for node in nodes:
        host = node["host"]
        try:
            output = _run(host, ssh_user, ssh_key, _package_ready_command(params.get("version", "")))
            inspection.append(_inspection_line(host, "packages", "đã hoàn tất; " + output))
        except Exception:
            if not params.get("install_packages"):
                raise
            output = _run(host, ssh_user, ssh_key, _install_command(params.get("version", "")))
            inspection.append(_inspection_line(host, "packages", "đã cài tiếp; " + output))
    progress("packages", "done", "Package đã được xác minh; chỉ cài bổ sung node còn thiếu")

    for node in nodes:
        host = node["host"]
        try:
            output = _run(host, ssh_user, ssh_key, _config_matches_command(config))
            inspection.append(_inspection_line(host, "config", "đã khớp; " + output))
        except Exception:
            output = _run(host, ssh_user, ssh_key, "set -eu; " + _config_command(config))
            inspection.append(_inspection_line(host, "config", "đã ghi bổ sung; " + output))
    progress("config", "done", "Cấu hình đã được đối chiếu và bổ sung khi cần")

    for host in monitors:
        output = _run(host, ssh_user, ssh_key, _monitor_setup_command(reuse_existing_etcd=True))
        inspection.append(_inspection_line(host, "monitors", "đã hoạt động; " + output))
    progress("monitors", "done", "Etcd và monitor đã được đối chiếu/khởi động tiếp")

    for node in nodes:
        for disk in node.get("disks", []):
            output = _run(node["host"], ssh_user, ssh_key, _osd_reconcile_command(disk))
            state = "OSD đã có superblock, bỏ qua prepare" if "superblock found" in output else "OSD được prepare tiếp"
            inspection.append(_inspection_line(node["host"], f"osd {disk}", state))
    progress("osds", "done", "OSD đã được kiểm tra; chỉ prepare thiết bị chưa có superblock")

    output = _run(monitors[0], ssh_user, ssh_key, "vitastor-cli --json --no-color status")
    inspection.append(_inspection_line(monitors[0], "verify", "cluster status đã phản hồi; " + output))
    progress("verify", "done", "Cụm phản hồi vitastor-cli status; resume hoàn tất")


def _install_command(version: str) -> str:
    """Configure the official repo and install the requested Vitastor version."""
    version = str(version or "").strip()
    if version:
        version_q = shlex.quote(version)
        apt_package = f"vitastor={version_q}"
        rpm_package = f"vitastor-{version_q}"
    else:
        apt_package = rpm_package = "vitastor"
    return (
        "set -eu; test -r /etc/os-release; . /etc/os-release; "
        "if command -v apt-get >/dev/null; then "
        "case \"${ID}:${VERSION_ID}\" in "
        "debian:13*) vita_suite=trixie;; debian:12*) vita_suite=bookworm;; "
        "debian:11*) vita_suite=bullseye;; debian:10*) vita_suite=buster;; "
        "ubuntu:22.04) vita_suite=jammy;; ubuntu:24.04) vita_suite=noble;; "
        "ubuntu:26.04) vita_suite=resolute;; "
        "*) echo \"Unsupported Debian/Ubuntu release: ${ID} ${VERSION_ID}\" >&2; exit 2;; esac; "
        "install -d -m 0755 /etc/apt/trusted.gpg.d /etc/apt/sources.list.d; "
        "if command -v wget >/dev/null; then wget -q https://vitastor.io/debian/pubkey.gpg -O /etc/apt/trusted.gpg.d/vitastor.gpg; "
        "elif command -v curl >/dev/null; then curl -fsSL https://vitastor.io/debian/pubkey.gpg -o /etc/apt/trusted.gpg.d/vitastor.gpg; "
        "else echo 'Cần wget hoặc curl để tải signing key Vitastor' >&2; exit 2; fi; "
        "printf 'deb https://vitastor.io/debian %s main\\n' \"$vita_suite\" > /etc/apt/sources.list.d/vitastor.list; "
        "DEBIAN_FRONTEND=noninteractive apt-get update; "
        f"DEBIAN_FRONTEND=noninteractive apt-get install -y {apt_package} etcd lp-solve; "
        "elif command -v dnf >/dev/null || command -v yum >/dev/null; then "
        "package_manager=dnf; command -v dnf >/dev/null || package_manager=yum; "
        "vita_major=\"${VERSION_ID%%.*}\"; "
        "case \"$vita_major\" in "
        "7) vita_release=https://vitastor.io/rpms/centos/7/vitastor-release.rpm; "
        "vita_extra=centos-release-scl; vita_kernel=https://www.elrepo.org/elrepo-release-7.el7.elrepo.noarch.rpm;; "
        "8) vita_release=https://vitastor.io/rpms/centos/8/vitastor-release.rpm; "
        "vita_extra=centos-release-advanced-virtualization; vita_kernel=https://www.elrepo.org/elrepo-release-8.el8.elrepo.noarch.rpm;; "
        "9) vita_release=https://vitastor.io/rpms/centos/9/vitastor-release.rpm; "
        "vita_extra=; vita_kernel=;; "
        "10) vita_release=https://vitastor.io/rpms/centos/10/vitastor-release.rpm; "
        "vita_extra=; vita_kernel=;; "
        "*) echo \"Unsupported RHEL-compatible release: ${ID} ${VERSION_ID}\" >&2; exit 2;; esac; "
        "$package_manager install -y \"$vita_release\" epel-release; "
        "if test -n \"$vita_extra\"; then $package_manager install -y \"$vita_extra\"; fi; "
        "if test -n \"$vita_kernel\"; then $package_manager install -y \"$vita_kernel\"; fi; "
        f"$package_manager install -y {rpm_package} etcd lpsolve; "
        "else echo 'Không hỗ trợ package manager trên node' >&2; exit 2; fi"
    )


def deploy(params: dict, progress: Callable[[str, str, str], None]) -> None:
    nodes = params["nodes"]
    ssh_user, ssh_key = params["ssh_user"], params["ssh_key_path"]
    monitors = [n["host"] for n in nodes if "mon" in n["roles"]]
    config = _deploy_config(params)

    progress("preflight", "running", "Kiểm tra SSH và thiết bị")
    for node in nodes:
        _run(node["host"], ssh_user, ssh_key, _preflight_command(node.get("disks", [])))
    progress("preflight", "done", "Kiểm tra an toàn hoàn tất")

    progress("packages", "running", "Cấu hình repository chính thức và kiểm tra/cài gói Vitastor, Etcd")
    for node in nodes:
        if params.get("install_packages"):
            _run(node["host"], ssh_user, ssh_key, _install_command(params.get("version", "")))
        _run(node["host"], ssh_user, ssh_key, _package_ready_command(params.get("version", "")))
    progress("packages", "done", "Binary Vitastor sẵn sàng")

    progress("config", "running", "Ghi cấu hình Vitastor đồng nhất")
    for node in nodes:
        _run(node["host"], ssh_user, ssh_key, "set -eu; " + _config_command(config))
    progress("config", "done", "Đã ghi /etc/vitastor/vitastor.conf")

    progress("monitors", "running", "Khởi tạo Etcd và monitor")
    for host in monitors:
        # ``make-etcd`` asks an interactive copy question by default.  The
        # dashboard already invokes it once per monitor, so nested copying is
        # unnecessary and would block a non-interactive SSH session forever.
        _run(host, ssh_user, ssh_key, _monitor_setup_command(reuse_existing_etcd=False))
    progress("monitors", "done", "Etcd và monitor đã khởi động")

    progress("osds", "running", "Chuẩn bị thiết bị OSD — thao tác ghi dữ liệu")
    for node in nodes:
        disks = node.get("disks", [])
        if not disks:
            continue
        quoted = " ".join(shlex.quote(disk) for disk in disks)
        _run(
            node["host"], ssh_user, ssh_key,
            f"set -eu; vitastor-disk prepare --dry-run {quoted}; vitastor-disk prepare {quoted}; "
            "systemctl start vitastor.target; "
            "test \"$(systemctl show -p ActiveState --value vitastor.target)\" = active; "
            "test \"$(systemctl show -p SubState --value vitastor.target)\" = active; "
            "systemctl list-units --type=service --state=running --no-legend 'vitastor-osd@*.service' | grep -q .",
        )
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
            # etcd.conf mang membership + prefix của CỤM NÀY; để lại thì node
            # còn một mảnh cấu hình không khớp với bất kỳ cụm nào đang tồn tại.
            "done; rm -f /etc/vitastor/vitastor.conf /etc/vitastor/etcd.conf",
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


def _vitastor_cli(params: dict, *, json_output: bool = False) -> str:
    args = ["vitastor-cli"]
    if json_output:
        args.append("--json")
    args.append("--no-color")
    if params.get("config_path"):
        args += ["--config_path", params["config_path"]]
    else:
        args += ["--etcd_address", params["etcd_address"], "--etcd_prefix", params.get("etcd_prefix", "/vitastor")]
    command = " ".join(shlex.quote(str(arg)) for arg in args)
    mode = params.get("exec_mode", "none")
    if mode in {"docker", "podman"}:
        command = f"{mode} exec {shlex.quote(params['container_name'])} {command}"
    return command


def _metadata_bundle_command(params: dict) -> str:
    """Build an atomic, timestamped cluster-metadata backup on the remote host.

    The etcd snapshot is the authoritative Vitastor metadata backup.  The
    accompanying CLI exports and configuration make the backup auditable and
    reduce recovery time without touching any OSD device or block data.
    """
    base = shlex.quote(params["destination"])
    endpoint = shlex.quote(params["etcd_address"])
    config_path = shlex.quote(params.get("config_path") or "/etc/vitastor/vitastor.conf")
    cluster_name = params.get("cluster_name")
    cluster_name_line = (
        f"  {shlex.quote('cluster_name=' + str(cluster_name))} "
        if cluster_name else ""
    )
    cli = _vitastor_cli(params, json_output=True)
    return (
        "set -eu; "
        f"base={base}; "
        "stamp=$(date -u +%Y%m%dT%H%M%SZ); "
        "final=\"$base/${stamp}-$$\"; "
        "test ! -e \"$final\"; "
        "tmp=\"$base/.metadata-$stamp-$$\"; "
        "umask 077; mkdir \"$tmp\"; "
        "trap 'rm -rf -- \"$tmp\"' EXIT; "
        f"ETCDCTL_API=3 etcdctl --endpoints={endpoint} snapshot save \"$tmp/etcd-snapshot.db\"; "
        "ETCDCTL_API=3 etcdctl snapshot status \"$tmp/etcd-snapshot.db\" -w json > \"$tmp/etcd-snapshot-status.json\"; "
        f"{cli} status > \"$tmp/status.json\"; "
        f"{cli} df > \"$tmp/df.json\"; "
        f"{cli} ls-pools --detail > \"$tmp/pools.json\"; "
        f"{cli} ls -l > \"$tmp/images.json\"; "
        f"{cli} ls-osd -l > \"$tmp/osds.json\"; "
        f"{_vitastor_cli(params)} osd-tree > \"$tmp/osd-tree.txt\"; "
        f"{cli} ls-user > \"$tmp/users.json\" 2>/dev/null || true; "
        f"test -r {config_path}; cp -- {config_path} \"$tmp/vitastor.conf\"; "
        "chmod 0600 \"$tmp/vitastor.conf\"; "
        "printf '%s\\n' "
        "  'backup_type=vitastor-cluster-metadata' "
        f"{cluster_name_line}"
        "  \"created_at=$stamp\" "
        f"  {shlex.quote('etcd_endpoints=' + params['etcd_address'])} "
        f"  {shlex.quote('etcd_prefix=' + params.get('etcd_prefix', '/vitastor'))} "
        "  'restore_scope=metadata-only; keep existing OSD disks intact' "
        "> \"$tmp/backup-info.txt\"; "
        "sha256sum \"$tmp\"/* > \"$tmp/SHA256SUMS\"; "
        "(cd \"$tmp\" && sha256sum -c SHA256SUMS); "
        "test -s \"$tmp/etcd-snapshot.db\"; "
        "mv \"$tmp\" \"$final\"; "
        "trap - EXIT; "
        "printf '%s\\n' \"$final\""
    )


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


def backup(params: dict, progress: Callable[[str, str, str], None]) -> str | None:
    """Create native snapshots or export image/metadata backups on the management host."""
    host, user, key = params["management_host"], params["ssh_user"], params["ssh_key_path"]
    method = params["method"]
    cli = _vitastor_cli(params)

    progress("preflight", "running", "Kiểm tra cụm và công cụ backup")
    _assert_healthy(params)
    tools = "set -eu; command -v vitastor-cli >/dev/null"
    if method in {"full_qcow2", "incremental_qcow2"}:
        tools += "; command -v qemu-img >/dev/null"
    elif method in {"metadata_etcd", "metadata_cluster"}:
        tools += "; command -v etcdctl >/dev/null"
    elif method == "metadata_antietcd":
        tools += "; command -v npm >/dev/null"
    if method != "snapshot":
        destination = shlex.quote(params["destination"])
        parent = shlex.quote(posixpath.dirname(params["destination"]) or "/")
        if method == "metadata_cluster":
            tools += (
                f"; existing_parent={destination}; "
                f"if test ! -d {destination}; then existing_parent={parent}; "
                "while [ \"$existing_parent\" != / ] && [ ! -d \"$existing_parent\" ]; do "
                "existing_parent=$(dirname \"$existing_parent\"); done; fi; "
                "test \"$existing_parent\" != /; test -d \"$existing_parent\"; "
                "test -w \"$existing_parent\"; "
                f"mkdir -p {destination}; test -d {destination}; test -w {destination}"
            )
        else:
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
    elif method == "metadata_cluster":
        progress("export", "running", "Chụp snapshot etcd và export metadata cụm")
        bundle_output = _run(host, user, key, _metadata_bundle_command(params))
        progress("export", "done", "Đã lưu snapshot etcd, cấu hình và inventory cụm")
        progress("verify", "running", "Xác minh checksum toàn bộ metadata backup")
        progress("verify", "done", "Metadata backup đã được xác minh và đóng gói atomic")
        return bundle_output.splitlines()[-1] if bundle_output.strip() else None
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
