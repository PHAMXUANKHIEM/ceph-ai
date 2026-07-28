"""Multi-phase orchestrator for the "Dựng cụm Ceph tự động" feature (Story 8.1).

Deliberately a SEPARATE execution path from
worker/llm/router_client.py::_execute_approved_action's generic per-host
loop — that loop fires ONE command family identically at every host with no
cross-host ordering and no wait step, which cannot express "MON before
MGR/OSD, wait for quorum first" (Story 8.2's ceph-deploy phases need the
wait; this module's framework is shared by all 3 install methods so that
capability exists from day one). Plugged into the existing approved-Action
pipeline via one early-branch dispatch in `_execute_approved_action` — see
that function's docstring.

AD-3: this module lives under worker/executor/, Worker-process-only, same
as ssh_executor.py/commands.py — it is never imported by dashboard/.
"""

import base64
import json
import logging
import os
import shlex
import time
import uuid
from datetime import datetime

from shared import env_config
from shared.ceph_releases import codename_for_version, repo_path_version
from worker.executor.commands import _package_manager_branch
from worker.executor.ssh_executor import ExecutorError, execute_command
from worker.policy.gate import VALID_CLUSTER_DEPLOY_ACTION_IDS

# Bounded MON-quorum poll (AC #2 item 6: a Python-level retry loop, not a
# bash `while`, so it's mockable in tests — see test_cluster_deploy.py).
# Overridable per-run via action_params["quorum_timeout_seconds"] (Task 1's
# "default 180s, configurable").
_QUORUM_POLL_INTERVAL_SECONDS = 5
_QUORUM_DEFAULT_TIMEOUT_SECONDS = 180

# Paths written on every ceph-deploy-method node (mon/mgr/osd alike) — same
# conventional locations the official Ceph manual-deployment docs use, so a
# node built this way looks like any other traditional package install to
# later tooling (systemctl unit names, `ceph-volume`'s own default keyring
# lookup, etc).
_REMOTE_CEPH_CONF_PATH = "/etc/ceph/ceph.conf"
_REMOTE_MON_KEYRING_PATH = "/tmp/ceph-aiops-mon.keyring"
_REMOTE_MONMAP_PATH = "/tmp/ceph-aiops.monmap"
_REMOTE_ADMIN_KEYRING_PATH = "/etc/ceph/ceph.client.admin.keyring"
_REMOTE_BOOTSTRAP_OSD_KEYRING_PATH = "/var/lib/ceph/bootstrap-osd/ceph.keyring"

_ROLE_TO_PACKAGE = {"mon": "ceph-mon", "mgr": "ceph-mgr", "osd": "ceph-osd"}

logger = logging.getLogger(__name__)

# Re-exported from the policy layer (single source of truth is
# action_policy.yaml's `cluster_deploy_action_ids:`, loaded by
# worker/policy/gate.py) — this is the frozenset
# worker/llm/router_client.py::_execute_approved_action checks membership
# against to decide whether to delegate here at all.
CLUSTER_DEPLOY_ACTION_IDS = VALID_CLUSTER_DEPLOY_ACTION_IDS

_SUPPORTED_OS_FAMILIES = {
    "rhel": "rpm",
    "centos": "rpm",
    "rocky": "rpm",
    "almalinux": "rpm",
    "fedora": "rpm",
    "debian": "deb",
    "ubuntu": "deb",
}


class DeployPhaseError(Exception):
    """Raised by a phase function with a specific, human-readable reason —
    caught by `run()`, which marks that phase (and the whole deploy)
    `failed` and stops before any later phase runs. Distinct from
    `ExecutorError` (an SSH-level failure) the same way this codebase
    already distinguishes command-execution errors from higher-level
    orchestration decisions elsewhere."""


def _node_ips_with_role(nodes: list[dict], role: str) -> list[str]:
    return [n["ip"] for n in nodes if role in (n.get("roles") or [])]


def _first_mon_ip(nodes: list[dict]) -> str:
    mon_ips = _node_ips_with_role(nodes, "mon")
    if not mon_ips:
        raise DeployPhaseError("Không có node MON nào trong cấu hình")
    return mon_ips[0]


# --- Phase 1: SSH check + system check (shared byte-for-byte by every
# install method — Story 8.2/8.3 reuse this function unchanged) -----------
#
# This is the single most safety-critical phase in the whole feature: the
# OSD-disk check below is read-only (test -b / blkid / lsblk — never
# wipefs/dd/mkfs) and runs BEFORE any node is touched by a later phase, so
# a bug here that skipped or weakened the check would be the most
# consequential bug in the whole story (see the story file's Dev Notes).


def _detect_os_family(host: str) -> str:
    """Reads /etc/os-release on `host`, returns a human-readable label
    (e.g. "Rocky Linux 9.3"). Raises DeployPhaseError if the host is
    unreachable, the file can't be read, or the distro ID isn't one of the
    RPM/Debian families this deploy engine knows how to handle."""
    try:
        output = execute_command(host, "cat /etc/os-release")
    except ExecutorError as exc:
        raise DeployPhaseError(f"{host}: không đọc được /etc/os-release: {exc}") from exc

    fields: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip().strip('"')

    os_id = (fields.get("ID") or "").lower()
    if os_id not in _SUPPORTED_OS_FAMILIES:
        raise DeployPhaseError(
            f"{host}: hệ điều hành '{os_id or '?'}' không được hỗ trợ (chỉ hỗ trợ RHEL/CentOS/"
            f"Rocky Linux/AlmaLinux/Fedora hoặc Debian/Ubuntu)"
        )
    pretty_name = fields.get("PRETTY_NAME") or os_id
    version = fields.get("VERSION_ID", "")
    return f"{pretty_name} {version}".strip()


def _check_osd_disk_safe(host: str, osd_disk: str | None) -> None:
    """Read-only pre-flight check on `osd_disk` at `host` — must exist as a
    block device AND be genuinely empty (no filesystem/partition-table/LVM
    signature, not mounted) before any later phase is allowed to touch it.
    Raises DeployPhaseError with a specific reason on any violation; never
    runs a destructive command."""
    if not osd_disk:
        raise DeployPhaseError(f"{host}: chưa cấu hình đĩa OSD (osd_disk)")
    quoted = shlex.quote(osd_disk)

    try:
        execute_command(host, f"test -b {quoted}")
    except ExecutorError as exc:
        raise DeployPhaseError(
            f"{host}: {osd_disk} không tồn tại hoặc không phải là block device"
        ) from exc

    # blkid exits non-zero (commonly 2) precisely when the device has NO
    # recognized signature — the SAFE case — so `; true` keeps the overall
    # SSH command's exit status at 0 regardless; only stdout is examined.
    try:
        blkid_output = execute_command(host, f"blkid {quoted} 2>/dev/null; true").strip()
    except ExecutorError as exc:
        raise DeployPhaseError(f"{host}: không kiểm tra được blkid trên {osd_disk}: {exc}") from exc
    if blkid_output:
        raise DeployPhaseError(
            f"Đĩa {osd_disk} trên node {host} đã có dữ liệu ({blkid_output}) — dừng lại để "
            f"tránh mất dữ liệu"
        )

    try:
        mount_output = execute_command(host, f"lsblk -no MOUNTPOINT {quoted} 2>/dev/null; true").strip()
    except ExecutorError as exc:
        raise DeployPhaseError(f"{host}: không kiểm tra được điểm mount trên {osd_disk}: {exc}") from exc
    if mount_output:
        raise DeployPhaseError(f"Đĩa {osd_disk} trên node {host} đang được mount — dừng lại")


def _phase_ssh_check(nodes: list[dict], action_params: dict, on_host_update) -> None:
    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))

    hostnames: dict[str, str] = {}

    for i, node in enumerate(nodes):
        host = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))

        try:
            execute_command(host, "true")
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"Không kết nối được SSH tới {host}: {exc}") from exc

        try:
            os_label = _detect_os_family(host)
        except DeployPhaseError:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise

        if "osd" in (node.get("roles") or []):
            try:
                _check_osd_disk_safe(host, node.get("osd_disk"))
            except DeployPhaseError:
                host_status[i]["status"] = "failed"
                on_host_update(list(host_status))
                raise

        try:
            hostname_output = execute_command(host, "hostname -f 2>/dev/null || hostname").strip()
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{host}: không lấy được hostname: {exc}") from exc
        hostnames[host] = hostname_output or host

        host_status[i]["status"] = "done"
        host_status[i]["message"] = os_label
        on_host_update(list(host_status))

    # Scratch state for later phases within THIS run() call only — never
    # persisted back to the DB (action_params here is the in-memory dict
    # run() was called with, not the Action row's JSON column).
    action_params["_node_hostnames"] = hostnames


# --- cephadm-specific phases (Story 8.1 only executes this method) -------


def _phase_cephadm_bootstrap(nodes: list[dict], action_params: dict, on_host_update) -> None:
    first_mon = _first_mon_ip(nodes)
    version = action_params.get("version", "")
    codename = codename_for_version(version)
    if codename is None:
        raise DeployPhaseError(f"Không tìm thấy mã tên release Ceph cho phiên bản {version!r}")

    host_status = [{"host": first_mon, "status": "running"}]
    on_host_update(list(host_status))

    # The downloaded `cephadm` script itself is a `#!/usr/bin/python3` file —
    # a bare/minimal node (verified live, 2026-07-26: a fresh node had no
    # /usr/bin/python3 at all) fails with exit 126 "bad interpreter" on the
    # VERY FIRST invocation (`add-repo`), before cephadm's own `install`
    # subcommand ever gets a chance to pull in its real dependencies. python3
    # is ensured by the `dependencies` phase now (runs on EVERY node, before
    # this one) — originally this phase ran its own separate python3 check
    # here, but that only ever covered first_mon: `ceph orch host add` later
    # failed with "no python3 in ..." on the SECOND node added, because
    # cephadm's per-host management agent is itself a python3 script the
    # orchestrator runs via SSH on every host it manages, not just first_mon
    # (verified live, 2026-07-26).
    # This bare `cephadm install` (no package args) only needs the
    # `cephadm` package itself, which has worked fine via cephadm's own
    # `add-repo` in every live run so far — unlike `ceph-common` below,
    # there's no evidence this part is broken, so it's left as cephadm's
    # own responsibility. Switched `--release {codename}` to
    # `--version {version}` anyway (matching the fix already made for
    # `_phase_ceph_deploy_repo`) since the codename's rolling alias is a
    # real, verified bug class in general — harmless here even though it
    # didn't turn out to be what broke ceph-common (see that step below).
    # `codename_for_version` above is still called purely to validate the
    # version is recognized before touching any node — the curl fetch of
    # the cephadm SCRIPT ITSELF (one static file, no repo-metadata
    # resolution involved) is unaffected and keeps using the release name.
    install_cephadm = (
        "command -v cephadm >/dev/null 2>&1 || "
        f"(curl -fsSL https://download.ceph.com/rpm-{codename}/el9/noarch/cephadm "
        "-o /usr/local/bin/cephadm && chmod +x /usr/local/bin/cephadm && "
        f"/usr/local/bin/cephadm add-repo --version {version} && /usr/local/bin/cephadm install)"
    )
    # A MON container left running from an EARLIER, partially-failed deploy
    # attempt on this same node still holds the MSGR v2 port — verified
    # live, 2026-07-26: "Cannot bind to IP ... port 3300: Address already in
    # use", right after --allow-overwrite fixed the ceph.conf-exists error.
    # `cephadm rm-cluster --force` is the documented full teardown for a
    # previous cephadm cluster on a node (stops/removes every daemon +
    # /etc/ceph + /var/lib/ceph/<fsid> for that fsid) — grep/sed rather than
    # a JSON parser since `cephadm ls`'s fsid field is a simple flat string,
    # no nested structure to justify the extra dependency. Deliberately
    # does NOT pass --zap-osds — this must never touch the OSD block device
    # itself, only cephadm's own container/systemd state (`command -v
    # cephadm` guards a truly first-time node, where `cephadm ls` would
    # otherwise fail as "no such command yet").
    cleanup_previous_attempt = (
        "command -v cephadm >/dev/null 2>&1 && "
        "for fsid in $(cephadm ls 2>/dev/null | grep -o '\"fsid\": *\"[^\"]*\"' | "
        "sed -E 's/.*\"([a-f0-9-]+)\"$/\\1/' | sort -u); do "
        "cephadm rm-cluster --fsid \"$fsid\" --force; done; "
        "true"
    )
    # --allow-fqdn-hostname: cephadm bootstrap otherwise hard-refuses to
    # proceed (exit 1) whenever the node's `hostname` command returns an
    # FQDN (e.g. "khiempx-ceph1.novalocal", common on cloud/OpenStack-
    # provisioned VMs) — verified live, 2026-07-26. Unconditional rather
    # than detected-and-conditional: harmless when the hostname is already
    # short, so there's no reason to special-case it.
    # --allow-overwrite: cephadm bootstrap also hard-refuses (exit 1) if
    # /etc/ceph/ceph.conf already exists — verified live, 2026-07-26, left
    # behind on this node by an earlier deploy attempt that got this far
    # before failing on a LATER phase (e.g. this session's own FQDN-hostname
    # and time-sync fixes). Safe to pass unconditionally here specifically
    # because this whole feature only ever runs against nodes the operator
    # is deliberately bootstrapping a BRAND-NEW cluster on (the ssh_check
    # phase's read-only OSD-disk check already enforces "must be empty" —
    # a node with a real, already-running production cluster wouldn't pass
    # that check in the first place).
    bootstrap = (
        f"cephadm bootstrap --mon-ip {shlex.quote(first_mon)} --skip-monitoring-stack "
        "--allow-fqdn-hostname --allow-overwrite"
    )
    # `cephadm bootstrap` on its own leaves the `ceph` CLI reachable only
    # via the containerized `cephadm shell` wrapper, not directly on PATH —
    # every later phase in this method (orch_host_add/orch_apply_mgr/
    # orch_apply_osd/verify) calls `ceph ...` directly on `first_mon`
    # (verified live, 2026-07-26: "ceph: command not found", exit 127,
    # right after a successful bootstrap). Deliberately does NOT use
    # `cephadm install ceph-common` for this — verified live (2026-07-26)
    # that it left ceph-common unfindable via yum twice in a row, even after
    # switching cephadm's own `add-repo` from `--release` to `--version`
    # above; rather than keep guessing at cephadm's internal repo-URL logic,
    # this uses OUR OWN already-verified repo command
    # (`_build_ceph_package_repo_command`, same one `_phase_ceph_deploy_repo`
    # uses) plus a plain named-package install.
    ensure_ceph_repo = _build_ceph_package_repo_command(version)
    ensure_ceph_common = _package_manager_branch(
        {
            "apt": "command -v ceph >/dev/null 2>&1 || apt-get install -y ceph-common",
            "rpm": "command -v ceph >/dev/null 2>&1 || (dnf install -y ceph-common || yum install -y ceph-common)",
        }
    )

    try:
        execute_command(first_mon, cleanup_previous_attempt)
        execute_command(first_mon, f"{install_cephadm} && {bootstrap}")
        execute_command(first_mon, ensure_ceph_repo)
        execute_command(first_mon, ensure_ceph_common)
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"cephadm bootstrap thất bại trên {first_mon}: {exc}") from exc

    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_cephadm_orch_host_add(nodes: list[dict], action_params: dict, on_host_update) -> None:
    first_mon = _first_mon_ip(nodes)
    hostnames: dict[str, str] = action_params.get("_node_hostnames", {})
    remaining = [n for n in nodes if n["ip"] != first_mon]

    host_status = [{"host": n["ip"], "status": "pending"} for n in remaining]
    on_host_update(list(host_status))

    # cephadm's own orchestrator SSHes from first_mon to every OTHER host
    # using a DEDICATED keypair it generates during bootstrap
    # (/etc/ceph/ceph.pub) — verified live, 2026-07-26: `ceph orch host add`
    # failed with "Permission denied" for the first non-first-mon host,
    # because that key had never been authorized there. Same step official
    # Ceph docs describe as `ssh-copy-id -f -i /etc/ceph/ceph.pub
    # root@<host>` — done here via the Worker's OWN already-proven SSH
    # access to each node (the exact same access ssh_check already used),
    # not by shelling out to ssh-copy-id.
    try:
        cephadm_pubkey = execute_command(first_mon, "cat /etc/ceph/ceph.pub").strip()
    except ExecutorError as exc:
        raise DeployPhaseError(
            f"{first_mon}: không đọc được khoá SSH của cephadm (/etc/ceph/ceph.pub): {exc}"
        ) from exc

    for i, node in enumerate(remaining):
        ip = node["ip"]
        hostname = hostnames.get(ip, ip)
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            quoted_key = shlex.quote(cephadm_pubkey)
            execute_command(
                ip,
                "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
                f"(grep -qxF {quoted_key} /root/.ssh/authorized_keys 2>/dev/null || "
                f"echo {quoted_key} >> /root/.ssh/authorized_keys) && "
                "chmod 600 /root/.ssh/authorized_keys",
            )
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(
                f"{ip}: không thêm được khoá SSH của cephadm vào authorized_keys: {exc}"
            ) from exc
        try:
            execute_command(
                first_mon, f"ceph orch host add {shlex.quote(hostname)} {shlex.quote(ip)}"
            )
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(
                f"'ceph orch host add {hostname} {ip}' thất bại: {exc}"
            ) from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_cephadm_orch_apply_mgr(nodes: list[dict], action_params: dict, on_host_update) -> None:
    first_mon = _first_mon_ip(nodes)
    hostnames: dict[str, str] = action_params.get("_node_hostnames", {})
    mgr_ips = _node_ips_with_role(nodes, "mgr")
    if not mgr_ips:
        raise DeployPhaseError("Không có node MGR nào trong cấu hình")
    placement = ",".join(hostnames.get(ip, ip) for ip in mgr_ips)

    host_status = [{"host": first_mon, "status": "running"}]
    on_host_update(list(host_status))
    try:
        execute_command(first_mon, f"ceph orch apply mgr --placement={shlex.quote(placement)}")
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"'ceph orch apply mgr' thất bại: {exc}") from exc
    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_cephadm_orch_apply_osd(nodes: list[dict], action_params: dict, on_host_update) -> None:
    first_mon = _first_mon_ip(nodes)
    hostnames: dict[str, str] = action_params.get("_node_hostnames", {})
    osd_nodes = [n for n in nodes if "osd" in (n.get("roles") or [])]
    if not osd_nodes:
        raise DeployPhaseError("Không có node OSD nào trong cấu hình")

    host_status = [{"host": n["ip"], "status": "pending"} for n in osd_nodes]
    on_host_update(list(host_status))

    for i, node in enumerate(osd_nodes):
        ip = node["ip"]
        hostname = hostnames.get(ip, ip)
        # Per-node disk (AC: mỗi node OSD có thể dùng tên đĩa khác nhau, vd
        # node1 /dev/vdc, node2 /dev/vdb) — already validated non-empty at
        # propose time, but the ssh_check phase's read-only safety check is
        # what actually proved THIS disk safe-to-use on THIS host, so re-
        # reading it fresh from `node` here (not a single cluster-wide
        # value) is what keeps that guarantee meaningful per-host.
        osd_disk = node.get("osd_disk")
        if not osd_disk:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{ip}: chưa cấu hình đĩa OSD (osd_disk)")
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            # Explicit device — never --all-available-devices — so only the
            # operator's chosen osd_disk is ever touched (already proven
            # safe-to-use by the ssh_check phase's read-only disk check).
            execute_command(
                first_mon,
                f"ceph orch daemon add osd {shlex.quote(hostname)}:{shlex.quote(osd_disk)}",
            )
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(
                f"Tạo OSD trên {hostname} ({osd_disk}) thất bại: {exc}"
            ) from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


# --- ceph-deploy-specific phases (Story 8.2) -------------------------------
#
# Unlike the cephadm phases above (each a single `ceph orch ...` call sent to
# the already-bootstrapped first MON, which does the real cross-host work
# itself), there is no orchestrator here — every step below is this module
# driving each host directly over SSH, matching the traditional/manual Ceph
# deployment procedure (see the story file's Dev Notes for the exact
# sequence this was modeled on).
#
# Stop-on-first-host-failure (AC #4): every phase below is a plain
# `for node in nodes: ... except ExecutorError: raise DeployPhaseError(...)`
# loop — the first failing host's exception propagates straight out of the
# phase function and `run()` stops the whole deploy right there. This is
# already the natural behavior of a plain loop with an immediate raise; no
# extra "continue vs stop" flag is needed the way it would be if this reused
# `worker/llm/router_client.py::_execute_approved_action`'s generic
# continue-on-failure loop (that loop is for the package-based Cluster
# UPGRADE feature, which deliberately does NOT stop early — see
# commands.py's own module comment on that distinction).


def _mon_data_dir(hostname: str) -> str:
    return f"/var/lib/ceph/mon/ceph-{hostname}"


def _read_remote_file_b64(host: str, path: str) -> str:
    try:
        return execute_command(host, f"base64 {shlex.quote(path)}").strip()
    except ExecutorError as exc:
        raise DeployPhaseError(f"{host}: không đọc được {path}: {exc}") from exc


def _write_remote_file_b64(host: str, path: str, content_b64: str) -> None:
    """Writes base64-decoded bytes to `path` on `host`, creating its parent
    directory first — binary-safe (keyrings/monmaps), unlike a plain heredoc,
    same base64-over-exec_command trick `commands.py::_patch_build_and_stage_command`
    already uses for the same reason (no SFTP anywhere in this codebase —
    see ssh_executor.py's docstring)."""
    quoted_path = shlex.quote(path)
    quoted_dir = shlex.quote(os.path.dirname(path))
    cmd = f"mkdir -p {quoted_dir} && base64 -d > {quoted_path} <<< {shlex.quote(content_b64)}"
    try:
        execute_command(host, cmd)
    except ExecutorError as exc:
        raise DeployPhaseError(f"{host}: không ghi được {path}: {exc}") from exc


def _write_remote_file(host: str, path: str, content: str) -> None:
    _write_remote_file_b64(host, path, base64.b64encode(content.encode()).decode())


def _build_ceph_conf(action_params: dict, mon_nodes: list[dict], hostnames: dict[str, str], fsid: str) -> str:
    """One shared ceph.conf pushed to every node (mon/mgr/osd alike) — lists
    ALL mon nodes (mon_host, mon initial members) plus one [mon.<hostname>]
    section PER mon with that mon's OWN public_addr set explicitly (avoids
    the "wrong NIC picked" failure mode on multi-homed lab nodes — see the
    story file's Dev Notes). A non-mon node simply never uses the [mon.*]
    sections that aren't its own."""
    mon_initial_members = ",".join(hostnames.get(n["ip"], n["ip"]) for n in mon_nodes)
    mon_host = ",".join(n["ip"] for n in mon_nodes)
    public_network = action_params.get("public_network") or ""
    cluster_network = action_params.get("cluster_network") or public_network

    lines = [
        "[global]",
        f"fsid = {fsid}",
        f"mon initial members = {mon_initial_members}",
        f"mon host = {mon_host}",
        f"public network = {public_network}",
        f"cluster network = {cluster_network}",
        "auth cluster required = cephx",
        "auth service required = cephx",
        "auth client required = cephx",
        f"osd pool default size = {action_params.get('osd_pool_default_size', 3)}",
        f"osd pool default min size = {action_params.get('osd_pool_default_min_size', 2)}",
        "",
    ]
    for n in mon_nodes:
        hostname = hostnames.get(n["ip"], n["ip"])
        lines.append(f"[mon.{hostname}]")
        lines.append(f"public addr = {n['ip']}")
        lines.append("")
    return "\n".join(lines)


def _mkfs_and_start_mon_command(hostname: str) -> str:
    data_dir = shlex.quote(_mon_data_dir(hostname))
    quoted_hostname = shlex.quote(hostname)
    return (
        # 2026-07-28 fix (verified live): `ceph-mon --mkfs` against a data
        # dir that ALREADY has a valid store from an earlier attempt does
        # NOT reinitialize it — it silently keeps the OLD store (old fsid,
        # old monmap) and ignores the --monmap/--keyring passed THIS time.
        # A retried "Dựng cụm" (e.g. after an earlier attempt got past
        # mon_init but failed at a LATER phase, and the operator clicked
        # "Dựng cụm" again without an intervening "Xoá cụm") silently ended
        # up with a mon whose real fsid didn't match the freshly-written
        # /etc/ceph/ceph.conf, and every `ceph` CLI call against it failed
        # with the deeply misleading "[errno 1] error connecting to the
        # cluster" — the mon itself was actually healthy and in quorum
        # (confirmed via `ceph daemon mon.<name> mon_status`, which talks
        # to the local admin socket directly, bypassing the network client
        # entirely), just answering to a DIFFERENT fsid than any client
        # reading the current ceph.conf would ever try. Stop any running
        # mon for this hostname and wipe its data dir first so mkfs always
        # starts genuinely clean, regardless of what a previous attempt
        # left behind.
        f"systemctl stop ceph-mon@{quoted_hostname} 2>/dev/null; "
        f"rm -rf {data_dir} && mkdir -p {data_dir} && "
        f"ceph-mon --mkfs -i {quoted_hostname} --monmap {shlex.quote(_REMOTE_MONMAP_PATH)} "
        f"--keyring {shlex.quote(_REMOTE_MON_KEYRING_PATH)} && "
        f"(chown -R ceph:ceph {data_dir} 2>/dev/null || true) && "
        f"systemctl enable --now ceph-mon@{quoted_hostname}"
    )


def _phase_ceph_deploy_dependencies(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Per node: ensure python3 (cephadm's own per-host management agent is
    itself a python3 script the ORCHESTRATOR runs via SSH on EVERY host it
    manages — not just first_mon — verified live, 2026-07-26: `ceph orch
    host add` failed with "no python3 in ..." for the SECOND node added,
    even though first_mon itself had python3 from _phase_cephadm_bootstrap's
    own now-redundant check), stop firewalld, disable SELinux enforcement
    if present, install chrony (+ epel-release on the RPM family only —
    Debian/Ubuntu has no equivalent extra repo to enable), explicitly
    enable+start its service (package install alone doesn't reliably do
    this on every distro — verified live, 2026-07-26: cephadm bootstrap's
    own preflight check ("No time sync service is running") failed on a
    fresh node even after `apt-get install chrony` succeeded, because the
    service was installed but never started), then step the clock so a
    freshly-installed lab VM with a badly drifted clock doesn't fail cephx
    auth later (same reasoning as COMMANDS["resync_ntp"]).

    2026-07-27 fix (verified live): this phase runs BEFORE `repo`/
    `repo_local` in every method's phase list (see _PHASES_BY_ACTION_ID
    below), so on a RETRY after a previous attempt failed partway through
    (e.g. an unrecognized target_version, or download.ceph.com being
    temporarily unreachable), any `download.ceph.com_rpm-*.repo` file that
    previous attempt's `repo` phase already added is still sitting there,
    still enabled — dnf/yum refuse to do ANYTHING (even installing chrony,
    completely unrelated) while any enabled repo fails to refresh, so a
    stale broken Ceph repo blocks this phase too, not just `repo` itself.
    Same defensive `rm -f` the `repo` phase's own command already does,
    just also done here first.
    """
    apt_snippet = (
        "(command -v python3 >/dev/null 2>&1 || apt-get install -y python3) && "
        "(systemctl stop firewalld 2>/dev/null || true) && "
        "(command -v setenforce >/dev/null 2>&1 && setenforce 0 || true) && "
        "apt-get update -y && apt-get install -y chrony && "
        "systemctl enable --now chrony && "
        "(chronyc makestep || true)"
    )
    rpm_snippet = (
        "(command -v python3 >/dev/null 2>&1 || (dnf install -y python3 || yum install -y python3)) && "
        "(systemctl stop firewalld 2>/dev/null || true) && "
        "(command -v setenforce >/dev/null 2>&1 && setenforce 0 || true) && "
        "rm -f /etc/yum.repos.d/download.ceph.com_rpm-*.repo && "
        "(dnf install -y chrony epel-release || yum install -y chrony epel-release) && "
        "systemctl enable --now chronyd && "
        "(chronyc makestep || true)"
    )
    install_command = _package_manager_branch({"apt": apt_snippet, "rpm": rpm_snippet})

    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))
    for i, node in enumerate(nodes):
        host = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            execute_command(host, install_command)
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{host}: cài đặt phụ thuộc thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _build_ceph_package_repo_command(version: str) -> str:
    """Shared by `_phase_ceph_deploy_repo` (ceph-deploy method, all nodes)
    and `_phase_cephadm_bootstrap` (cephadm method, first MON only, for
    `ceph-common`) — our OWN repo setup, deliberately NOT delegated to
    cephadm's own `add-repo` subcommand: verified live, 2026-07-26, that
    `cephadm add-repo` (tried with both `--release <codename>` and
    `--version <exact version>`) left `ceph-common` unfindable via yum on a
    real node twice in a row, for reasons this codebase doesn't control or
    fully understand. This snippet detects the RHEL major version and arch
    AT RUNTIME on the target node (`rpm -E %rhel`, `uname -m`) rather than
    hardcoding one el version — unlike cephadm's own internal logic here,
    this is fully within our control and can be debugged/fixed directly if
    it's ever wrong for a given node.

    Uses the exact VERSION (not the release codename's rolling alias) to
    build the repo URL — same fix `commands.py::_upgrade_ceph_cluster_package_download_command`
    already made (2026-07-24, verified live): the codename alias (e.g.
    rpm-quincy/) only ever carries the OS versions the LATEST point release
    of that codename still supports, silently becoming an empty/404 repo for
    an older-but-still-supported OS. EXCEPT Nautilus (see
    `shared/ceph_releases.py::repo_path_version`'s docstring, verified live
    2026-07-27): download.ceph.com never published a per-exact-version
    directory for Nautilus at all, only the codename alias — which, unlike
    every later release, is now frozen forever since Nautilus is EOL.
    `repo_path_version()` returns the codename instead of the raw version
    for that one case; every other release still gets the exact version.
    """
    repo_path = repo_path_version(version)
    apt_snippet = (
        "wget -q -O- https://download.ceph.com/keys/release.asc "
        "| gpg --dearmor -o /usr/share/keyrings/ceph-archive-keyring.gpg "
        f"&& echo \"deb [signed-by=/usr/share/keyrings/ceph-archive-keyring.gpg] "
        f"https://download.ceph.com/debian-{repo_path}/ $(lsb_release -sc) main\" "
        "> /etc/apt/sources.list.d/ceph.list "
        "&& apt-get update -y"
    )
    rpm_snippet = (
        "rm -f /etc/yum.repos.d/download.ceph.com_rpm-*.repo "
        "&& rpm --import https://download.ceph.com/keys/release.asc "
        "&& (dnf config-manager --add-repo "
        f"https://download.ceph.com/rpm-{repo_path}/el$(rpm -E %rhel)/$(uname -m)/ 2>/dev/null "
        "|| yum-config-manager --add-repo "
        f"https://download.ceph.com/rpm-{repo_path}/el$(rpm -E %rhel)/$(uname -m)/) "
        "&& (dnf config-manager --add-repo "
        f"https://download.ceph.com/rpm-{repo_path}/el$(rpm -E %rhel)/noarch/ 2>/dev/null "
        "|| yum-config-manager --add-repo "
        f"https://download.ceph.com/rpm-{repo_path}/el$(rpm -E %rhel)/noarch/)"
    )
    return _package_manager_branch({"apt": apt_snippet, "rpm": rpm_snippet})


def _phase_ceph_deploy_repo(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Adds the official Ceph package repo for `version` on every node.
    `codename_for_version` is called below purely to reject an unrecognized
    target_version before building any URL at all — the story file's own
    text names it as "the single source of truth this codebase uses for
    release names", which remains true for that validation role even
    though the URL itself doesn't use the codename directly (see
    `_build_ceph_package_repo_command`)."""
    version = action_params.get("version", "")
    if codename_for_version(version) is None:
        raise DeployPhaseError(f"Không tìm thấy mã tên release Ceph cho phiên bản {version!r}")

    repo_command = _build_ceph_package_repo_command(version)

    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))
    for i, node in enumerate(nodes):
        host = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            execute_command(host, repo_command)
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{host}: cấu hình repo thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_ceph_deploy_packages(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """The first phase in the whole feature where a SINGLE Action's per-host
    command depends on that host's OWN role — a node with multiple roles
    (e.g. mon+osd) gets multiple packages in one install command. Plain
    per-host branch inside this phase function, not a new generic capability
    in commands.py's dispatch tables (see this story's Dev Notes).

    2026-07-27 fix (verified live): for every release EXCEPT Nautilus, the
    repo this phase installs from is scoped to exactly ONE version (see
    `_build_ceph_package_repo_command`'s docstring — `rpm-<version>/` is a
    frozen per-release archive), so a bare `dnf install ceph-mon` (no
    version pin) trivially always resolves to the one version present.
    Nautilus is the exception: `rpm-nautilus/` physically hosts EVERY
    Nautilus point release's RPMs side by side (14.2.10 through 14.2.22 as
    of this fix), and its repodata genuinely advertises all of them as
    separate installable NEVRAs — `dnf install ceph-mon` there silently
    resolves to whichever is numerically newest (currently 14.2.22),
    REGARDLESS of which Nautilus point release the operator actually
    picked. Pin the exact version in the package name for RPM installs
    whenever `repo_path_version()` had to fall back to the codename, so an
    operator picking an older-but-still-Nautilus point release (e.g.
    14.2.15) actually gets that version, not silently 14.2.22. apt/dpkg is
    unaffected — debian-nautilus/dists/*/Packages only ever lists the
    latest version per suite (verified live), so there is no equivalent
    ambiguity to pin against there."""
    version = action_params.get("version", "")
    pin_exact_version = repo_path_version(version) != version

    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))

    for i, node in enumerate(nodes):
        host = node["ip"]
        packages = sorted(
            {_ROLE_TO_PACKAGE[r] for r in (node.get("roles") or []) if r in _ROLE_TO_PACKAGE}
        )
        if not packages:
            host_status[i]["status"] = "done"
            host_status[i]["message"] = "không có vai trò mon/mgr/osd — bỏ qua"
            on_host_update(list(host_status))
            continue

        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        package_list = " ".join(packages)
        rpm_package_list = " ".join(f"{pkg}-{version}" for pkg in packages) if pin_exact_version else package_list
        apt_snippet = f"apt-get install -y {package_list}"
        rpm_snippet = f"(dnf install -y {rpm_package_list} || yum install -y {rpm_package_list})"
        install_command = _package_manager_branch({"apt": apt_snippet, "rpm": rpm_snippet})
        try:
            execute_command(host, install_command)
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{host}: cài gói {package_list} thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        host_status[i]["message"] = package_list
        on_host_update(list(host_status))


def _phase_ceph_deploy_mon_init(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Generates a fresh fsid, monitor/admin/bootstrap-osd keyrings, and a
    monmap ONCE on the first MON node (it already has ceph-authtool/
    monmaptool from the packages phase — this codebase's Worker host itself
    is never assumed to have Ceph tools installed; every worker/executor/
    command runs entirely over SSH, see ssh_executor.py's docstring), then
    fetches those artifacts and pushes the SAME bytes to every other MON
    node before running `ceph-mon --mkfs` + starting the daemon everywhere.
    All MON nodes MUST share the exact same keyring/monmap — generating a
    fresh one per node independently would produce mons that can never form
    one quorum."""
    mon_nodes = [n for n in nodes if "mon" in (n.get("roles") or [])]
    if not mon_nodes:
        raise DeployPhaseError("Không có node MON nào trong cấu hình")
    hostnames: dict[str, str] = action_params.get("_node_hostnames", {})
    first_mon_ip = mon_nodes[0]["ip"]
    first_mon_hostname = hostnames.get(first_mon_ip, first_mon_ip)

    fsid = str(uuid.uuid4())
    ceph_conf = _build_ceph_conf(action_params, mon_nodes, hostnames, fsid)
    # Scratch state for later phases within THIS run() call only — never
    # persisted back to the DB, same posture as _node_hostnames above.
    action_params["_ceph_conf"] = ceph_conf

    host_status = [{"host": n["ip"], "status": "pending"} for n in mon_nodes]
    on_host_update(list(host_status))

    host_status[0]["status"] = "running"
    on_host_update(list(host_status))
    try:
        _write_remote_file(first_mon_ip, _REMOTE_CEPH_CONF_PATH, ceph_conf)
        add_args = " ".join(
            f"--add {shlex.quote(hostnames.get(n['ip'], n['ip']))} {shlex.quote(n['ip'])}"
            for n in mon_nodes
        )
        keygen_command = (
            # 2026-07-27 fix (verified live): monmaptool --create refuses to
            # overwrite an existing /tmp/ceph-aiops.monmap ("--clobber to
            # overwrite") — these /tmp scratch files (see _REMOTE_MON_KEYRING_PATH/
            # _REMOTE_MONMAP_PATH) are never cleaned up after use, so a
            # RETRY of a failed/aborted deploy attempt always hit this,
            # even on a node "Xoá cụm" had already fully torn down (that
            # feature only ever cleaned /etc/ceph, /var/lib/ceph — real
            # Ceph state — never these transient generation-time scratch
            # files under /tmp). Clean them first so this phase is
            # idempotent across retries.
            f"rm -f {shlex.quote(_REMOTE_MON_KEYRING_PATH)} {shlex.quote(_REMOTE_MONMAP_PATH)} && "
            f"ceph-authtool --create-keyring {shlex.quote(_REMOTE_MON_KEYRING_PATH)} "
            f"--gen-key -n mon. --cap mon 'allow *' && "
            f"ceph-authtool --create-keyring {shlex.quote(_REMOTE_ADMIN_KEYRING_PATH)} "
            f"--gen-key -n client.admin --cap mon 'allow *' --cap osd 'allow *' "
            f"--cap mds 'allow *' --cap mgr 'allow *' && "
            f"mkdir -p {shlex.quote(os.path.dirname(_REMOTE_BOOTSTRAP_OSD_KEYRING_PATH))} && "
            f"ceph-authtool --create-keyring {shlex.quote(_REMOTE_BOOTSTRAP_OSD_KEYRING_PATH)} "
            f"--gen-key -n client.bootstrap-osd --cap mon 'profile bootstrap-osd' && "
            f"ceph-authtool {shlex.quote(_REMOTE_MON_KEYRING_PATH)} "
            f"--import-keyring {shlex.quote(_REMOTE_ADMIN_KEYRING_PATH)} && "
            f"ceph-authtool {shlex.quote(_REMOTE_MON_KEYRING_PATH)} "
            f"--import-keyring {shlex.quote(_REMOTE_BOOTSTRAP_OSD_KEYRING_PATH)} && "
            f"monmaptool --create --fsid {shlex.quote(fsid)} {add_args} "
            f"{shlex.quote(_REMOTE_MONMAP_PATH)}"
        )
        execute_command(first_mon_ip, keygen_command)
        execute_command(first_mon_ip, _mkfs_and_start_mon_command(first_mon_hostname))
    except (ExecutorError, DeployPhaseError) as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{first_mon_ip}: khởi tạo MON đầu tiên thất bại: {exc}") from exc
    host_status[0]["status"] = "done"
    on_host_update(list(host_status))

    mon_keyring_b64 = _read_remote_file_b64(first_mon_ip, _REMOTE_MON_KEYRING_PATH)
    monmap_b64 = _read_remote_file_b64(first_mon_ip, _REMOTE_MONMAP_PATH)
    admin_keyring_b64 = _read_remote_file_b64(first_mon_ip, _REMOTE_ADMIN_KEYRING_PATH)
    action_params["_admin_keyring_b64"] = admin_keyring_b64
    action_params["_bootstrap_osd_keyring_b64"] = _read_remote_file_b64(
        first_mon_ip, _REMOTE_BOOTSTRAP_OSD_KEYRING_PATH
    )

    for i, node in enumerate(mon_nodes[1:], start=1):
        ip = node["ip"]
        hostname = hostnames.get(ip, ip)
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            _write_remote_file(ip, _REMOTE_CEPH_CONF_PATH, ceph_conf)
            _write_remote_file_b64(ip, _REMOTE_MON_KEYRING_PATH, mon_keyring_b64)
            _write_remote_file_b64(ip, _REMOTE_MONMAP_PATH, monmap_b64)
            _write_remote_file_b64(ip, _REMOTE_ADMIN_KEYRING_PATH, admin_keyring_b64)
            execute_command(ip, _mkfs_and_start_mon_command(hostname))
        except (ExecutorError, DeployPhaseError) as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{ip}: khởi tạo MON thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_ceph_deploy_wait_quorum(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Python-level bounded retry loop (AC #2 item 6) — NOT a bash `while`,
    so tests can drive it deterministically (monkeypatch execute_command's
    returned quorum_names, and/or _QUORUM_POLL_INTERVAL_SECONDS /
    action_params["quorum_timeout_seconds"] to keep it fast). Polls via the
    first MON node only — any mon that reached quorum can answer this query
    for the whole set."""
    mon_nodes = [n for n in nodes if "mon" in (n.get("roles") or [])]
    if not mon_nodes:
        raise DeployPhaseError("Không có node MON nào trong cấu hình")
    first_mon_ip = mon_nodes[0]["ip"]
    expected = len(mon_nodes)

    host_status = [{"host": first_mon_ip, "status": "running"}]
    on_host_update(list(host_status))

    timeout = action_params.get("quorum_timeout_seconds", _QUORUM_DEFAULT_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        try:
            output = execute_command(first_mon_ip, "ceph quorum_status --format json")
            quorum_names = json.loads(output).get("quorum_names") or []
            if len(quorum_names) >= expected:
                host_status[0]["status"] = "done"
                host_status[0]["message"] = f"quorum: {','.join(quorum_names)}"
                on_host_update(list(host_status))
                return
        except ExecutorError as exc:
            last_error = exc
        except (TypeError, ValueError) as exc:
            last_error = exc

        if time.monotonic() >= deadline:
            break
        time.sleep(_QUORUM_POLL_INTERVAL_SECONDS)

    host_status[0]["status"] = "failed"
    on_host_update(list(host_status))
    detail = f" (lỗi gần nhất: {last_error})" if last_error else ""
    raise DeployPhaseError(f"Không đạt quorum MON sau {timeout}s{detail}")


def _phase_ceph_deploy_mon_security(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Runs right after MON quorum is established — enables msgr2 (the v2
    wire protocol) and disables the legacy insecure-global-id-reclaim
    allowance, via the first MON node (any mon that reached quorum can run
    these against the whole cluster, same reasoning as wait_quorum's own
    query above).

    Verified live, 2026-07-27: _phase_ceph_deploy_mon_init generates its
    monmap via a plain `monmaptool --add <hostname> <ip>` (no explicit
    v1:/v2: address), which binds each mon to v1 ONLY — every cluster built
    via this method (ceph-deploy or rpm-local, both share this same mon_init
    phase) started already reporting MON_MSGR2_NOT_ENABLED +
    AUTH_INSECURE_GLOBAL_ID_RECLAIM_ALLOWED health warnings out of the box,
    exactly the pair this app's OWN Watcher flagged as investigate_manually
    incidents against a real cluster built this way this session — an
    operator had to fix both by hand after every single deploy. Both
    commands are idempotent/non-disruptive (verified live: `ceph mon
    enable-msgr2` briefly drops the mon it's run through out of quorum
    while it rebinds, self-recovers within seconds; `ceph config set` is
    just a config write) — safe to always run, not gated behind any
    operator choice."""
    mon_nodes = [n for n in nodes if "mon" in (n.get("roles") or [])]
    if not mon_nodes:
        raise DeployPhaseError("Không có node MON nào trong cấu hình")
    first_mon_ip = mon_nodes[0]["ip"]

    host_status = [{"host": first_mon_ip, "status": "running"}]
    on_host_update(list(host_status))
    try:
        execute_command(
            first_mon_ip,
            "ceph mon enable-msgr2 && "
            "ceph config set mon auth_allow_insecure_global_id_reclaim false",
        )
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(
            f"{first_mon_ip}: bật msgr2 / tắt insecure global-id-reclaim thất bại: {exc}"
        ) from exc
    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_ceph_deploy_mgr_create(nodes: list[dict], action_params: dict, on_host_update) -> None:
    mon_nodes = [n for n in nodes if "mon" in (n.get("roles") or [])]
    if not mon_nodes:
        raise DeployPhaseError("Không có node MON nào trong cấu hình")
    first_mon_ip = mon_nodes[0]["ip"]
    hostnames: dict[str, str] = action_params.get("_node_hostnames", {})
    mgr_nodes = [n for n in nodes if "mgr" in (n.get("roles") or [])]
    if not mgr_nodes:
        raise DeployPhaseError("Không có node MGR nào trong cấu hình")

    ceph_conf = action_params.get("_ceph_conf")
    if not ceph_conf:
        raise DeployPhaseError("Thiếu ceph.conf từ bước khởi tạo MON — không thể tạo MGR")

    host_status = [{"host": n["ip"], "status": "pending"} for n in mgr_nodes]
    on_host_update(list(host_status))

    for i, node in enumerate(mgr_nodes):
        ip = node["ip"]
        hostname = hostnames.get(ip, ip)
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            remote_tmp_keyring = f"/tmp/ceph-aiops-mgr-{hostname}.keyring"
            execute_command(
                first_mon_ip,
                f"ceph auth get-or-create mgr.{shlex.quote(hostname)} "
                f"mon 'allow profile mgr' osd 'allow *' mds 'allow *' "
                f"-o {shlex.quote(remote_tmp_keyring)}",
            )
            keyring_b64 = _read_remote_file_b64(first_mon_ip, remote_tmp_keyring)

            mgr_dir = f"/var/lib/ceph/mgr/ceph-{hostname}"
            _write_remote_file(ip, _REMOTE_CEPH_CONF_PATH, ceph_conf)
            _write_remote_file_b64(ip, f"{mgr_dir}/keyring", keyring_b64)
            execute_command(
                ip,
                f"(chown -R ceph:ceph {shlex.quote(mgr_dir)} 2>/dev/null || true) && "
                f"systemctl enable --now ceph-mgr@{shlex.quote(hostname)}",
            )
        except (ExecutorError, DeployPhaseError) as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{ip}: tạo/khởi động MGR thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_ceph_deploy_osd_create(nodes: list[dict], action_params: dict, on_host_update) -> None:
    osd_nodes = [n for n in nodes if "osd" in (n.get("roles") or [])]
    if not osd_nodes:
        raise DeployPhaseError("Không có node OSD nào trong cấu hình")
    ceph_conf = action_params.get("_ceph_conf")
    bootstrap_osd_keyring_b64 = action_params.get("_bootstrap_osd_keyring_b64")
    if not ceph_conf or not bootstrap_osd_keyring_b64:
        raise DeployPhaseError(
            "Thiếu ceph.conf/bootstrap-osd keyring từ bước khởi tạo MON — không thể tạo OSD"
        )

    host_status = [{"host": n["ip"], "status": "pending"} for n in osd_nodes]
    on_host_update(list(host_status))

    for i, node in enumerate(osd_nodes):
        ip = node["ip"]
        # Per-node disk (vd node1 /dev/vdc, node2 /dev/vdb) — see the cephadm
        # phase's own osd_disk comment for why this is read fresh per node
        # rather than one cluster-wide value.
        osd_disk = node.get("osd_disk")
        if not osd_disk:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{ip}: chưa cấu hình đĩa OSD (osd_disk)")
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            _write_remote_file(ip, _REMOTE_CEPH_CONF_PATH, ceph_conf)
            _write_remote_file_b64(ip, _REMOTE_BOOTSTRAP_OSD_KEYRING_PATH, bootstrap_osd_keyring_b64)
            # Explicit device — never --all-available-devices — same safety
            # posture as the cephadm phase's own OSD-creation step; osd_disk
            # was already proven empty/unmounted by the ssh_check phase's
            # read-only check before ANY phase (including this one) ran.
            execute_command(ip, f"ceph-volume lvm create --data {shlex.quote(osd_disk)}")
        except (ExecutorError, DeployPhaseError) as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"Tạo OSD trên {ip} ({osd_disk}) thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_verify(nodes: list[dict], action_params: dict, on_host_update) -> None:
    first_mon = _first_mon_ip(nodes)
    host_status = [{"host": first_mon, "status": "running"}]
    on_host_update(list(host_status))

    try:
        output = execute_command(first_mon, "ceph -s --format json")
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"Không lấy được trạng thái cụm: {exc}") from exc

    try:
        health = json.loads(output).get("health", {}).get("status")
    except (TypeError, ValueError, AttributeError):
        health = None

    if health == "HEALTH_ERR":
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError("Cụm ở trạng thái HEALTH_ERR sau khi dựng — dừng lại")

    host_status[0]["status"] = "done"
    host_status[0]["message"] = health or "unknown"
    on_host_update(list(host_status))


# --- Xóa cụm Ceph (2026-07-26) -----------------------------------------------
#
# dashboard/routes/delete_cluster.py-only — tears down the CURRENTLY
# CONFIGURED cluster (nodes come from shared/cluster_nodes.py::configured_nodes(),
# NOT operator-entered like Dựng cụm's own form — deleting the wrong
# cluster because of a typo in a freshly-typed node list would be a far
# worse failure mode here than reusing what Settings already has
# configured). Two action_ids depending on how the cluster reports itself
# as currently run (CEPH_EXEC_MODE): cephadm's own `rm-cluster` handles
# daemons/containers/config and (optionally) OSD disks in ONE command; a
# manually/package-deployed cluster (ceph-deploy or rpm-local method, both
# land in CEPH_EXEC_MODE=none) has no such tool, so this stops every
# discovered systemd unit itself and, only if the operator opted in,
# wipes each OSD disk via `ceph-volume lvm zap --destroy`.


def _phase_delete_ssh_check(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Lightweight connectivity-only check — unlike Dựng cụm's ssh_check,
    there is no "disk must be empty" requirement here (the whole point of
    THIS feature is a cluster that's NOT empty); this only confirms every
    configured node is reachable before any teardown command is sent to
    any of them."""
    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))
    for i, node in enumerate(nodes):
        host = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            execute_command(host, "true")
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"Không kết nối được SSH tới {host}: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_delete_manual_stop_daemons(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Discovers and stops+disables every Ceph systemd unit on every node,
    in ONE remote shell invocation per host (a plain `systemctl list-units
    'ceph-*'` glob rather than reusing commands.py's
    _discover_ceph_units — that helper calls execute_command itself from
    commands.py's own module-level reference, which tests monkeypatching
    THIS module's execute_command wouldn't intercept; a self-contained
    shell one-liner keeps this phase testable the same way every other
    phase in this module already is)."""
    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))
    command = (
        "units=$(systemctl list-units --all --plain --no-legend 'ceph-*' 2>/dev/null | awk '{print $1}'); "
        "if [ -z \"$units\" ]; then echo 'Khong tim thay daemon Ceph nao tren node nay'; exit 0; fi; "
        "for u in $units; do systemctl stop \"$u\" 2>/dev/null; systemctl disable \"$u\" 2>/dev/null; done; "
        "echo \"Da dung: $units\""
    )
    for i, node in enumerate(nodes):
        host = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            output = execute_command(host, command)
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{host}: dừng daemon Ceph thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        last_line = output.strip().splitlines()[-1] if output.strip() else None
        host_status[i]["message"] = last_line
        on_host_update(list(host_status))


def _phase_delete_manual_remove_state(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Removes Ceph's own software state (/etc/ceph, /var/lib/ceph) on
    every node — config, keyrings, mon/mgr data dirs. Deliberately ALWAYS
    runs regardless of the wipe_osd_disks choice: this is Ceph's software
    footprint, not the raw OSD block device's data (a ceph-volume lvm OSD's
    /var/lib/ceph/osd/ceph-<id> is a tmpfs-backed activation mountpoint,
    not where the real data lives — see _phase_delete_manual_wipe_osd_disk
    for what actually touches the disk).

    Verified live, 2026-07-27: stopping the ceph-osd systemd unit (previous
    phase) does NOT unmount that tmpfs activation mountpoint — it stays
    mounted independently of the daemon's own lifecycle, so a bare `rm -rf
    /var/lib/ceph` fails with "Device or resource busy" on
    /var/lib/ceph/osd/ceph-<id> every time an OSD was ever activated on that
    node. Unmount every /var/lib/ceph/... mountpoint first (a plain `umount`
    is enough now that the daemon is already stopped; `umount -l` is a
    fallback only, not the primary path, in case anything else still has a
    file open under it).

    2026-07-27, "xoá cho kĩ": also removes every OTHER trace this app's own
    Deploy phases leave on a node, so a later "Dựng cụm" on the SAME node
    starts genuinely clean instead of tripping over leftovers from the
    cluster just deleted — (1) /tmp/ceph-aiops* scratch files
    (_phase_ceph_deploy_mon_init's fsid/monmap/keyring generation scratch
    space, never cleaned up after use — verified live: a STALE one of these
    is exactly what made monmaptool refuse a later deploy attempt with
    "--clobber to overwrite"), and (2) the Ceph package repo files this
    app's own install phases add (download.ceph.com_rpm-*.repo,
    ceph.list, and rpm-local's own ceph-aiops-local.repo/.list) — belt and
    suspenders alongside the dependencies phase's own defensive cleanup of
    the same files, since a node could otherwise sit with a now-pointless
    enabled repo indefinitely between one cluster's deletion and the next
    deploy. Never touches an operator's own repo files (different names) or
    anything under /etc/ceph, /var/lib/ceph beyond what's already covered
    above."""
    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))
    unmount_then_remove = (
        "for m in $(awk '$2 ~ /^\\/var\\/lib\\/ceph\\// {print $2}' /proc/mounts); do "
        "umount \"$m\" 2>/dev/null || umount -l \"$m\" 2>/dev/null; done; "
        "rm -rf /etc/ceph /var/lib/ceph /tmp/ceph-aiops*; "
        "rm -f /etc/yum.repos.d/download.ceph.com_rpm-*.repo /etc/yum.repos.d/ceph-aiops-local.repo "
        "/etc/apt/sources.list.d/ceph.list /etc/apt/sources.list.d/ceph-aiops-local.list"
    )
    for i, node in enumerate(nodes):
        host = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            execute_command(host, unmount_then_remove)
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{host}: xoá /etc/ceph, /var/lib/ceph thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_delete_manual_remove_packages(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Uninstalls the Ceph OS packages themselves (ceph/ceph-mon/ceph-mgr/
    ceph-osd/cephadm/librados2/librbd1/libcephfs2/python3-ceph*/...) —
    _phase_delete_manual_remove_state above only ever removed Ceph's own
    software STATE (/etc/ceph, /var/lib/ceph), never the packages, so a
    node that was ever part of a cluster kept them installed indefinitely
    even after "Xoá cụm".

    Verified live, 2026-07-27: a node still carrying an earlier cluster's
    packages (e.g. Quincy 17.2.7, left behind by a prior cephadm deploy
    that was later "Xoá cụm"-ed) makes a LATER "Dựng cụm" attempt at a
    different major version (e.g. Nautilus 14.2.22) fail outright —
    ceph-mgr/ceph-osd/librados2 etc. all require an EXACT matching version
    of each other, so dnf refuses to have both 14.2.22 and 17.2.7 sets
    installed side by side ("cannot install both ceph-mgr-2:14.2.22... and
    ceph-mgr-2:17.2.7... from @System"). "Xoá cụm" deleting a cluster
    should mean the node is actually clean again, able to host a
    differently-versioned cluster next time — not just have its config
    wiped while the old package set silently lingers.

    Glob-based removal (`ceph*`/`librados*`/...) so this doesn't need its
    own hardcoded package list to keep in sync with every install phase —
    deliberately does NOT touch epel-release (a general-purpose repo the
    dependencies phase enables, not Ceph-specific — removing it could
    affect other things an operator installed via EPEL unrelated to Ceph).
    Every command is wrapped in `|| true`: a node with nothing Ceph-related
    installed (e.g. never had OSD/MGR roles) has nothing to remove, and
    that must never fail this phase."""
    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))
    package_globs = "'ceph*' 'libcephfs*' 'librados*' 'librbd*' 'python3-ceph*' 'python3-rados' 'python3-rbd'"
    apt_snippet = (
        f"(apt-get purge -y {package_globs} 2>/dev/null || true) && "
        "(apt-get autoremove -y 2>/dev/null || true)"
    )
    rpm_snippet = (
        f"(dnf remove -y {package_globs} 2>/dev/null "
        f"|| yum remove -y {package_globs} 2>/dev/null || true)"
    )
    remove_packages_command = _package_manager_branch({"apt": apt_snippet, "rpm": rpm_snippet})
    for i, node in enumerate(nodes):
        host = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            execute_command(host, remove_packages_command)
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{host}: gỡ cài đặt gói Ceph thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_delete_manual_wipe_osd_disk(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Only touches a disk if the operator explicitly opted into
    wipe_osd_disks — otherwise every host is marked done immediately
    without a single command sent, so this phase's presence in the fixed
    phase list is harmless for an operator who chose NOT to wipe anything.
    `ceph-volume lvm zap --destroy` (not a bare wipefs) — the same tool
    _phase_ceph_deploy_osd_create used to CREATE the OSD, so it correctly
    tears down the LVM structures ceph-volume itself created, not just the
    device's leading bytes."""
    osd_nodes = [n for n in nodes if "osd" in (n.get("roles") or [])]
    host_status = [{"host": n["ip"], "status": "pending"} for n in osd_nodes]
    on_host_update(list(host_status))

    if not action_params.get("wipe_osd_disks"):
        for i, node in enumerate(osd_nodes):
            host_status[i]["status"] = "done"
            host_status[i]["message"] = "Bỏ qua — không yêu cầu xoá dữ liệu đĩa"
        on_host_update(list(host_status))
        return

    for i, node in enumerate(osd_nodes):
        ip = node["ip"]
        osd_disk = node.get("osd_disk")
        if not osd_disk:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{ip}: chưa cấu hình đĩa OSD cần xoá (osd_disk)")
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            execute_command(ip, f"ceph-volume lvm zap --destroy {shlex.quote(osd_disk)}")
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"Xoá dữ liệu đĩa {osd_disk} trên {ip} thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


# --- Chuyển đổi cụm systemd -> cephadm (2026-07-28) -------------------------
#
# dashboard/routes/convert_cluster.py-only — adopts every MON/MGR/OSD daemon
# of the CURRENTLY CONFIGURED cluster (same "nodes come from
# shared/cluster_nodes.py::configured_nodes(), never operator-typed" posture
# as Xóa cụm above) into cephadm management IN PLACE, following Ceph's own
# documented `cephadm adopt --style legacy` procedure — no daemon is
# recreated, no OSD data is touched, only how each daemon is started/managed
# changes (native systemd unit -> cephadm-managed container). Deliberately
# ONE-DIRECTION only (systemd -> cephadm): the reverse has no equivalent
# official Ceph command, and was explicitly scoped out per an operator
# decision when this feature was requested (see action_policy.yaml's
# `convert_cluster_to_cephadm` comment).
#
# NOT verified against a real running systemd cluster this session — unlike
# every phase above (each has its own "verified live, 2026-07-2x" fixes from
# iterating against a real lab cluster), this is a first-pass implementation
# of the documented adoption procedure. Test against a non-critical cluster
# before trusting this against real production data.
#
# Scope: MON/MGR/OSD only, matching every other phase list in this module
# (none of them handle MDS/RGW daemons either) — an existing RGW/MDS daemon
# on a converted cluster is left running under systemd, untouched, and will
# NOT show up under `ceph orch ps` afterward. `_delete_manual_stop_daemons`
# above works around this same gap differently (a broad `ceph-*` systemd
# glob catches RGW/MDS too, since it only ever STOPS units, never needs to
# know their exact adopted name) — adoption can't take that shortcut, since
# `cephadm adopt --name` needs the EXACT daemon type+id.


def _discover_systemd_daemon_id(host: str, service_prefix: str) -> str | None:
    """Finds the running `<service_prefix>@<id>` systemd unit on `host` and
    returns just the `<id>` part (e.g. service_prefix="ceph-mon" finding
    "ceph-mon@node1.service" running returns "node1") — this is the REAL id
    Ceph already knows this daemon by, needed for `cephadm adopt --name
    mon.<id>`/`mgr.<id>` below. Deliberately discovered fresh from the
    live system rather than assumed to equal this host's current `hostname`
    output (which happens to be true for a cluster THIS app's own
    ceph-deploy/rpm-local method built, but adoption must also work
    correctly against a pre-existing systemd cluster this app never built).
    Returns None if no matching unit is found (that role isn't actually
    running on this host — caller decides whether that's an error)."""
    try:
        output = execute_command(
            host,
            f"systemctl list-units --all --plain --no-legend {shlex.quote(service_prefix + '@*')} "
            "2>/dev/null | awk '{print $1}'",
        )
    except ExecutorError as exc:
        raise DeployPhaseError(
            f"{host}: không liệt kê được systemd unit {service_prefix}@*: {exc}"
        ) from exc
    units = [u.strip() for u in output.splitlines() if u.strip()]
    if not units:
        return None
    # A host running more than one instance of the same daemon TYPE (e.g.
    # two mons) would be unusual for mon/mgr specifically — take the first
    # rather than fail outright.
    unit = units[0]
    if "@" not in unit:
        return None
    daemon_id = unit.split("@", 1)[1]
    return daemon_id[: -len(".service")] if daemon_id.endswith(".service") else daemon_id


def _phase_convert_ssh_check(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Same connectivity-only check as _phase_delete_ssh_check (no "disk
    must be empty" requirement — this cluster's OSD disks are expected to
    already hold real data), plus hostname discovery for
    `ceph orch host add`'s label later (register_hosts phase) — that label
    is just an orchestrator-internal identifier, unrelated to the mon/mgr
    daemon's OWN id (see _discover_systemd_daemon_id above for that)."""
    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))
    hostnames: dict[str, str] = {}
    for i, node in enumerate(nodes):
        host = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            execute_command(host, "true")
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"Không kết nối được SSH tới {host}: {exc}") from exc
        try:
            hostname_output = execute_command(host, "hostname -f 2>/dev/null || hostname").strip()
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{host}: không lấy được hostname: {exc}") from exc
        hostnames[host] = hostname_output or host
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))
    action_params["_node_hostnames"] = hostnames


def _phase_convert_install_cephadm(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Ensures the `cephadm` binary itself is present on EVERY node (needed
    locally by every later `cephadm adopt` call, on whichever host runs
    that daemon) — same install snippet _phase_cephadm_bootstrap uses for
    first_mon only, applied here to every node instead. Deliberately does
    NOT install a container runtime (docker/podman) — same assumption every
    OTHER phase in this module already makes (cephadm bootstrap itself
    requires one pre-installed; this codebase has never automated that
    installation, see this module's own docstring). Inherits that same
    snippet's RPM/EL9-only download URL — a known, pre-existing scope limit
    of this codebase's cephadm support, not something new to this feature.
    """
    version = action_params.get("version", "")
    codename = codename_for_version(version)
    if codename is None:
        raise DeployPhaseError(f"Không tìm thấy mã tên release Ceph cho phiên bản {version!r}")

    install_cephadm = (
        "command -v cephadm >/dev/null 2>&1 || "
        f"(curl -fsSL https://download.ceph.com/rpm-{codename}/el9/noarch/cephadm "
        "-o /usr/local/bin/cephadm && chmod +x /usr/local/bin/cephadm && "
        f"/usr/local/bin/cephadm add-repo --version {version} && /usr/local/bin/cephadm install)"
    )

    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))
    for i, node in enumerate(nodes):
        ip = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            execute_command(ip, install_cephadm)
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{ip}: cài đặt cephadm thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _adopt_daemon_on_host(ip: str, service_prefix: str, daemon_type: str) -> str:
    """Shared by adopt_mons/adopt_mgrs below — discovers the real daemon id
    running on `ip` (via _discover_systemd_daemon_id) and runs
    `cephadm adopt --style legacy --name <daemon_type>.<id>` there. Returns
    the id (for the caller's own status message). Raises DeployPhaseError
    if no matching systemd unit is found at all, or if the adopt command
    itself fails."""
    daemon_id = _discover_systemd_daemon_id(ip, service_prefix)
    if not daemon_id:
        raise DeployPhaseError(f"{ip}: không tìm thấy systemd unit {service_prefix}@* đang chạy")
    try:
        execute_command(
            ip, f"cephadm adopt --style legacy --name {shlex.quote(daemon_type + '.' + daemon_id)}"
        )
    except ExecutorError as exc:
        raise DeployPhaseError(f"{ip}: chuyển đổi {daemon_type}.{daemon_id} thất bại: {exc}") from exc
    return daemon_id


def _phase_convert_adopt_mons(nodes: list[dict], action_params: dict, on_host_update) -> None:
    mon_nodes = [n for n in nodes if "mon" in (n.get("roles") or [])]
    if not mon_nodes:
        raise DeployPhaseError("Không có node MON nào trong cấu hình")
    host_status = [{"host": n["ip"], "status": "pending"} for n in mon_nodes]
    on_host_update(list(host_status))
    for i, node in enumerate(mon_nodes):
        ip = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            mon_id = _adopt_daemon_on_host(ip, "ceph-mon", "mon")
        except DeployPhaseError:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise
        host_status[i]["status"] = "done"
        host_status[i]["message"] = f"mon.{mon_id}"
        on_host_update(list(host_status))


def _phase_convert_adopt_mgrs(nodes: list[dict], action_params: dict, on_host_update) -> None:
    mgr_nodes = [n for n in nodes if "mgr" in (n.get("roles") or [])]
    if not mgr_nodes:
        raise DeployPhaseError("Không có node MGR nào trong cấu hình")
    host_status = [{"host": n["ip"], "status": "pending"} for n in mgr_nodes]
    on_host_update(list(host_status))
    for i, node in enumerate(mgr_nodes):
        ip = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            mgr_id = _adopt_daemon_on_host(ip, "ceph-mgr", "mgr")
        except DeployPhaseError:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise
        host_status[i]["status"] = "done"
        host_status[i]["message"] = f"mgr.{mgr_id}"
        on_host_update(list(host_status))


def _phase_convert_enable_orchestrator(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Enables the cephadm mgr module + routes the orchestrator to it, on
    the just-adopted MGR's host — needed before `ceph orch host add`/`ceph
    orch ps` (later phases) mean anything. `ceph cephadm generate-key`
    ensures the orchestrator's own dedicated SSH keypair exists (bootstrap
    generates one automatically as part of its own setup; adoption never
    calls bootstrap, so this is the equivalent explicit step) — harmless if
    a key already exists."""
    first_mon = _first_mon_ip(nodes)
    host_status = [{"host": first_mon, "status": "running"}]
    on_host_update(list(host_status))
    try:
        execute_command(
            first_mon,
            "ceph mgr module enable cephadm && ceph orch set backend cephadm && "
            "(ceph cephadm generate-key || true)",
        )
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"Bật cephadm orchestrator trên {first_mon} thất bại: {exc}") from exc
    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_convert_distribute_ssh_key(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Same SSH-key-distribution step _phase_cephadm_orch_host_add does for
    a brand-new bootstrap, adapted for adoption: the key comes from
    `ceph cephadm get-pub-key` (a live `ceph` CLI query against the
    orchestrator module enabled in the previous phase) rather than reading
    a local `/etc/ceph/ceph.pub` file — that file is only ever written as a
    SIDE EFFECT of `cephadm bootstrap` itself, which adoption never runs."""
    first_mon = _first_mon_ip(nodes)
    other_nodes = [n for n in nodes if n["ip"] != first_mon]
    host_status = [{"host": n["ip"], "status": "pending"} for n in other_nodes]
    on_host_update(list(host_status))

    try:
        cephadm_pubkey = execute_command(first_mon, "ceph cephadm get-pub-key").strip()
    except ExecutorError as exc:
        raise DeployPhaseError(
            f"{first_mon}: không lấy được khoá SSH của cephadm (ceph cephadm get-pub-key): {exc}"
        ) from exc
    if not cephadm_pubkey:
        raise DeployPhaseError(f"{first_mon}: ceph cephadm get-pub-key trả về rỗng")

    for i, node in enumerate(other_nodes):
        ip = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            quoted_key = shlex.quote(cephadm_pubkey)
            execute_command(
                ip,
                "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
                f"(grep -qxF {quoted_key} /root/.ssh/authorized_keys 2>/dev/null || "
                f"echo {quoted_key} >> /root/.ssh/authorized_keys) && "
                "chmod 600 /root/.ssh/authorized_keys",
            )
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(
                f"{ip}: không thêm được khoá SSH của cephadm vào authorized_keys: {exc}"
            ) from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_convert_register_hosts(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """`ceph orch host add <label> <ip>` for EVERY node (including
    first_mon itself — unlike a fresh bootstrap, which auto-registers its
    own host, adoption never implicitly registers anything). `<label>` is
    just the orchestrator's own display name for the host (shown in `ceph
    orch host ls`) — unrelated to the mon/mgr daemon id
    _discover_systemd_daemon_id found."""
    first_mon = _first_mon_ip(nodes)
    hostnames: dict[str, str] = action_params.get("_node_hostnames", {})
    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))
    for i, node in enumerate(nodes):
        ip = node["ip"]
        hostname = hostnames.get(ip, ip)
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            execute_command(
                first_mon, f"ceph orch host add {shlex.quote(hostname)} {shlex.quote(ip)}"
            )
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"ceph orch host add {hostname} ({ip}) thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_convert_adopt_osds(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Per OSD host: discovers which OSD ids actually live there via
    `ceph-volume lvm list --format json` (its top-level keys are OSD ids —
    self-contained per-host discovery, no need to cross-reference `ceph osd
    tree`'s hostname strings against this app's own node list), then
    `cephadm adopt --style legacy --name osd.<id>` for each one found. Runs
    LAST among the 3 daemon types (after mon+mgr, per Ceph's own documented
    adoption order) — deliberately never invents/reassigns an OSD id, only
    adopts whatever ids ceph-volume already reports live on that host."""
    osd_nodes = [n for n in nodes if "osd" in (n.get("roles") or [])]
    host_status = [{"host": n["ip"], "status": "pending"} for n in osd_nodes]
    on_host_update(list(host_status))

    for i, node in enumerate(osd_nodes):
        ip = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            output = execute_command(ip, "ceph-volume lvm list --format json")
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(
                f"{ip}: không liệt kê được OSD cục bộ (ceph-volume lvm list): {exc}"
            ) from exc
        try:
            osd_map = json.loads(output) if output.strip() else {}
        except (TypeError, ValueError):
            osd_map = {}
        if not isinstance(osd_map, dict):
            osd_map = {}
        osd_ids = sorted(osd_map.keys(), key=lambda x: int(x) if x.isdigit() else x)

        if not osd_ids:
            host_status[i]["status"] = "done"
            host_status[i]["message"] = "Không có OSD nào trên node này"
            on_host_update(list(host_status))
            continue

        for osd_id in osd_ids:
            try:
                execute_command(
                    ip, f"cephadm adopt --style legacy --name {shlex.quote('osd.' + osd_id)}"
                )
            except ExecutorError as exc:
                host_status[i]["status"] = "failed"
                on_host_update(list(host_status))
                raise DeployPhaseError(f"{ip}: chuyển đổi osd.{osd_id} thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        host_status[i]["message"] = f"Đã chuyển đổi {len(osd_ids)} OSD: {', '.join(osd_ids)}"
        on_host_update(list(host_status))


def _clear_cluster_config() -> None:
    """Inverse of _write_cluster_config — after a successful cluster
    deletion, the Dashboard must stop trying to monitor a cluster that no
    longer exists. Same "must not turn a successful deletion into a
    reported FAILURE" posture run() already applies to _write_cluster_config
    below (see run()'s own comment)."""
    fields = {
        env_config.CLUSTER_ENV_NAMES["ceph_mon_nodes"]: "",
        env_config.CLUSTER_ENV_NAMES["ceph_mgr_nodes"]: "",
        env_config.CLUSTER_ENV_NAMES["ceph_osd_nodes"]: "",
        env_config.CLUSTER_ENV_NAMES["ceph_exec_mode"]: "none",
    }
    env_config.update_env_file_batch(fields)


# --- rpm-local-specific phase (Story 8.3) -----------------------------------
#
# The ONLY phase that differs from Story 8.2's ceph-deploy method (see the
# story file's Dev Notes) — every phase before and after this one
# (dependencies, packages, mon_init, wait_quorum, mgr_create, osd_create,
# verify) is Story 8.2's function, completely unchanged. Builds a local
# package repo directly out of `rpm_path` (already staged on every node
# beforehand, no scp/copy step — same posture as
# commands.py::_upgrade_ceph_cluster_package_local_command) so the
# downstream `_phase_ceph_deploy_packages` phase can keep installing
# ceph-mon/ceph-mgr/ceph-osd BY NAME exactly as it already does against the
# download.ceph.com repo, with zero changes to that phase.

_REMOTE_LOCAL_YUM_REPO_PATH = "/etc/yum.repos.d/ceph-aiops-local.repo"
_REMOTE_LOCAL_APT_LIST_PATH = "/etc/apt/sources.list.d/ceph-aiops-local.list"


def _phase_ceph_deploy_repo_local(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Per node: verify `rpm_path` exists and is non-empty (read-only check,
    AC #3), then build a local repo index against it (`createrepo`/
    `dpkg-scanpackages`) and point a local `.repo`/sources.list entry at it
    — NEVER the download.ceph.com repo. OS-family branching reuses
    `_package_manager_branch`'s inline `command -v` detection (same style
    every other package phase in this module already uses); its `else`
    branch already fails clearly rather than guessing for an OS family this
    engine can't build a local repo for."""
    rpm_path = action_params.get("rpm_path")
    if not rpm_path:
        raise DeployPhaseError("Chưa cấu hình đường dẫn thư mục RPM (rpm_path)")
    quoted_path = shlex.quote(rpm_path)

    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))

    for i, node in enumerate(nodes):
        host = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))

        try:
            execute_command(
                host, f'[ -d {quoted_path} ] && [ -n "$(ls -A {quoted_path} 2>/dev/null)" ]'
            )
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(
                f"Không tìm thấy thư mục RPM tại `{rpm_path}` trên node `{host}`"
            ) from exc

        apt_snippet = (
            "(command -v dpkg-scanpackages >/dev/null 2>&1 || "
            "(apt-get update -y && apt-get install -y dpkg-dev)) && "
            f"(cd {quoted_path} && dpkg-scanpackages . /dev/null 2>/dev/null | gzip -9c > Packages.gz) && "
            f"echo 'deb [trusted=yes] file:{rpm_path} ./' > {_REMOTE_LOCAL_APT_LIST_PATH} && "
            "apt-get update -y"
        )
        rpm_snippet = (
            "(command -v createrepo_c >/dev/null 2>&1 || command -v createrepo >/dev/null 2>&1 || "
            "(dnf install -y createrepo_c 2>/dev/null || yum install -y createrepo)) && "
            f"(createrepo_c {quoted_path} 2>/dev/null || createrepo {quoted_path}) && "
            "printf '[ceph-aiops-local]\\nname=Ceph AIOps Local RPM\\n"
            f"baseurl=file://{rpm_path}\\nenabled=1\\ngpgcheck=0\\n' "
            f"> {_REMOTE_LOCAL_YUM_REPO_PATH}"
        )
        repo_command = _package_manager_branch({"apt": apt_snippet, "rpm": rpm_snippet})
        try:
            execute_command(host, repo_command)
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{host}: cấu hình repo cục bộ thất bại: {exc}") from exc

        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


# step_key, label, progress %, phase function
_PHASES_BY_ACTION_ID: dict[str, list[tuple[str, str, int, object]]] = {
    "deploy_cluster_cephadm": [
        ("ssh_check", "Kiểm tra kết nối SSH & hệ thống", 10, _phase_ssh_check),
        ("dependencies", "Cài đặt phụ thuộc (chrony, tắt firewalld/SELinux)", 15, _phase_ceph_deploy_dependencies),
        ("bootstrap", "cephadm bootstrap", 55, _phase_cephadm_bootstrap),
        ("orch_host_add", "Thêm node vào cụm (orch host add)", 65, _phase_cephadm_orch_host_add),
        ("orch_apply_mgr", "Tạo MGR (orch apply mgr)", 70, _phase_cephadm_orch_apply_mgr),
        ("orch_apply_osd", "Tạo OSD (orch daemon add osd)", 85, _phase_cephadm_orch_apply_osd),
        ("verify", "Kiểm tra cluster health", 95, _phase_verify),
    ],
    "deploy_cluster_ceph_deploy": [
        ("ssh_check", "Kiểm tra kết nối SSH & hệ thống", 10, _phase_ssh_check),
        ("dependencies", "Cài đặt phụ thuộc (chrony, tắt firewalld/SELinux)", 15, _phase_ceph_deploy_dependencies),
        ("repo", "Cấu hình repo gói Ceph", 25, _phase_ceph_deploy_repo),
        ("packages", "Cài gói Ceph theo vai trò từng node", 40, _phase_ceph_deploy_packages),
        ("mon_init", "Khởi tạo MON (fsid, monmap, keyring, mkfs)", 55, _phase_ceph_deploy_mon_init),
        ("wait_quorum", "Chờ MON đạt quorum", 60, _phase_ceph_deploy_wait_quorum),
        ("mon_security", "Bật msgr2, tắt insecure global-id-reclaim", 62, _phase_ceph_deploy_mon_security),
        ("mgr_create", "Tạo MGR", 65, _phase_ceph_deploy_mgr_create),
        ("osd_create", "Tạo OSD (ceph-volume lvm create)", 80, _phase_ceph_deploy_osd_create),
        ("verify", "Kiểm tra cluster health", 95, _phase_verify),
    ],
    "deploy_cluster_rpm_local": [
        ("ssh_check", "Kiểm tra kết nối SSH & hệ thống", 10, _phase_ssh_check),
        ("dependencies", "Cài đặt phụ thuộc (chrony, tắt firewalld/SELinux)", 15, _phase_ceph_deploy_dependencies),
        ("repo_local", "Cấu hình repo cục bộ từ thư mục RPM", 25, _phase_ceph_deploy_repo_local),
        ("packages", "Cài gói Ceph theo vai trò từng node", 40, _phase_ceph_deploy_packages),
        ("mon_init", "Khởi tạo MON (fsid, monmap, keyring, mkfs)", 55, _phase_ceph_deploy_mon_init),
        ("wait_quorum", "Chờ MON đạt quorum", 60, _phase_ceph_deploy_wait_quorum),
        ("mon_security", "Bật msgr2, tắt insecure global-id-reclaim", 62, _phase_ceph_deploy_mon_security),
        ("mgr_create", "Tạo MGR", 65, _phase_ceph_deploy_mgr_create),
        ("osd_create", "Tạo OSD (ceph-volume lvm create)", 80, _phase_ceph_deploy_osd_create),
        ("verify", "Kiểm tra cluster health", 95, _phase_verify),
    ],
    # Same phase list as delete_cluster_manual below — verified live,
    # 2026-07-27: `cephadm rm-cluster` (the original implementation here)
    # is unreliable as a cluster-wide teardown even on a genuinely
    # cephadm-deployed cluster. Two real, separate problems found: (1) the
    # `cephadm` binary is only reliably present on first_mon (curl-installed
    # there directly) — on every OTHER host, cephadm's own orchestrator
    # manages daemons via a transient SSH-delivered agent, never leaving a
    # permanently-installed `cephadm` CLI a plain SSH session can find
    # afterward, so `command -v cephadm` genuinely fails there; (2) even ON
    # first_mon itself, `cephadm rm-cluster --force --zap-osds` zapped the
    # local OSD disk correctly but left the mon/mgr/crash containers
    # running — it did NOT tear down everything the docs suggest. The
    # generic systemctl-discovery + rm -rf + ceph-volume-zap approach
    # already proven for the manual/ceph-deploy method needs no per-host
    # `cephadm` binary at all and was hand-verified to fully clean a real
    # 3-node cephadm cluster (containers, systemd units, /etc/ceph,
    # /var/lib/ceph, and — with wipe_osd_disks — the LVM structures on
    # every node), so it's reused here unchanged rather than trying to fix
    # cephadm's own rm-cluster further.
    "delete_cluster_cephadm": [
        ("ssh_check", "Kiểm tra kết nối SSH", 10, _phase_delete_ssh_check),
        ("stop_daemons", "Dừng daemon Ceph trên từng node", 35, _phase_delete_manual_stop_daemons),
        (
            "remove_state",
            "Xoá cấu hình & dữ liệu Ceph (/etc/ceph, /var/lib/ceph)",
            60,
            _phase_delete_manual_remove_state,
        ),
        # 2026-07-28 fix (verified live): wipe_osd_disk MUST run before
        # remove_packages, not after — `ceph-volume lvm zap --destroy` is
        # provided by the very packages remove_packages uninstalls
        # (verified live: "bash: ceph-volume: command not found" once
        # remove_packages had already run first). Wiping the disk while the
        # tool that does it is still installed, then removing packages
        # last, is the only order that works.
        ("wipe_osd_disk", "Xoá dữ liệu đĩa OSD (nếu được chọn)", 80, _phase_delete_manual_wipe_osd_disk),
        ("remove_packages", "Gỡ cài đặt gói Ceph khỏi hệ điều hành", 95, _phase_delete_manual_remove_packages),
    ],
    "delete_cluster_manual": [
        ("ssh_check", "Kiểm tra kết nối SSH", 10, _phase_delete_ssh_check),
        ("stop_daemons", "Dừng daemon Ceph trên từng node", 35, _phase_delete_manual_stop_daemons),
        (
            "remove_state",
            "Xoá cấu hình & dữ liệu Ceph (/etc/ceph, /var/lib/ceph)",
            60,
            _phase_delete_manual_remove_state,
        ),
        # 2026-07-28 fix (verified live): wipe_osd_disk MUST run before
        # remove_packages, not after — `ceph-volume lvm zap --destroy` is
        # provided by the very packages remove_packages uninstalls
        # (verified live: "bash: ceph-volume: command not found" once
        # remove_packages had already run first). Wiping the disk while the
        # tool that does it is still installed, then removing packages
        # last, is the only order that works.
        ("wipe_osd_disk", "Xoá dữ liệu đĩa OSD (nếu được chọn)", 80, _phase_delete_manual_wipe_osd_disk),
        ("remove_packages", "Gỡ cài đặt gói Ceph khỏi hệ điều hành", 95, _phase_delete_manual_remove_packages),
    ],
    # Order matches Ceph's own documented adoption procedure: verify health
    # -> cephadm binary on every node -> adopt MON -> adopt MGR -> enable
    # orchestrator (needs a running, adopted MGR) -> distribute its SSH key
    # -> register every host -> adopt OSD (last, and only once the
    # orchestrator/host inventory is in place) -> final verify.
    "convert_cluster_to_cephadm": [
        ("ssh_check", "Kiểm tra kết nối SSH tới từng node", 5, _phase_convert_ssh_check),
        ("health_precheck", "Kiểm tra sức khoẻ cụm trước khi chuyển đổi", 10, _phase_verify),
        ("install_cephadm", "Cài đặt cephadm trên từng node", 25, _phase_convert_install_cephadm),
        ("adopt_mons", "Chuyển đổi từng MON sang cephadm", 40, _phase_convert_adopt_mons),
        ("adopt_mgrs", "Chuyển đổi từng MGR sang cephadm", 50, _phase_convert_adopt_mgrs),
        ("enable_orchestrator", "Bật cephadm orchestrator", 60, _phase_convert_enable_orchestrator),
        (
            "distribute_ssh_key",
            "Phân phối khoá SSH của cephadm tới từng node",
            70,
            _phase_convert_distribute_ssh_key,
        ),
        ("register_hosts", "Đăng ký từng node với orchestrator", 80, _phase_convert_register_hosts),
        ("adopt_osds", "Chuyển đổi từng OSD sang cephadm", 95, _phase_convert_adopt_osds),
        ("verify", "Kiểm tra cụm sau khi chuyển đổi", 100, _phase_verify),
    ],
}

# Deploy vs delete post-phase env-config writes go opposite directions
# (populate vs clear) — this set is how run() tells them apart without a
# separate parameter threaded through every call site.
_DELETE_CLUSTER_ACTION_IDS = frozenset({"delete_cluster_cephadm", "delete_cluster_manual"})


def _make_step(step_key: str, label: str, pct: int) -> dict:
    # started_at/finished_at (2026-07-28): plain UTC ISO strings, same "JSON
    # can't hold a datetime" posture as worker/llm/router_client.py's own
    # per-host progress dicts — set ONCE each (running/terminal transition
    # in run() below) and never touched again, so dashboard/routes/
    # deploy_cluster.py's live log can freeze a finished step's displayed
    # time instead of it drifting to "now" on every poll tick (that drift
    # was the actual bug: the frontend used to stamp EVERY line with the
    # browser's current clock on every render, regardless of that step's
    # real status).
    return {
        "step": step_key,
        "label": label,
        "pct": pct,
        "status": "pending",
        "hosts": [],
        "message": None,
        "started_at": None,
        "finished_at": None,
    }


def _write_cluster_config(action_params: dict, action_id: str) -> None:
    nodes = action_params.get("nodes") or []
    # convert_cluster_to_cephadm (2026-07-28): same node list as before
    # (mon/mgr/osd unchanged — this doesn't add/remove any node), only
    # CEPH_EXEC_MODE actually changes, from "none" to "cephadm".
    exec_mode = "cephadm" if action_id in ("deploy_cluster_cephadm", "convert_cluster_to_cephadm") else "none"
    fields = {
        env_config.CLUSTER_ENV_NAMES["ceph_mon_nodes"]: ",".join(_node_ips_with_role(nodes, "mon")),
        env_config.CLUSTER_ENV_NAMES["ceph_mgr_nodes"]: ",".join(_node_ips_with_role(nodes, "mgr")),
        env_config.CLUSTER_ENV_NAMES["ceph_osd_nodes"]: ",".join(_node_ips_with_role(nodes, "osd")),
        env_config.CLUSTER_ENV_NAMES["ceph_exec_mode"]: exec_mode,
    }
    env_config.update_env_file_batch(fields)


def run(
    action_pk: str,
    action_id: str,
    action_params: dict,
    incident_id: str,
    write_progress,
    check_kill_switch,
) -> bool:
    """Executes the ordered phase sequence for `action_id`, checking the
    kill-switch fresh before EACH phase (AD-4) and persisting STEP-shaped
    progress via `write_progress(action_pk, progress)` after every status
    change — same callback `worker/llm/router_client.py::_write_action_progress`
    already provides, reused unchanged.

    Returns True only if every phase completed successfully (the deploy is
    healthy) — the caller (`_execute_approved_action`) turns this into
    Action.status EXECUTED/FAILED the same way it already does for the
    generic per-host loop's own True/False result.
    """
    nodes = action_params.get("nodes") or []
    phases = _PHASES_BY_ACTION_ID.get(action_id)
    if not phases:
        logger.error("cluster_deploy.run: no phase sequence registered for action_id=%s", action_id)
        return False

    progress = [_make_step(key, label, pct) for key, label, pct, _fn in phases]
    write_progress(action_pk, progress)

    for index, (step_key, _label, _pct, fn) in enumerate(phases):
        if check_kill_switch(incident_id):
            progress[index]["status"] = "failed"
            progress[index]["message"] = "Kill-switch đang bật — dừng lại trước bước này"
            progress[index]["finished_at"] = datetime.utcnow().isoformat()
            write_progress(action_pk, progress)
            logger.warning(
                "cluster_deploy.run: kill-switch ON before phase %s, stopping action %s",
                step_key,
                action_pk,
            )
            return False

        progress[index]["status"] = "running"
        progress[index]["started_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)

        def _on_host_update(host_status, _index=index):
            progress[_index]["hosts"] = host_status
            write_progress(action_pk, progress)

        try:
            fn(nodes, action_params, _on_host_update)
        except DeployPhaseError as exc:
            progress[index]["status"] = "failed"
            progress[index]["message"] = str(exc)
            progress[index]["finished_at"] = datetime.utcnow().isoformat()
            write_progress(action_pk, progress)
            logger.warning(
                "cluster_deploy.run: phase %s failed for action %s: %s", step_key, action_pk, exc
            )
            return False
        except Exception as exc:
            progress[index]["status"] = "failed"
            progress[index]["message"] = f"Lỗi không mong đợi: {exc}"
            progress[index]["finished_at"] = datetime.utcnow().isoformat()
            write_progress(action_pk, progress)
            logger.exception(
                "cluster_deploy.run: unexpected error in phase %s for action %s", step_key, action_pk
            )
            return False

        progress[index]["status"] = "done"
        progress[index]["finished_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)

    try:
        if action_id in _DELETE_CLUSTER_ACTION_IDS:
            _clear_cluster_config()
        else:
            _write_cluster_config(action_params, action_id)
    except Exception:
        # The cluster itself is up and healthy (verify already passed) —
        # a failure writing the convenience .env shortcut must not turn a
        # successful deploy into a reported FAILURE; the operator can add
        # the cluster manually via Cài đặt afterward. Same posture applies
        # in reverse for a successful DELETE: the cluster is genuinely gone
        # (every phase above already succeeded) — failing to clear the
        # .env shortcut afterward must not turn that into a reported
        # FAILURE either; the operator can clear Cài đặt manually.
        logger.exception(
            "cluster_deploy.run: action succeeded but writing/clearing .env config failed for "
            "action %s",
            action_pk,
        )

    return True
