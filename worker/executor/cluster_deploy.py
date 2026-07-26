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

from shared import env_config
from shared.ceph_releases import codename_for_version
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
        f"mkdir -p {data_dir} && "
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
    auth later (same reasoning as COMMANDS["resync_ntp"])."""
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
    an older-but-still-supported OS.
    """
    apt_snippet = (
        "wget -q -O- https://download.ceph.com/keys/release.asc "
        "| gpg --dearmor -o /usr/share/keyrings/ceph-archive-keyring.gpg "
        f"&& echo \"deb [signed-by=/usr/share/keyrings/ceph-archive-keyring.gpg] "
        f"https://download.ceph.com/debian-{version}/ $(lsb_release -sc) main\" "
        "> /etc/apt/sources.list.d/ceph.list "
        "&& apt-get update -y"
    )
    rpm_snippet = (
        "rm -f /etc/yum.repos.d/download.ceph.com_rpm-*.repo "
        "&& rpm --import https://download.ceph.com/keys/release.asc "
        "&& (dnf config-manager --add-repo "
        f"https://download.ceph.com/rpm-{version}/el$(rpm -E %rhel)/$(uname -m)/ 2>/dev/null "
        "|| yum-config-manager --add-repo "
        f"https://download.ceph.com/rpm-{version}/el$(rpm -E %rhel)/$(uname -m)/) "
        "&& (dnf config-manager --add-repo "
        f"https://download.ceph.com/rpm-{version}/el$(rpm -E %rhel)/noarch/ 2>/dev/null "
        "|| yum-config-manager --add-repo "
        f"https://download.ceph.com/rpm-{version}/el$(rpm -E %rhel)/noarch/)"
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
    in commands.py's dispatch tables (see this story's Dev Notes)."""
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
        apt_snippet = f"apt-get install -y {package_list}"
        rpm_snippet = f"(dnf install -y {package_list} || yum install -y {package_list})"
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
        ("mgr_create", "Tạo MGR", 65, _phase_ceph_deploy_mgr_create),
        ("osd_create", "Tạo OSD (ceph-volume lvm create)", 80, _phase_ceph_deploy_osd_create),
        ("verify", "Kiểm tra cluster health", 95, _phase_verify),
    ],
}


def _make_step(step_key: str, label: str, pct: int) -> dict:
    return {"step": step_key, "label": label, "pct": pct, "status": "pending", "hosts": [], "message": None}


def _write_cluster_config(action_params: dict, action_id: str) -> None:
    nodes = action_params.get("nodes") or []
    exec_mode = "cephadm" if action_id == "deploy_cluster_cephadm" else "none"
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
            write_progress(action_pk, progress)
            logger.warning(
                "cluster_deploy.run: kill-switch ON before phase %s, stopping action %s",
                step_key,
                action_pk,
            )
            return False

        progress[index]["status"] = "running"
        write_progress(action_pk, progress)

        def _on_host_update(host_status, _index=index):
            progress[_index]["hosts"] = host_status
            write_progress(action_pk, progress)

        try:
            fn(nodes, action_params, _on_host_update)
        except DeployPhaseError as exc:
            progress[index]["status"] = "failed"
            progress[index]["message"] = str(exc)
            write_progress(action_pk, progress)
            logger.warning(
                "cluster_deploy.run: phase %s failed for action %s: %s", step_key, action_pk, exc
            )
            return False
        except Exception as exc:
            progress[index]["status"] = "failed"
            progress[index]["message"] = f"Lỗi không mong đợi: {exc}"
            write_progress(action_pk, progress)
            logger.exception(
                "cluster_deploy.run: unexpected error in phase %s for action %s", step_key, action_pk
            )
            return False

        progress[index]["status"] = "done"
        write_progress(action_pk, progress)

    try:
        _write_cluster_config(action_params, action_id)
    except Exception:
        # The cluster itself is up and healthy (verify already passed) —
        # a failure writing the convenience .env shortcut must not turn a
        # successful deploy into a reported FAILURE; the operator can add
        # the cluster manually via Cài đặt afterward.
        logger.exception(
            "cluster_deploy.run: deploy succeeded but writing .env config failed for action %s",
            action_pk,
        )

    return True
