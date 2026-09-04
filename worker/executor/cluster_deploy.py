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
import re
import shlex
import time
import uuid
from datetime import datetime, timedelta

from config.settings import settings
from shared import db, env_config
from shared.clusters import sync_default_cluster_from_env
from shared.ceph_releases import codename_for_version, major_version, repo_path_version
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import Cluster, Incident, NodeUpgradeGate, NodeUpgradeGateState
from shared.node_upgrade_gate import is_node_upgrade_gate_pending, release_node_upgrade_gate_lock
from worker.backup import metadata as backup_metadata
from worker.backup import restore as backup_restore
from worker.backup.policy_config import load_backup_policy
from worker.backup.storage.factory import get_backend
from worker.executor.commands import _package_manager_branch
from worker.executor.ssh_executor import ExecutorError, execute_command
from worker.policy.gate import VALID_CLUSTER_DEPLOY_ACTION_IDS

# Bounded MON-quorum poll (AC #2 item 6: a Python-level retry loop, not a
# bash `while`, so it's mockable in tests — see test_cluster_deploy.py).
# Overridable per-run via action_params["quorum_timeout_seconds"] (Task 1's
# "default 180s, configurable").
_QUORUM_POLL_INTERVAL_SECONDS = 5
_QUORUM_DEFAULT_TIMEOUT_SECONDS = 180

# Code-review fix (Story 11.4): same shape as dashboard/routes/upgrade.py's
# own _TARGET_VERSION_RE — a defense-in-depth format guard at the point
# _phase_gate_install_packages interpolates target_version into a shell
# command, independent of whether every Dashboard route that can set it
# already validates it.
_TARGET_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

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

# Story 9.7 (DR restore, Task 2) — scratch paths for metadata artifacts
# downloaded from the backup target and pushed to the first MON node.
_REMOTE_RESTORE_AUTH_PATH = "/tmp/ceph-aiops-restore-auth.txt"
_REMOTE_RESTORE_CRUSHMAP_PATH = "/tmp/ceph-aiops-restore-crushmap.bin"
_REMOTE_RESTORE_MONMAP_PATH = "/tmp/ceph-aiops-restore.monmap"

# Epic 11 (OS Upgrade Gate + Node OS Reinstall/Ceph Recovery) — cluster-wide
# maintenance flags set before a node's mon/osd are touched (FR-4) and
# cleared once no node is mid-flight anymore (FR-6/FR-16). Same 4 flags as
# the (independent, deliberately not reused — see their own docstrings)
# copies in watcher/ceph_client.py / worker/executor/commands.py /
# worker/llm/router_client.py.
_MAINTENANCE_FLAGS = ("noout", "noscrub", "nodeep-scrub", "nosnaptrim")

# Metadata backup (worker/backup/metadata.py, Epic 9) is considered "fresh
# enough" to skip an on-demand re-run if a successful one exists within
# this many hours (FR-3) — same RPO threshold worker/backup/alerting.py's
# own RPO_HOURS already uses for the unrelated "is a backup overdue" alert.
_METADATA_BACKUP_FRESHNESS_HOURS = 24

# 2026-07-29 fix (verified live + against the actual repo this app
# installs from): the 2026-07-28 version of this table added "ceph-volume"
# as its own separate package for BOTH package managers, based on Fedora
# Project's own package listing — but Fedora's own dnf repos are a
# DIFFERENT build/spec than download.ceph.com (the repo
# _build_ceph_package_repo_command below actually configures, and the only
# one this codebase ever installs from). Directly checked
# download.ceph.com's real directory listings for three major versions
# (rpm-nautilus/el8, rpm-quincy/el9, rpm-18.2.8/el9) — NONE of them ship a
# standalone `ceph-volume*.rpm`; `ceph-volume` was bundled inside
# `ceph-osd`'s own RPM there. Newer Reef EL9 repositories, however, do
# publish `ceph-volume` as a separate noarch RPM. Pinning
# "ceph-volume-<version>" for an RPM install therefore 404s ALWAYS ("Error:
# Unable to find a match: ceph-volume-14.2.22" — confirmed live on
# el8/Nautilus), not just for some versions.
#
# debian-quincy's own pool DOES ship a separate `ceph-volume_*.deb`
# distinct from `ceph-osd_*.deb`, though — genuinely a packaging
# difference between the two distros' upstream builds, not the same fact
# misapplied. Two separate tables below, one per package manager, instead
# of one shared list — the initial RPM install relies on ceph-osd, then the
# package phase installs standalone ceph-volume only when the executable is
# still absent. APT installs it explicitly in the initial transaction.
# "rgw" added alongside mon/mgr/osd (see this module's own RGW phases
# below) — package NAME differs by manager the same way it already does for
# ceph-volume above: upstream Ceph ships the RPM build as `ceph-radosgw`
# but the Debian build as plain `radosgw` (verified against download.ceph.com's
# real pool/repodata listings for both families), so this is a genuine
# packaging difference, not one table arbitrarily reusing the other's name.
_ROLE_TO_PACKAGES_RPM = {"mon": ("ceph-mon",), "mgr": ("ceph-mgr",), "osd": ("ceph-osd",), "rgw": ("ceph-radosgw",)}
_ROLE_TO_PACKAGES_APT = {
    "mon": ("ceph-mon",),
    "mgr": ("ceph-mgr",),
    "osd": ("ceph-osd", "ceph-volume"),
    "rgw": ("radosgw",),
}

# Paramiko's exec_command starts a non-login shell. On several EL/Rocky
# installations that shell's PATH does not contain /usr/sbin even though
# ceph-osd correctly installed ceph-volume there. Keep the host's existing
# PATH, but make the standard administrator binary directories explicit for
# every ceph-volume invocation/check. This avoids misdiagnosing a PATH issue
# as a missing package (exit 127) and does not pull an unversioned package
# from a different repository.
_CEPH_VOLUME_PATH_PREFIX = "export PATH=/usr/local/sbin:/usr/sbin:/sbin:$PATH"

# Standalone RGW default: one shared service id/port for every RGW node
# this module creates, matching the traditional `ceph-deploy rgw create`
# default (beast frontend, port 7480 — the well-known RGW default port,
# distinct from 80/443 so it never collides with anything else that might
# already be listening on an RGW node). Fixed rather than operator-
# configurable for now, same "no dedicated field for this yet" posture as
# osd_pool_default_size's own simple int fields — an operator who needs a
# different port/realm/zone can still change it by hand via `ceph orch apply
# rgw`/ceph.conf afterward, same as any other post-deploy tuning this
# feature doesn't expose.
_RGW_SVC_ID = "default"
_RGW_PORT = 7480

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
            osd_disks = node.get("osd_disks") or []
            if not osd_disks:
                host_status[i]["status"] = "failed"
                on_host_update(list(host_status))
                raise DeployPhaseError(f"{host}: chưa cấu hình đĩa OSD (osd_disks)")
            # One node can carry multiple OSD disks (e.g. node1
            # /dev/vdc+/dev/vdd) — each is checked independently so a
            # failure names the EXACT disk at fault, not just the host.
            for osd_disk in osd_disks:
                try:
                    _check_osd_disk_safe(host, osd_disk)
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

    # 2026-07-28 (verified live against download.ceph.com, twice): the
    # intermediate fix here — installing `cephadm` as a normal dnf/apt
    # package via our own repo command — turned out to depend on
    # download.ceph.com actually publishing THIS version for THIS node's
    # OS, which it may not: a real CentOS Stream 8 node targeting reef
    # (18.2.8) hit a 404 for BOTH the exact-version AND the codename-alias
    # el8 path — Ceph simply never built reef (or quincy) for el8 at all,
    # confirmed by checking the real directory listings, not something a
    # smarter repo-URL fallback can work around. Back to curl-fetching the
    # standalone `cephadm` SCRIPT (one static, OS/arch-agnostic Python
    # file — its own fixed el9/noarch download path works regardless of
    # the TARGET node's actual OS, since nothing about that path involves
    # installing anything ON el8) and using it directly — no `cephadm
    # add-repo`/`cephadm install` step at all, which is what actually
    # needed a same-version-and-OS package repo to exist.
    install_cephadm = (
        "command -v cephadm >/dev/null 2>&1 || "
        f"(curl -fsSL https://download.ceph.com/rpm-{codename}/el9/noarch/cephadm "
        "-o /usr/local/bin/cephadm && chmod +x /usr/local/bin/cephadm)"
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
    try:
        execute_command(first_mon, cleanup_previous_attempt)
        execute_command(first_mon, f"{install_cephadm} && {bootstrap}")
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
                first_mon,
                f"cephadm shell -- ceph orch host add {shlex.quote(hostname)} {shlex.quote(ip)}",
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
        execute_command(
            first_mon,
            f"cephadm shell -- ceph orch apply mgr --placement={shlex.quote(placement)}",
        )
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
        # Per-node disk LIST (AC: mỗi node OSD có thể dùng nhiều đĩa khác
        # nhau, vd node1 /dev/vdc + /dev/vdd, node2 /dev/vdb) — already
        # validated non-empty at propose time, but the ssh_check phase's
        # read-only safety check is what actually proved EACH disk
        # safe-to-use on THIS host, so re-reading it fresh from `node` here
        # (not a single cluster-wide value) is what keeps that guarantee
        # meaningful per-host. `ceph orch daemon add osd` only ever takes
        # ONE device per call — one call per disk.
        osd_disks = node.get("osd_disks") or []
        if not osd_disks:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{ip}: chưa cấu hình đĩa OSD (osd_disks)")
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        current_disk = osd_disks[0]
        try:
            for osd_disk in osd_disks:
                current_disk = osd_disk
                # Explicit device — never --all-available-devices — so only
                # the operator's chosen osd_disk is ever touched (already
                # proven safe-to-use by the ssh_check phase's read-only disk
                # check).
                execute_command(
                    first_mon,
                    f"cephadm shell -- ceph orch daemon add osd "
                    f"{shlex.quote(hostname)}:{shlex.quote(osd_disk)}",
                )
        except ExecutorError as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(
                f"Tạo OSD trên {hostname} ({current_disk}) thất bại: {exc}"
            ) from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_cephadm_orch_apply_rgw(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """RGW is OPTIONAL, unlike mon/mgr/osd above — a node table with zero
    "rgw"-role nodes is a perfectly valid cluster (object storage is opt-in),
    so this phase no-ops (empty host list, no error) rather than raising the
    "Không có node ... nào trong cấu hình" DeployPhaseError every other
    role-specific phase in this module raises for an EMPTY required role."""
    first_mon = _first_mon_ip(nodes)
    hostnames: dict[str, str] = action_params.get("_node_hostnames", {})
    rgw_ips = _node_ips_with_role(nodes, "rgw")
    if not rgw_ips:
        on_host_update([])
        return
    placement = ",".join(hostnames.get(ip, ip) for ip in rgw_ips)

    host_status = [{"host": first_mon, "status": "running"}]
    on_host_update(list(host_status))
    try:
        execute_command(
            first_mon,
            f"cephadm shell -- ceph orch apply rgw {_RGW_SVC_ID} "
            f"--placement={shlex.quote(placement)} "
            f"--port={_RGW_PORT}",
        )
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"'ceph orch apply rgw' thất bại: {exc}") from exc
    host_status[0]["status"] = "done"
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


def _read_remote_file_b64(host: str, path: str, action_params: dict | None = None) -> str:
    try:
        return _gate_execute(host, f"base64 {shlex.quote(path)}", action_params or {}).strip()
    except ExecutorError as exc:
        raise DeployPhaseError(f"{host}: không đọc được {path}: {exc}") from exc


def _write_remote_file_b64(
    host: str, path: str, content_b64: str, action_params: dict | None = None
) -> None:
    """Writes base64-decoded bytes to `path` on `host`, creating its parent
    directory first — binary-safe (keyrings/monmaps), unlike a plain heredoc,
    same base64-over-exec_command trick `commands.py::_patch_build_and_stage_command`
    already uses for the same reason (no SFTP anywhere in this codebase —
    see ssh_executor.py's docstring)."""
    quoted_path = shlex.quote(path)
    quoted_dir = shlex.quote(os.path.dirname(path))
    cmd = f"mkdir -p {quoted_dir} && base64 -d > {quoted_path} <<< {shlex.quote(content_b64)}"
    try:
        _gate_execute(host, cmd, action_params or {})
    except ExecutorError as exc:
        raise DeployPhaseError(f"{host}: không ghi được {path}: {exc}") from exc


def _write_remote_file(host: str, path: str, content: str) -> None:
    _write_remote_file_b64(host, path, base64.b64encode(content.encode()).decode())


def _build_ceph_conf(
    action_params: dict,
    mon_nodes: list[dict],
    hostnames: dict[str, str],
    fsid: str,
    rgw_nodes: list[dict] | None = None,
) -> str:
    """One shared ceph.conf pushed to every node (mon/mgr/osd/rgw alike) —
    lists ALL mon nodes (mon_host, mon initial members) plus one
    [mon.<hostname>] section PER mon with that mon's OWN public_addr set
    explicitly (avoids the "wrong NIC picked" failure mode on multi-homed
    lab nodes — see the story file's Dev Notes). A non-mon node simply
    never uses the [mon.*] sections that aren't its own — same reasoning
    now applies to the [client.rgw.<hostname>] sections added below for
    `rgw_nodes` (optional — RGW is opt-in, unlike mon/mgr/osd), each
    pinned to the traditional/default beast port 7480 (see _RGW_PORT)."""
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
    for n in rgw_nodes or []:
        hostname = hostnames.get(n["ip"], n["ip"])
        lines.append(f"[client.rgw.{hostname}]")
        lines.append(f"host = {hostname}")
        lines.append(f'rgw frontends = "beast port={_RGW_PORT}"')
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
    _run_dependency_install(nodes, on_host_update, install_container_runtime=False)


def _phase_ceph_deploy_dependencies_cephadm(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Same as `_phase_ceph_deploy_dependencies` above, PLUS a container
    runtime on every node -- cephadm's own preflight check (run by
    `cephadm bootstrap` in the next phase) hard-refuses to proceed unless
    podman or docker is already installed, and neither this app nor
    `cephadm bootstrap` itself ever installs one. Verified live, 2026-08-10:
    a fresh CentOS Stream 9 node has neither by default, so `deploy_cluster_
    cephadm` failed outright at the bootstrap phase with no earlier phase
    having tried to fix it.

    Applies to EVERY node in this list, not just first_mon -- the
    orchestrator's own per-host agent (installed later by `orch_host_add`)
    needs a container runtime on every host it will ever place a MON/MGR/
    OSD/RGW daemon on, not only the bootstrap node.

    Only wired into `deploy_cluster_cephadm`'s phase list -- the
    `ceph_deploy`/`rpm_local` methods install Ceph as plain native systemd
    services, never touch a container runtime, and must not gain an
    unrelated podman/docker install they never asked for."""
    _run_dependency_install(nodes, on_host_update, install_container_runtime=True)


def _run_dependency_install(nodes: list[dict], on_host_update, *, install_container_runtime: bool) -> None:
    install_command = _build_base_dependency_install_command(install_container_runtime=install_container_runtime)

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

    Prefers the exact VERSION over the release codename's rolling alias to
    build the repo URL — same fix `commands.py::_upgrade_ceph_cluster_package_download_command`
    already made (2026-07-24, verified live): the codename alias (e.g.
    rpm-quincy/) only ever carries the OS versions the LATEST point release
    of that codename still supports, silently becoming an empty/404 repo for
    an older-but-still-supported OS. EXCEPT Nautilus (see
    `shared/ceph_releases.py::repo_path_version`'s docstring, verified live
    2026-07-27): download.ceph.com never published a per-exact-version
    directory for Nautilus at all, only the codename alias.

    2026-07-28 fix (verified live): the reverse gap also happens — a real
    CentOS Stream 8 node hit a 404 fetching `rpm-18.2.8/el8/noarch/
    repodata/repomd.xml` (dnf then refuses to do ANYTHING while any
    enabled repo fails metadata refresh, not just the Ceph install this
    was for — a later Ceph point release can drop support for an OS an
    EARLIER point release of that same codename still built for, the
    opposite direction from the "codename alias only has the latest OS
    set" problem the exact-version preference above already solves). The
    RPM branch now probes the exact-version noarch repodata URL first and
    falls back to the codename alias if that 404s, rather than trusting
    either blindly — never adds a repo it hasn't confirmed actually
    resolves, and fails loudly with a clear reason if NEITHER does (e.g.
    genuinely no build exists for this OS at all, a real "pick a different
    version or OS" situation, not something to keep guessing at).
    """
    repo_path = repo_path_version(version)
    codename = codename_for_version(version)
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
        "&& rhel_ver=$(rpm -E %rhel) "
        f"&& ceph_repo_path='' "
        f"&& for candidate in {shlex.quote(repo_path)} {shlex.quote(codename or repo_path)}; do "
        "curl -fsSL -o /dev/null "
        "\"https://download.ceph.com/rpm-$candidate/el$rhel_ver/noarch/repodata/repomd.xml\" "
        "&& ceph_repo_path=\"$candidate\" && break; done; "
        # ";" not "&&" before this `if` on purpose: the `for` loop's own
        # exit status is whatever its LAST iteration's command returned —
        # when EVERY candidate 404s, that's curl's failure code, which
        # would otherwise short-circuit this whole check via && and skip
        # straight to the raw curl error, hiding the actually useful
        # message below (verified: without this fix, "both candidates
        # failed" surfaced as a bare `curl` exit code, never this echo).
        "if [ -z \"$ceph_repo_path\" ]; then "
        f"echo \"No Ceph RPM repo found for el$rhel_ver at version {shlex.quote(repo_path)} or "
        f"codename {shlex.quote(codename or repo_path)}\" >&2; exit 1; fi "
        "&& (dnf config-manager --add-repo "
        "\"https://download.ceph.com/rpm-$ceph_repo_path/el$rhel_ver/$(uname -m)/\" 2>/dev/null "
        "|| yum-config-manager --add-repo "
        "\"https://download.ceph.com/rpm-$ceph_repo_path/el$rhel_ver/$(uname -m)/\") "
        "&& (dnf config-manager --add-repo "
        "\"https://download.ceph.com/rpm-$ceph_repo_path/el$rhel_ver/noarch/\" 2>/dev/null "
        "|| yum-config-manager --add-repo "
        "\"https://download.ceph.com/rpm-$ceph_repo_path/el$rhel_ver/noarch/\")"
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


_CEPH_CONFLICT_RPM_GLOBS = (
    "'ceph-*' 'librados2*' 'librbd1*' 'librgw2*' 'libradosstriper1*' 'libcephfs2*' "
    "'python*-rados' 'python*-rbd' 'python*-cephfs' 'python*-rgw' 'python*-ceph-argparse'"
)
_CEPH_CONFLICT_APT_GLOBS = (
    "'ceph*' 'librados2*' 'librbd1*' 'radosgw*' 'python3-rados*' 'python3-rbd*' 'python3-cephfs*'"
)


def _remove_conflicting_ceph_install_snippet(version: str) -> str:
    """Real-world case this fixes (2026-08-06): a lab node reused from an
    earlier manual/ceph-deploy install (or an earlier attempt at a
    DIFFERENT version through this same tool) already had ceph-common
    installed when `_phase_ceph_deploy_packages` below tried to install a
    different major version on top — yum/dnf's dependency resolver then
    gets stuck between the OLD version's already-installed librados2/etc
    and the NEW ceph-common's own librados2 requirement, surfacing as an
    unreadable wall of "Requires:/Available:/Installed:" lines instead of
    the install just working.

    Prepended (via `&&`/`;`, see call site) to the real install command so
    the two run as ONE ssh round trip per node: if ceph-common is already
    installed and its version differs from the one we're about to install,
    force-removes every ceph-related package (glob covers the actual
    NEVRAs seen in that failure: ceph-*, librados2, librbd1, librgw2,
    libradosstriper1, libcephfs2, and the python bindings) plus any
    leftover `ceph.repo`/`ceph-deploy.repo` file that might still be
    enabled and feeding the SAME conflict back into the next attempt.
    Never touches anything if ceph-common isn't installed, or is already
    the exact version being installed (nothing to fix — removing and
    reinstalling the same version would be pure churn, and this must stay
    a no-op on an already-correct node so re-running this phase is safe).
    Deliberately swallows the removal's own exit status (`|| true` /
    trailing `2>/dev/null`) — a partial removal failure must not block the
    fresh install that follows from even attempting to run."""
    quoted_version = shlex.quote(version)
    rpm_cleanup = (
        "existing=$(rpm -q --qf '%{VERSION}\\n' ceph-common 2>/dev/null | head -1); "
        f'if [ -n "$existing" ] && [ "$existing" != {quoted_version} ]; then '
        f"(yum remove -y {_CEPH_CONFLICT_RPM_GLOBS} 2>/dev/null "
        f"|| dnf remove -y {_CEPH_CONFLICT_RPM_GLOBS} 2>/dev/null || true); "
        "rm -f /etc/yum.repos.d/ceph.repo /etc/yum.repos.d/ceph-deploy.repo; fi"
    )
    apt_cleanup = (
        "existing=$(dpkg-query -W -f='${Version}\\n' ceph-common 2>/dev/null | head -1); "
        f'if [ -n "$existing" ] && [ "${{existing%%-*}}" != {quoted_version} ]; then '
        f"apt-get remove -y --purge {_CEPH_CONFLICT_APT_GLOBS} 2>/dev/null || true; "
        "rm -f /etc/apt/sources.list.d/ceph.list; fi"
    )
    return _package_manager_branch({"apt": apt_cleanup, "rpm": rpm_cleanup})


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
        roles = node.get("roles") or []
        apt_packages = sorted({pkg for role in roles for pkg in _ROLE_TO_PACKAGES_APT.get(role, ())})
        rpm_packages = sorted({pkg for role in roles for pkg in _ROLE_TO_PACKAGES_RPM.get(role, ())})
        if not apt_packages and not rpm_packages:
            host_status[i]["status"] = "done"
            host_status[i]["message"] = "không có vai trò mon/mgr/osd — bỏ qua"
            on_host_update(list(host_status))
            continue

        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        apt_package_list = " ".join(apt_packages)
        rpm_package_list = (
            " ".join(f"{pkg}-{version}" for pkg in rpm_packages) if pin_exact_version else " ".join(rpm_packages)
        )
        apt_snippet = f"apt-get install -y {apt_package_list}"
        rpm_snippet = f"(dnf install -y {rpm_package_list} || yum install -y {rpm_package_list})"
        install_command = _package_manager_branch({"apt": apt_snippet, "rpm": rpm_snippet})
        # Auto-cleanup FIRST, same SSH round trip: removes a conflicting
        # pre-existing Ceph install (different major version) so the
        # install right after doesn't hit the cross-version librados2 wall
        # — see _remove_conflicting_ceph_install_snippet's own docstring.
        full_command = f"{_remove_conflicting_ceph_install_snippet(version)} && {install_command}"
        if "osd" in roles:
            # Fail in the package phase with an actionable message instead
            # of reaching the destructive OSD phase and failing with the
            # opaque `bash: ceph-volume: command not found` error. On RPM
            # systems older Ceph repos bundle ceph-volume in ceph-osd while
            # newer Reef EL9 repos publish a standalone noarch RPM. Install
            # that RPM only when ceph-osd did not provide the executable, so
            # both repository layouts remain supported. On Debian it is the
            # separate package already included in apt_packages above.
            full_command += (
                f" && {_CEPH_VOLUME_PATH_PREFIX}"
                " && if command -v rpm >/dev/null 2>&1"
                "; then (command -v ceph-volume >/dev/null 2>&1"
                " || dnf install -y ceph-volume || yum install -y ceph-volume)"
                "; fi"
                " && (command -v ceph-volume >/dev/null 2>&1"
                " || { echo 'ceph-volume is missing after installing Ceph OSD packages' >&2; exit 127; })"
            )
        # Only used for progress display text — the ACTUAL install command
        # correctly differs per package manager above (apt_package_list
        # includes ceph-volume, rpm_package_list never does).
        package_list = " ".join(sorted(set(apt_packages) | set(rpm_packages)))
        try:
            execute_command(host, full_command)
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

    rgw_nodes = [n for n in nodes if "rgw" in (n.get("roles") or [])]

    fsid = str(uuid.uuid4())
    ceph_conf = _build_ceph_conf(action_params, mon_nodes, hostnames, fsid, rgw_nodes)
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
    just a config write).

    2026-08-06 fix (verified live against a real Mimic 13.2.10 mon —
    "no valid command found... mon rm/mon add/mon dump/..." for `ceph mon
    enable-msgr2`, EINVAL): this phase was written when Nautilus (14.x) was
    the OLDEST deployable release, where both commands have always existed
    — "safe to always run" was true THEN. Mimic (13.x) support was added
    later (shared/ceph_releases.py) without revisiting this assumption.
    msgr2 and the insecure-global-id-reclaim config knob were both
    introduced in Nautilus; Mimic's `ceph mon` has neither command at all,
    so running this against a Mimic cluster fails the whole deploy on a
    step that doesn't even apply to it. Skip entirely for major version <
    14."""
    version = action_params.get("version", "")
    if (major := major_version(version)) is not None and major < 14:
        on_host_update([{"host": "n/a", "status": "done"}])
        return

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
        # Per-node disk LIST (vd node1 /dev/vdc + /dev/vdd, node2 /dev/vdb) —
        # see the cephadm phase's own osd_disks comment for why this is read
        # fresh per node rather than one cluster-wide value. One
        # `ceph-volume lvm create` call PER disk — it creates exactly one
        # OSD per invocation, no built-in multi-device batch mode that keeps
        # this codebase's "explicit device, never --all-available-devices"
        # safety posture.
        osd_disks = node.get("osd_disks") or []
        if not osd_disks:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{ip}: chưa cấu hình đĩa OSD (osd_disks)")
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        current_disk = osd_disks[0]
        try:
            _write_remote_file(ip, _REMOTE_CEPH_CONF_PATH, ceph_conf)
            _write_remote_file_b64(ip, _REMOTE_BOOTSTRAP_OSD_KEYRING_PATH, bootstrap_osd_keyring_b64)
            for osd_disk in osd_disks:
                current_disk = osd_disk
                # Explicit device — never --all-available-devices — same
                # safety posture as the cephadm phase's own OSD-creation
                # step; osd_disk was already proven empty/unmounted by the
                # ssh_check phase's read-only check before ANY phase
                # (including this one) ran.
                execute_command(
                    ip,
                    f"{_CEPH_VOLUME_PATH_PREFIX} && "
                    f"ceph-volume lvm create --data {shlex.quote(osd_disk)}",
                )
        except (ExecutorError, DeployPhaseError) as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"Tạo OSD trên {ip} ({current_disk}) thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


def _phase_ceph_deploy_rgw_create(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """RGW is OPTIONAL (see _phase_cephadm_orch_apply_rgw's own docstring —
    same posture here) — no-ops if no node has the "rgw" role. Otherwise
    mirrors _phase_ceph_deploy_mgr_create's own shape exactly (auth
    get-or-create on first_mon -> fetch keyring -> push ceph.conf+keyring to
    the target node -> systemctl enable --now), the traditional/manual
    equivalent of `ceph-deploy rgw create`: keyring at
    /var/lib/ceph/radosgw/ceph-rgw.<hostname>/keyring, capability
    `osd 'allow rwx' mon 'allow rw'` (the standard client.rgw.* caps Ceph's
    own manual-deployment docs use), unit `ceph-radosgw@rgw.<hostname>` —
    already matched by commands.py's `_UNIT_TYPE_MARKERS` "rgw" substring
    classification, so upgrade/restart tooling elsewhere in this codebase
    discovers it correctly with no further change needed there. The
    `[client.rgw.<hostname>]` section (host + rgw_frontends port 7480) was
    already baked into the shared `_ceph_conf` blob by `_build_ceph_conf`
    during mon_init, so this phase only needs to push that same blob, same
    as mgr_create/osd_create already do."""
    mon_nodes = [n for n in nodes if "mon" in (n.get("roles") or [])]
    if not mon_nodes:
        raise DeployPhaseError("Không có node MON nào trong cấu hình")
    first_mon_ip = mon_nodes[0]["ip"]
    hostnames: dict[str, str] = action_params.get("_node_hostnames", {})
    rgw_nodes = [n for n in nodes if "rgw" in (n.get("roles") or [])]
    if not rgw_nodes:
        on_host_update([])
        return

    ceph_conf = action_params.get("_ceph_conf")
    if not ceph_conf:
        raise DeployPhaseError("Thiếu ceph.conf từ bước khởi tạo MON — không thể tạo RGW")

    host_status = [{"host": n["ip"], "status": "pending"} for n in rgw_nodes]
    on_host_update(list(host_status))

    for i, node in enumerate(rgw_nodes):
        ip = node["ip"]
        hostname = hostnames.get(ip, ip)
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            remote_tmp_keyring = f"/tmp/ceph-aiops-rgw-{hostname}.keyring"
            execute_command(
                first_mon_ip,
                f"ceph auth get-or-create client.rgw.{shlex.quote(hostname)} "
                f"osd 'allow rwx' mon 'allow rw' "
                f"-o {shlex.quote(remote_tmp_keyring)}",
            )
            keyring_b64 = _read_remote_file_b64(first_mon_ip, remote_tmp_keyring)

            rgw_dir = f"/var/lib/ceph/radosgw/ceph-rgw.{hostname}"
            _write_remote_file(ip, _REMOTE_CEPH_CONF_PATH, ceph_conf)
            _write_remote_file_b64(ip, f"{rgw_dir}/keyring", keyring_b64)
            execute_command(
                ip,
                f"(chown -R ceph:ceph {shlex.quote(rgw_dir)} 2>/dev/null || true) && "
                f"systemctl enable --now ceph-radosgw@rgw.{shlex.quote(hostname)}",
            )
        except (ExecutorError, DeployPhaseError) as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{ip}: tạo/khởi động RGW thất bại: {exc}") from exc
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

    if health not in {"HEALTH_OK", "HEALTH_WARN"}:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(
            f"Không xác minh được sức khoẻ cụm sau khi dựng (health={health!r}) — dừng lại"
        )

    host_status[0]["status"] = "done"
    host_status[0]["message"] = health or "unknown"
    on_host_update(list(host_status))


def _phase_cephadm_verify(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Verify through cephadm's container; do not require host ceph-common."""
    first_mon = _first_mon_ip(nodes)
    host_status = [{"host": first_mon, "status": "running"}]
    on_host_update(list(host_status))

    try:
        output = execute_command(first_mon, "cephadm shell -- ceph -s --format json")
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"Không lấy được trạng thái cụm: {exc}") from exc

    try:
        health = json.loads(output).get("health", {}).get("status")
    except (TypeError, ValueError, AttributeError):
        health = None

    if health not in {"HEALTH_OK", "HEALTH_WARN"}:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(
            f"Không xác minh được sức khoẻ cụm sau khi dựng (health={health!r}) — dừng lại"
        )

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
    deploy.

    2026-07-29, "xoá mọi thứ liên quan tới Ceph" (operator request): three
    more categories of leftover this app's own phases create, none of them
    under /etc/ceph or /var/lib/ceph so the rm above never touched them —
    (3) /usr/local/bin/cephadm — a plain curl-downloaded script
    (_phase_cephadm_bootstrap/_phase_convert_install_cephadm), never
    package-managed, so remove_packages' `dnf/apt remove ceph*` below can
    never touch it either; left behind, `command -v cephadm` keeps
    succeeding on a node this app just called "deleted". (4) /var/log/ceph
    — daemon logs, not covered by any of the above. (5) cephadm's own
    generated systemd unit FILES (`ceph-<fsid>@.service` template,
    `ceph-<fsid>.target`, and their .wants symlinks) — the stop_daemons
    phase before this one only stops+disables units it can already find
    via `systemctl`, which drops the enablement symlink but leaves the
    unit FILE itself in /etc/systemd/system, so it keeps showing up in
    `systemctl list-units` indefinitely; `systemctl daemon-reload`
    afterward makes systemd actually forget them immediately instead of
    only at next boot. Still never touches an operator's own repo files
    (different names), any other systemd unit, or anything under
    /etc/ceph, /var/lib/ceph beyond what's already covered above."""
    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))
    unmount_then_remove = (
        "for m in $(awk '$2 ~ /^\\/var\\/lib\\/ceph\\// {print $2}' /proc/mounts); do "
        "umount \"$m\" 2>/dev/null || umount -l \"$m\" 2>/dev/null; done; "
        "rm -rf /etc/ceph /var/lib/ceph /var/log/ceph /tmp/ceph-aiops* /usr/local/bin/cephadm; "
        "rm -f /etc/yum.repos.d/download.ceph.com_rpm-*.repo /etc/yum.repos.d/ceph-aiops-local.repo "
        "/etc/apt/sources.list.d/ceph.list /etc/apt/sources.list.d/ceph-aiops-local.list; "
        "rm -rf /etc/systemd/system/ceph-*.target /etc/systemd/system/ceph-*.target.wants "
        "/etc/systemd/system/ceph-*@*.service* /etc/systemd/system/multi-user.target.wants/ceph-*; "
        "systemctl daemon-reload 2>/dev/null || true"
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


_ZAP_CONNECT_RETRY_ATTEMPTS = 3
_ZAP_CONNECT_RETRY_DELAY_SECONDS = 5


def _execute_zap_with_connect_retry(host: str, command: str) -> str:
    """execute_command()'s own docstring: no retry, fresh SSH connection
    every call. Verified live, 2026-08-10: on a host with 2+ OSD disks,
    `ceph-volume lvm zap --destroy` is heavy I/O (wipefs + LVM teardown +
    kernel partition-table re-read) -- the FRESH connection execute_command()
    opens for the NEXT disk, right after the previous disk's zap just
    finished, can hit paramiko's own transient `SSHException("No existing
    session")` (raised when auth is attempted before the transport's key
    exchange has settled) while the host is still busy. With zero retry,
    that single transient hiccup used to abort the entire multi-node "Xoá
    cụm" job outright -- every other pending node stuck "⏳" forever, never
    even attempted. A host with only 1 OSD disk (1 connection) rarely hits
    this, which is why it only ever showed up "khi có 2 osd".

    Only retries a CONNECTION/transport-level ExecutorError -- its message
    never contains "command exited" (that substring is exclusive to a real
    non-zero exit from the remote command, see execute_command's own
    f-string) -- a genuine zap failure (bad device path, disk busy) still
    fails immediately on the first attempt, never masked by 3 retries of
    something that would keep failing the same way."""
    last_error: ExecutorError | None = None
    for attempt in range(1, _ZAP_CONNECT_RETRY_ATTEMPTS + 1):
        try:
            return execute_command(host, command)
        except ExecutorError as exc:
            if "command exited" in str(exc):
                raise
            last_error = exc
            if attempt < _ZAP_CONNECT_RETRY_ATTEMPTS:
                time.sleep(_ZAP_CONNECT_RETRY_DELAY_SECONDS)
    raise last_error


def _phase_delete_manual_wipe_osd_disk(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Only touches a disk if the operator explicitly opted into
    wipe_osd_disks — otherwise every host is marked done immediately
    without a single command sent, so this phase's presence in the fixed
    phase list is harmless for an operator who chose NOT to wipe anything.
    `ceph-volume lvm zap --destroy` (not a bare wipefs) — the same tool
    _phase_ceph_deploy_osd_create used to CREATE the OSD, so it correctly
    tears down the LVM structures ceph-volume itself created, not just the
    device's leading bytes.

    2026-07-28 fix (verified live): for a cephadm-deployed cluster, native
    `ceph-volume` is NEVER installed on ANY host by this codebase (OSDs are
    created via `ceph orch apply osd` entirely inside containers cephadm
    manages, and even first_mon's own ceph-common install doesn't provide
    it — confirmed live: "ceph-volume: command not found" on first_mon
    itself, not just an OSD-only host) — this module's own earlier "hand-
    verified to fully clean a real 3-node cephadm cluster" claim for THIS
    exact command was wrong (or from a since-changed code path). Now tries
    native `ceph-volume` first (still the right, unchanged path for the
    manual/ceph-deploy method, where it genuinely is installed), falling
    back to `cephadm ceph-volume --` (runs it inside a container cephadm
    spins up using the local `/var/lib/ceph/<fsid>` state it auto-detects
    — which is why `delete_cluster_cephadm`'s phase list now runs this
    BEFORE remove_state, unlike delete_cluster_manual: remove_state
    deletes that exact directory, and cephadm needs it to still be there).
    Orchestrator-added hosts may only have a copied
    `/var/lib/ceph/<fsid>/cephadm.<hash>` script rather than cephadm in PATH;
    the fallback selects the newest such script and runs it with python3.
    """
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
        osd_disks = node.get("osd_disks") or []
        if not osd_disks:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{ip}: chưa cấu hình đĩa OSD cần xoá (osd_disks)")
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        for osd_disk in osd_disks:
            quoted_disk = shlex.quote(osd_disk)
            zap_command = (
                "if command -v ceph-volume >/dev/null 2>&1; then "
                f"ceph-volume lvm zap --destroy {quoted_disk}; "
                "elif command -v cephadm >/dev/null 2>&1; then "
                f"cephadm ceph-volume -- lvm zap --destroy {quoted_disk}; "
                "else cephadm_script=$(find /var/lib/ceph -mindepth 2 -maxdepth 2 -type f "
                "-name 'cephadm.*' -printf '%T@ %p\\n' 2>/dev/null "
                "| sort -nr | sed -n '1s/^[^ ]* //p'); "
                "if [ -n \"$cephadm_script\" ]; then "
                f"python3 \"$cephadm_script\" ceph-volume -- lvm zap --destroy {quoted_disk}; "
                "else echo 'no ceph-volume (native or via cephadm) found' >&2; exit 1; fi; fi"
            )
            try:
                _execute_zap_with_connect_retry(ip, zap_command)
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
    that daemon) — same curl-fetched standalone script
    _phase_cephadm_bootstrap uses for first_mon (see that function's own
    2026-07-28 comment for why: installing cephadm as an OS package
    requires download.ceph.com to publish THIS exact version for THIS
    node's OS, which a real CentOS Stream 8 node targeting reef proved
    false — the curl-fetched script is one static, OS/arch-agnostic Python
    file, sidestepping that entirely), applied here to every node instead.
    Deliberately does NOT install a container runtime (docker/podman) —
    same assumption every OTHER phase in this module already makes
    (cephadm bootstrap itself requires one pre-installed; this codebase
    has never automated that installation, see this module's own
    docstring)."""
    version = action_params.get("version", "")
    codename = codename_for_version(version)
    if codename is None:
        raise DeployPhaseError(f"Không tìm thấy mã tên release Ceph cho phiên bản {version!r}")

    install_cephadm = (
        "command -v cephadm >/dev/null 2>&1 || "
        f"(curl -fsSL https://download.ceph.com/rpm-{codename}/el9/noarch/cephadm "
        "-o /usr/local/bin/cephadm && chmod +x /usr/local/bin/cephadm)"
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


def _cephadm_image_for_version(version: str) -> str:
    """The container image `cephadm adopt`/`cephadm ceph-volume` should be
    pinned to via `cephadm --image <this> ...` — WITHOUT this, cephadm
    silently defaults to whatever the LATEST build tagged for that
    codename currently is on quay.io, which can be (and, verified live,
    WAS) newer than the exact version actually running natively on the
    not-yet-adopted daemons: a real conversion adopted mon+mgr onto
    17.2.8 while OSDs stayed native 17.2.5, leaving `ceph versions`
    permanently mixed with no way to converge except upgrading the OSDs
    afterward. Pinning here means every daemon this conversion adopts
    ends up on the EXACT SAME version already running, matching the
    detected `action_params["version"]` this whole conversion is
    predicated on (see convert_cluster.py's propose route)."""
    return f"quay.io/ceph/ceph:v{version}"


def _cephadm_managed_daemon_ids(ip: str, daemon_type: str) -> set[str]:
    """Queries `cephadm ls` on `ip` (a LOCAL, per-host listing of what
    cephadm itself already manages there — not a cluster-wide query) for
    every `daemon_type` daemon already adopted. Makes adoption resumable:
    a real, live-verified scenario is a conversion that adopted mon+mgr,
    then failed at a LATER phase (enable_orchestrator) — an operator who
    finishes those later steps by hand and then re-runs this feature must
    not have mon/mgr adoption re-attempted, since there's no native
    systemd unit left to discover for them anymore (already renamed by
    the first adoption) — that used to fail with a confusing "systemd
    unit not found" instead of recognizing the step is already done.

    2026-07-28 fix (verified live): this used to pass `--format json` —
    `cephadm ls` doesn't accept that flag at all ("error: unrecognized
    arguments: --format json") and always prints JSON regardless. The
    broken flag made every call fail, which the `except ExecutorError:
    return set()` below swallowed silently — so this always reported
    "nothing adopted yet", even when it plainly was, defeating the whole
    point of this function. Now uses `--no-detail` instead (same flag
    watcher/collector.py and commands.py already use for the identical
    reason: it returns exactly name/fsid/style/systemd_unit per daemon —
    all this function reads — skipping the heavier per-daemon container/
    memory stats `cephadm ls` includes by default).

    Returns an empty set (not an error) if cephadm isn't installed yet or
    the command genuinely fails — the correct/safe assumption for a
    genuinely fresh conversion attempt.

    2026-07-28 fix (verified live): `cephadm ls` lists EVERY Ceph daemon
    it can discover on the host, including ones it does NOT manage yet —
    a still-native, not-yet-adopted OSD shows up with `"style": "legacy"`
    and its real `"name": "osd.<id>"` right there alongside genuinely
    cephadm-managed daemons (`"style": "cephadm:v1"`). This function used
    to match on `name` alone, so it treated a legacy (unadopted) OSD as
    "already converted" and skipped adopting it — the whole OSD-adoption
    phase silently no-opped, reporting success, while `ceph versions`
    stayed mixed. Only `style == "cephadm:v1"` counts as actually
    adopted."""
    try:
        output = execute_command(ip, "cephadm ls --no-detail 2>/dev/null")
    except ExecutorError:
        return set()
    try:
        entries = json.loads(output) if output.strip() else []
    except (TypeError, ValueError):
        return set()
    if not isinstance(entries, list):
        return set()
    prefix = daemon_type + "."
    ids: set[str] = set()
    for entry in entries:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and entry["name"].startswith(prefix)
            and entry.get("style") == "cephadm:v1"
        ):
            ids.add(entry["name"][len(prefix) :])
    return ids


def _adopt_daemon_on_host(ip: str, service_prefix: str, daemon_type: str, image: str) -> tuple[str, bool]:
    """Shared by adopt_mons/adopt_mgrs below. Returns (daemon_id,
    already_adopted) — already_adopted=True means _cephadm_managed_daemon_ids
    found this daemon type ALREADY cephadm-managed on `ip` (a resumed
    conversion, see that function's own comment), so NOTHING was sent to
    this host this call. Otherwise discovers the real daemon id via
    _discover_systemd_daemon_id and runs `cephadm --image <image> adopt
    --style legacy --name <daemon_type>.<id>` (see
    _cephadm_image_for_version's own comment for why `--image` is pinned
    explicitly). Raises DeployPhaseError if no matching systemd unit is
    found at all, or if the adopt command itself fails."""
    already = _cephadm_managed_daemon_ids(ip, daemon_type)
    if already:
        return sorted(already)[0], True
    daemon_id = _discover_systemd_daemon_id(ip, service_prefix)
    if not daemon_id:
        raise DeployPhaseError(f"{ip}: không tìm thấy systemd unit {service_prefix}@* đang chạy")
    try:
        execute_command(
            ip,
            f"cephadm --image {shlex.quote(image)} adopt --style legacy "
            f"--name {shlex.quote(daemon_type + '.' + daemon_id)}",
        )
    except ExecutorError as exc:
        raise DeployPhaseError(f"{ip}: chuyển đổi {daemon_type}.{daemon_id} thất bại: {exc}") from exc
    return daemon_id, False


def _phase_convert_adopt_mons(nodes: list[dict], action_params: dict, on_host_update) -> None:
    mon_nodes = [n for n in nodes if "mon" in (n.get("roles") or [])]
    if not mon_nodes:
        raise DeployPhaseError("Không có node MON nào trong cấu hình")
    image = _cephadm_image_for_version(action_params.get("version", ""))
    host_status = [{"host": n["ip"], "status": "pending"} for n in mon_nodes]
    on_host_update(list(host_status))
    for i, node in enumerate(mon_nodes):
        ip = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            mon_id, already_adopted = _adopt_daemon_on_host(ip, "ceph-mon", "mon", image)
        except DeployPhaseError:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise
        host_status[i]["status"] = "done"
        host_status[i]["message"] = f"mon.{mon_id}" + (" (đã chuyển đổi từ trước)" if already_adopted else "")
        on_host_update(list(host_status))


def _phase_convert_adopt_mgrs(nodes: list[dict], action_params: dict, on_host_update) -> None:
    mgr_nodes = [n for n in nodes if "mgr" in (n.get("roles") or [])]
    if not mgr_nodes:
        raise DeployPhaseError("Không có node MGR nào trong cấu hình")
    image = _cephadm_image_for_version(action_params.get("version", ""))
    host_status = [{"host": n["ip"], "status": "pending"} for n in mgr_nodes]
    on_host_update(list(host_status))
    for i, node in enumerate(mgr_nodes):
        ip = node["ip"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))
        try:
            mgr_id, already_adopted = _adopt_daemon_on_host(ip, "ceph-mgr", "mgr", image)
        except DeployPhaseError:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise
        host_status[i]["status"] = "done"
        host_status[i]["message"] = f"mgr.{mgr_id}" + (" (đã chuyển đổi từ trước)" if already_adopted else "")
        on_host_update(list(host_status))


def _phase_convert_enable_orchestrator(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Enables the cephadm mgr module + routes the orchestrator to it, on
    the just-adopted MGR's host — needed before `ceph orch host add`/`ceph
    orch ps` (later phases) mean anything. `ceph cephadm generate-key`
    ensures the orchestrator's own dedicated SSH keypair exists (bootstrap
    generates one automatically as part of its own setup; adoption never
    calls bootstrap, so this is the equivalent explicit step) — harmless if
    a key already exists.

    2026-07-28 fix (verified live): `ceph mgr module enable cephadm`
    (no flags) failed right after adopt_mgrs with "Error ENOENT: all mgr
    daemons do not support module 'cephadm', pass --force to force
    enablement" — the mon's module-capability cache for the just-adopted/
    just-restarted mgr daemon hadn't caught up yet, a known, Ceph-
    documented race (the error message itself names the workaround).
    `--force` is exactly what Ceph's own error suggests here, not a risky
    workaround invented for this codebase — safe specifically because we
    just adopted this exact mgr ourselves and know it's the real thing."""
    first_mon = _first_mon_ip(nodes)
    host_status = [{"host": first_mon, "status": "running"}]
    on_host_update(list(host_status))
    try:
        execute_command(
            first_mon,
            "ceph mgr module enable cephadm --force && ceph orch set backend cephadm && "
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
    SIDE EFFECT of `cephadm bootstrap` itself, which adoption never runs.

    2026-07-28 fix (verified live): this used to SKIP first_mon itself,
    on the (wrong) assumption that `cephadm bootstrap` self-authorizes its
    own host the same way — it does, but adoption never runs bootstrap at
    all, so nothing had ever added cephadm's key to first_mon's OWN
    authorized_keys. The very next phase (`ceph orch host add`) runs
    INSIDE the orchestrator's own container on first_mon and needs to SSH
    back out to EVERY host it manages, including itself — failed with
    "Error EINVAL: Failed to connect to <first_mon> ... Permission
    denied" until first_mon got the same treatment as every other host."""
    first_mon = _first_mon_ip(nodes)
    host_status = [{"host": n["ip"], "status": "pending"} for n in nodes]
    on_host_update(list(host_status))

    try:
        cephadm_pubkey = execute_command(first_mon, "ceph cephadm get-pub-key").strip()
    except ExecutorError as exc:
        raise DeployPhaseError(
            f"{first_mon}: không lấy được khoá SSH của cephadm (ceph cephadm get-pub-key): {exc}"
        ) from exc
    if not cephadm_pubkey:
        raise DeployPhaseError(f"{first_mon}: ceph cephadm get-pub-key trả về rỗng")

    for i, node in enumerate(nodes):
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
    `cephadm --image <pinned> adopt --style legacy --name osd.<id>` for
    each one found (see _cephadm_image_for_version's own comment for why
    the image is pinned to the detected cluster version explicitly, not
    left to cephadm's own default — same reasoning applies to OSDs as to
    mon/mgr). Runs LAST among the 3 daemon types (after mon+mgr, per
    Ceph's own documented adoption order) — deliberately never invents/
    reassigns an OSD id, only adopts whatever ids ceph-volume already
    reports live on that host. Resumable, same _cephadm_managed_daemon_ids
    check as _adopt_daemon_on_host — an id already cephadm-managed on this
    host (e.g. a previous partial run, or manual cleanup by the operator)
    is skipped rather than re-adopted."""
    image = _cephadm_image_for_version(action_params.get("version", ""))
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

        # 2026-07-28: resumable, same reasoning as _adopt_daemon_on_host's
        # own comment — a resumed conversion must not re-attempt adopting
        # an OSD id cephadm already manages on this host.
        already_adopted_ids = _cephadm_managed_daemon_ids(ip, "osd")
        ids_to_adopt = [osd_id for osd_id in osd_ids if osd_id not in already_adopted_ids]

        for osd_id in ids_to_adopt:
            try:
                execute_command(
                    ip,
                    f"cephadm --image {shlex.quote(image)} adopt --style legacy "
                    f"--name {shlex.quote('osd.' + osd_id)}",
                )
            except ExecutorError as exc:
                host_status[i]["status"] = "failed"
                on_host_update(list(host_status))
                raise DeployPhaseError(f"{ip}: chuyển đổi osd.{osd_id} thất bại: {exc}") from exc
        host_status[i]["status"] = "done"
        skipped_count = len(osd_ids) - len(ids_to_adopt)
        if ids_to_adopt:
            message = f"Đã chuyển đổi {len(ids_to_adopt)} OSD: {', '.join(ids_to_adopt)}"
            if skipped_count:
                message += f" (bỏ qua {skipped_count} OSD đã chuyển đổi từ trước)"
        else:
            message = f"Tất cả {len(osd_ids)} OSD đã được chuyển đổi từ trước"
        host_status[i]["message"] = message
        on_host_update(list(host_status))


def _phase_convert_verify(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Same `ceph -s` HEALTH_ERR check as _phase_verify, PLUS (2026-07-28,
    added after the legacy-vs-cephadm:v1 style bug reached production) a
    genuine per-daemon completeness check. That bug let every adopt phase
    report "already converted" for a daemon that was actually still
    native, so the whole action finished with status="done" on every
    step — and run() unconditionally flips CEPH_EXEC_MODE to "cephadm"
    right after this phase passes. Once written, that wrong config makes
    every LATER Convert-to-Cephadm attempt refuse outright ("cụm hiện tại
    đã chạy cephadm rồi"), even though some daemons genuinely still
    aren't adopted — a config-level lie that's awkward to unwind (the
    operator has to go flip CEPH_EXEC_MODE back via Cài đặt just to retry).
    This phase re-derives ground truth independently right before that
    write happens, so a future regression of the same shape fails loudly
    here instead of writing a config that contradicts reality."""
    _phase_verify(nodes, action_params, on_host_update)

    for node in nodes:
        ip = node["ip"]
        roles = node.get("roles") or []
        if "mon" in roles and not _cephadm_managed_daemon_ids(ip, "mon"):
            raise DeployPhaseError(f"{ip}: MON vẫn chưa được cephadm quản lý sau khi chuyển đổi")
        if "mgr" in roles and not _cephadm_managed_daemon_ids(ip, "mgr"):
            raise DeployPhaseError(f"{ip}: MGR vẫn chưa được cephadm quản lý sau khi chuyển đổi")
        if "osd" not in roles:
            continue
        try:
            output = execute_command(ip, "ceph-volume lvm list --format json")
        except ExecutorError:
            continue
        try:
            osd_map = json.loads(output) if output.strip() else {}
        except (TypeError, ValueError):
            osd_map = {}
        if not isinstance(osd_map, dict) or not osd_map:
            continue
        missing = set(osd_map.keys()) - _cephadm_managed_daemon_ids(ip, "osd")
        if missing:
            raise DeployPhaseError(
                f"{ip}: OSD {', '.join(sorted(missing))} vẫn chưa được cephadm quản lý sau khi chuyển đổi"
            )


def _clear_cluster_config() -> None:
    """Inverse of _write_cluster_config — after a successful cluster
    deletion, the Dashboard must stop trying to monitor a cluster that no
    longer exists. Same "must not turn a successful deletion into a
    reported FAILURE" posture run() already applies to _write_cluster_config
    below (see run()'s own comment). Clears ceph_rgw_nodes too — leaving a
    stale RGW IP behind after the cluster (and that host's daemon) is gone
    would leave a dangling entry in shared/cluster_nodes.py's own SSH SSRF
    whitelist, which several unrelated features (bucket_access_log.py,
    Chat-with-AI tool loop) read from directly."""
    fields = {
        env_config.CLUSTER_ENV_NAMES["ceph_mon_nodes"]: "",
        env_config.CLUSTER_ENV_NAMES["ceph_mgr_nodes"]: "",
        env_config.CLUSTER_ENV_NAMES["ceph_osd_nodes"]: "",
        env_config.CLUSTER_ENV_NAMES["ceph_rgw_nodes"]: "",
        env_config.CLUSTER_ENV_NAMES["ceph_exec_mode"]: "none",
        env_config.CLUSTER_ENV_NAMES["ceph_keyring_path"]: _REMOTE_ADMIN_KEYRING_PATH,
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


# --- Khôi phục cụm sau thảm họa (Story 9.7, `restore_cluster_from_backup`) --
#
# 3 phases appended AFTER `deploy_cluster_ceph_deploy`'s own phase list
# (see `_PHASES_BY_ACTION_ID` below) — by the time these run, the cluster
# is already freshly bootstrapped and HEALTHY (that phase list ends with
# `_phase_verify`). These restore application-level state (auth/CRUSH
# map/RBD images) on top of it; they never touch MON bootstrap/quorum,
# which the reused phases already handled.


def _restore_monmap_on_mon(host: str, hostname: str, monmap_b64: str) -> None:
    """Stops the mon, injects the OLD cluster's monmap, restarts it — done
    ONE mon at a time (not in parallel) so the cluster never loses quorum
    entirely mid-restore. NOT verified against a real lab cluster this
    session (Dev Notes explicitly flag this): the downloaded monmap.bin
    carries the ORIGINAL cluster's fsid, while this freshly-bootstrapped
    mon's on-disk store already has a DIFFERENT fsid from
    `_phase_ceph_deploy_mon_init` above — Ceph is documented to reject an
    `--inject-monmap` whose fsid doesn't match the local store's fsid, and
    that behavior can differ by version. If this step fails in practice,
    mon membership already matches the backup (the operator was told by
    the runbook to rebuild with the SAME node list), so it is safe to
    treat this specific sub-step as best-effort — logged as a phase
    failure here rather than silently ignored, so an operator investigates
    rather than assumes the whole restore silently skipped it."""
    quoted_hostname = shlex.quote(hostname)
    _write_remote_file_b64(host, _REMOTE_RESTORE_MONMAP_PATH, monmap_b64)
    execute_command(
        host,
        f"systemctl stop ceph-mon@{quoted_hostname} && "
        f"ceph-mon -i {quoted_hostname} --inject-monmap {shlex.quote(_REMOTE_RESTORE_MONMAP_PATH)} && "
        f"systemctl start ceph-mon@{quoted_hostname}",
    )


def _phase_restore_metadata(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Story 9.7, Task 2 (AC #2) — restores auth keys, CRUSH map, and mon
    membership from the latest successful metadata backup (Story 9.3)."""
    first_mon = _first_mon_ip(nodes)
    mon_nodes = [n for n in nodes if "mon" in (n.get("roles") or [])]
    hostnames: dict[str, str] = action_params.get("_node_hostnames", {})

    host_status = [{"host": first_mon, "status": "running"}]
    on_host_update(list(host_status))

    latest = backup_metadata.latest_successful_metadata_job()
    if latest is None:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError("Không có bản backup metadata thành công nào để khôi phục")

    try:
        manifest = backup_metadata.artifact_manifest(latest.id)
        missing = [name for name in backup_metadata._RESTORE_ARTIFACTS if name not in manifest]
        if missing:
            raise DeployPhaseError(
                f"Backup metadata {latest.id} thiếu manifest cho: {', '.join(missing)}"
            )
        backend = get_backend(latest.backup_target_slot, settings)
        auth_size, auth_sha256 = manifest["auth_export.txt"]
        crushmap_size, crushmap_sha256 = manifest["crushmap.bin"]
        monmap_size, monmap_sha256 = manifest["monmap.bin"]
        auth_bytes = backup_metadata.download_artifact(
            backend, latest.remote_key, "auth_export.txt", auth_size, auth_sha256
        )
        crushmap_bytes = backup_metadata.download_artifact(
            backend, latest.remote_key, "crushmap.bin", crushmap_size, crushmap_sha256
        )
        monmap_bytes = backup_metadata.download_artifact(
            backend, latest.remote_key, "monmap.bin", monmap_size, monmap_sha256
        )
    except Exception as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"Không tải được bản backup metadata ({latest.remote_key}): {exc}") from exc

    try:
        _write_remote_file_b64(first_mon, _REMOTE_RESTORE_AUTH_PATH, base64.b64encode(auth_bytes).decode())
        execute_command(first_mon, f"ceph auth import -i {shlex.quote(_REMOTE_RESTORE_AUTH_PATH)}")

        _write_remote_file_b64(
            first_mon, _REMOTE_RESTORE_CRUSHMAP_PATH, base64.b64encode(crushmap_bytes).decode()
        )
        execute_command(first_mon, f"ceph osd setcrushmap -i {shlex.quote(_REMOTE_RESTORE_CRUSHMAP_PATH)}")
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{first_mon}: khôi phục auth/CRUSH map thất bại: {exc}") from exc

    monmap_b64 = base64.b64encode(monmap_bytes).decode()
    for mon_node in mon_nodes:
        ip = mon_node["ip"]
        hostname = hostnames.get(ip, ip)
        try:
            _restore_monmap_on_mon(ip, hostname, monmap_b64)
        except (ExecutorError, DeployPhaseError) as exc:
            logger.warning(
                "cluster_deploy._phase_restore_metadata: inject monmap thất bại trên %s (%s) — "
                "best-effort, xem docstring _restore_monmap_on_mon: %s",
                ip,
                hostname,
                exc,
            )

    host_status[0]["status"] = "done"
    host_status[0]["message"] = f"Đã khôi phục auth + CRUSH map từ {latest.remote_key}"
    on_host_update(list(host_status))


def _phase_restore_rbd_images(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Story 9.7, Task 2 (AC #3) — restores every configured
    `tracked_images` entry (`worker/policy/backup_policy.yaml`) via
    `worker/backup/restore.py::restore_image()`, the SAME shared restore
    path Task 3's `restore_rbd_image_to_production` uses. Progress here is
    tracked per-image (not per-host, unlike every other phase in this
    module) — `_make_step`'s `hosts` field is reused unchanged, just
    carrying `{pool}/{image}` labels instead of IPs."""
    first_mon = _first_mon_ip(nodes)
    tracked = [t for t in (load_backup_policy().get("tracked_images") or []) if t.get("pool") and t.get("image")]

    host_status = [{"host": f"{t['pool']}/{t['image']}", "status": "pending"} for t in tracked]
    on_host_update(list(host_status))
    if not tracked:
        return

    try:
        existing_pools = {p.strip() for p in execute_command(first_mon, "ceph osd pool ls").splitlines()}
    except ExecutorError as exc:
        host_status[:] = [{**s, "status": "failed"} for s in host_status]
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{first_mon}: không liệt kê được pool hiện có: {exc}") from exc

    for i, t in enumerate(tracked):
        pool, image = t["pool"], t["image"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))

        if pool not in existing_pools:
            try:
                execute_command(
                    first_mon, f"ceph osd pool create {shlex.quote(pool)} && rbd pool init {shlex.quote(pool)}"
                )
                existing_pools.add(pool)
            except ExecutorError as exc:
                host_status[i]["status"] = "failed"
                on_host_update(list(host_status))
                raise DeployPhaseError(f"{first_mon}: tạo pool {pool} thất bại: {exc}") from exc

        slot = backup_restore.latest_backup_target_slot(pool, image)
        if slot is None:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{pool}/{image}: không có bản backup full thành công để khôi phục")

        backend = get_backend(slot, settings)
        result = backup_restore.restore_image(pool, image, backend, pool, image)
        if not result.success:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{pool}/{image}: khôi phục thất bại: {result.error_message}")

        host_status[i]["status"] = "done"
        host_status[i]["message"] = f"{result.size_bytes} bytes, {len(result.applied_diff_job_ids)} diff(s)"
        on_host_update(list(host_status))


def _phase_verify_integrity(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """Story 9.7, Task 2 (AC #4) — for every restored image, confirms its
    logical size on the NEW cluster matches the full backup's own
    recorded `size_bytes` exactly (a plain `rbd export` of a whole image
    writes the same number of bytes as the image's logical size, so this
    is a real, not approximate, integrity check — not "gần đúng" per AC
    #4's own wording). Per-artifact download integrity was already
    enforced by `restore.py::restore_image()`'s `storage.verify()` round-
    trip check during `_phase_restore_rbd_images` above; this phase is the
    final end-to-end confirmation against the rebuilt image itself."""
    first_mon = _first_mon_ip(nodes)
    tracked = [t for t in (load_backup_policy().get("tracked_images") or []) if t.get("pool") and t.get("image")]

    host_status = [{"host": f"{t['pool']}/{t['image']}", "status": "pending"} for t in tracked]
    on_host_update(list(host_status))
    if not tracked:
        return

    for i, t in enumerate(tracked):
        pool, image = t["pool"], t["image"]
        host_status[i]["status"] = "running"
        on_host_update(list(host_status))

        full_job = backup_restore.latest_full_backup_job(pool, image)
        if full_job is None or full_job.size_bytes is None:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{pool}/{image}: không có kích thước backup gốc để đối chiếu")

        try:
            output = execute_command(first_mon, f"rbd info {pool}/{image} --format json")
            restored_size = json.loads(output)["size"]
        except (ExecutorError, KeyError, ValueError, TypeError) as exc:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{pool}/{image}: không lấy được kích thước sau khôi phục: {exc}") from exc

        if restored_size != full_job.size_bytes:
            host_status[i]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(
                f"{pool}/{image}: kích thước sau khôi phục ({restored_size}) không khớp bản backup "
                f"full ({full_job.size_bytes}) — checksum KHÔNG khớp"
            )

        host_status[i]["status"] = "done"
        on_host_update(list(host_status))


# step_key, label, progress %, phase function
_PHASES_BY_ACTION_ID: dict[str, list[tuple[str, str, int, object]]] = {
    "deploy_cluster_cephadm": [
        ("ssh_check", "Kiểm tra kết nối SSH & hệ thống", 10, _phase_ssh_check),
        ("dependencies", "Cài đặt phụ thuộc (chrony, podman/docker, tắt firewalld/SELinux)", 15, _phase_ceph_deploy_dependencies_cephadm),
        ("bootstrap", "cephadm bootstrap", 55, _phase_cephadm_bootstrap),
        ("orch_host_add", "Thêm node vào cụm (orch host add)", 65, _phase_cephadm_orch_host_add),
        ("orch_apply_mgr", "Tạo MGR (orch apply mgr)", 70, _phase_cephadm_orch_apply_mgr),
        ("orch_apply_osd", "Tạo OSD (orch daemon add osd)", 85, _phase_cephadm_orch_apply_osd),
        ("orch_apply_rgw", "Tạo RGW (orch apply rgw, nếu có node RGW)", 90, _phase_cephadm_orch_apply_rgw),
        ("verify", "Kiểm tra cluster health", 95, _phase_cephadm_verify),
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
        ("rgw_create", "Tạo RGW (radosgw, nếu có node RGW)", 90, _phase_ceph_deploy_rgw_create),
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
        ("rgw_create", "Tạo RGW (radosgw, nếu có node RGW)", 90, _phase_ceph_deploy_rgw_create),
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
    # already proven for the manual/ceph-deploy method was reused here —
    # containers/systemd units/`/etc/ceph`/`/var/lib/ceph` teardown all
    # hand-verified fine on a real 3-node cephadm cluster. The
    # wipe_osd_disks=True case specifically was NOT actually exercised in
    # that verification despite this comment previously claiming otherwise
    # — a real run found native `ceph-volume` genuinely absent everywhere
    # on a cephadm cluster (not just OSD-only hosts — first_mon too), fixed
    # 2026-07-28 in _phase_delete_manual_wipe_osd_disk's own comment.
    # 2026-07-28 fix (verified live): wipe_osd_disk runs BEFORE remove_state
    # here — unlike delete_cluster_manual below — because
    # _phase_delete_manual_wipe_osd_disk's cephadm fallback
    # (`cephadm ceph-volume -- ...`, needed since native ceph-volume is
    # never installed anywhere in a cephadm deployment — see that
    # function's own comment) auto-detects the cluster's fsid from the
    # local `/var/lib/ceph/<fsid>` directory, which remove_state deletes.
    # Running wipe first means that directory (and /etc/ceph) still exist
    # when cephadm needs them.
    "delete_cluster_cephadm": [
        ("ssh_check", "Kiểm tra kết nối SSH", 10, _phase_delete_ssh_check),
        ("stop_daemons", "Dừng daemon Ceph trên từng node", 35, _phase_delete_manual_stop_daemons),
        ("wipe_osd_disk", "Xoá dữ liệu đĩa OSD (nếu được chọn)", 55, _phase_delete_manual_wipe_osd_disk),
        (
            "remove_state",
            "Xoá cấu hình & dữ liệu Ceph (/etc/ceph, /var/lib/ceph)",
            80,
            _phase_delete_manual_remove_state,
        ),
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
        ("verify", "Kiểm tra cụm sau khi chuyển đổi", 100, _phase_convert_verify),
    ],
}

# Story 9.7 (DR restore) — reuses `deploy_cluster_ceph_deploy`'s ENTIRE
# phase list unchanged (list concatenation via `+`, not copy-pasted — per
# this story's own Dev Notes) to first rebuild an empty, healthy cluster,
# then appends 3 restore-only phases on top of it.
_PHASES_BY_ACTION_ID["restore_cluster_from_backup"] = _PHASES_BY_ACTION_ID["deploy_cluster_ceph_deploy"] + [
    ("restore_metadata", "Khôi phục auth/CRUSH map/monmap", 97, _phase_restore_metadata),
    ("restore_rbd_images", "Khôi phục dữ liệu RBD từ backup", 99, _phase_restore_rbd_images),
    ("verify_integrity", "Đối chiếu checksum sau khôi phục", 100, _phase_verify_integrity),
]

# --- Epic 11 (OS Upgrade Gate + Node OS Reinstall/Ceph Recovery) —
# node_os_gate_prepare (Story 11.3, FR-3/4/5) -------------------------------
#
# `action_params` contract for every node_os_gate_* phase below (AD-17):
# {"host", "target_version", "roles" (list[str] — SAME UPPERCASE
# "MON"/"OSD"/"MGR"/"RGW" values shared.cluster_nodes.configured_nodes()
# returns, NOT the lowercase dict-list "roles" convention the deploy/
# delete/convert/restore phases ABOVE this point use), "nodes": [host] (a
# ONE-ELEMENT list of the bare host string, never a node dict — "nodes is
# always [host]", AD-17), "node_upgrade_gate_id", "action_pk",
# "incident_id"}. Every phase looks its NodeUpgradeGate row up via
# action_params["node_upgrade_gate_id"] ONLY — never by host + ordering
# heuristic (Reviewer Gate finding #3's root cause).
#
# This is the FIRST place in this file that touches the application DB
# directly (shared.db / shared.models.NodeUpgradeGate) — every phase above
# this point is pure SSH-orchestration-plus-action_params.
#
# Only reachable when settings.ceph_exec_mode == "none" (the Gate screen,
# Story 11.1, only ever renders from a ceph_exec_mode="none"-only route) —
# every `ceph ...` call below runs directly via execute_command, with NO
# docker/cephadm wrapping, matching this file's own pre-existing
# _phase_ceph_deploy_wait_quorum/_phase_ceph_deploy_mon_security.


def _osd_role(action_params: dict) -> bool:
    return "OSD" in (action_params.get("roles") or [])


def _mon_role(action_params: dict) -> bool:
    return "MON" in (action_params.get("roles") or [])


def _gate_cluster(action_params: dict) -> Cluster | None:
    return action_params.get("_gate_cluster")


def _gate_cluster_id(action_params: dict) -> str | None:
    cluster = _gate_cluster(action_params)
    return cluster.id if cluster is not None else None


def _gate_nodes(action_params: dict) -> list[dict]:
    cluster = _gate_cluster(action_params)
    return configured_nodes() if cluster is None else configured_nodes(cluster)


def _gate_execute(host: str, command: str, action_params: dict) -> str:
    cluster = _gate_cluster(action_params)
    if cluster is None:
        return execute_command(host, command)
    user, key_path, _exec_mode, _container = resolve_ssh_creds(cluster)
    return execute_command(host, command, user=user, key_path=key_path)


def _cluster_for_gate_action(incident_id: str) -> Cluster | None:
    """Resolve the gate's authoritative cluster once in the Worker.

    ``None`` is the legacy/default cluster and intentionally keeps using the
    environment-backed settings. Non-default actions must have a live
    cluster row; silently falling back to default would be cross-cluster SSH.
    """
    with db.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        if incident is None:
            raise DeployPhaseError(
                f"Không tìm thấy Incident của NodeUpgradeGate: {incident_id!r} — từ chối chạy"
            )
        if incident.cluster_id is None:
            return None
        cluster = session.get(Cluster, incident.cluster_id)
        if cluster is None or not cluster.is_active:
            raise DeployPhaseError(
                f"Cụm của NodeUpgradeGate không còn tồn tại hoặc đã bị vô hiệu hoá: {incident.cluster_id}"
            )
        session.expunge(cluster)
        return cluster


def _any_configured_mon_host(exclude: str | None = None, cluster: Cluster | None = None) -> str:
    """Any currently-configured MON node's host, optionally excluding one
    (FR-5's "chạy từ MỘT MON KHÁC, không phải chính node đang xử lý") —
    raises DeployPhaseError if none remain, which also naturally covers
    "this node is the only configured mon" (a 1-mon cluster can never
    safely run node_os_gate_prepare's mon-removal step at all)."""
    nodes = configured_nodes() if cluster is None else configured_nodes(cluster)
    candidates = [n["host"] for n in nodes if "MON" in n["roles"] and n["host"] != exclude]
    if not candidates:
        raise DeployPhaseError(
            "Không có node MON nào khác trong cấu hình — không thể gỡ mon an toàn (cụm chỉ có 1 mon)"
            if exclude
            else "Không có node MON nào trong cấu hình"
        )
    return candidates[0]


def _read_osd_flags(mon_host: str, action_params: dict | None = None) -> set[str]:
    """`ceph osd dump`'s `flags` field is a single comma-joined STRING
    (e.g. "sortbitwise,recovery_deletes,noout"), NOT a JSON array."""
    output = _gate_execute(mon_host, "ceph osd dump --format json", action_params or {})
    flags_str = json.loads(output).get("flags") or ""
    return {f for f in flags_str.split(",") if f}


def _parse_osd_backup(output: str) -> list[dict]:
    """Parses `ceph-volume lvm list` output into
    `[{"osd_id": ..., "osd_fsid": ...}, ...]`, one entry per `======
    osd.N =======` block. The SAME output also contains a `cluster fsid`
    field (a DIFFERENT UUID) inside every block — only `osd fsid` is
    captured here (addendum.md's explicit warning: 2 different UUIDs
    appear in the same output). Raises DeployPhaseError if ANY block is
    missing either field (FR-3: a partial backup is worse than none — Story
    11.4's Node Recovery needs an exact OSD count) or if no OSD block is
    found at all (a node labeled with the OSD role that genuinely has none
    is a misconfiguration, not a valid empty backup)."""
    blocks = re.split(r"^====== osd\.\S+ =======\s*$", output, flags=re.MULTILINE)[1:]
    if not blocks:
        raise DeployPhaseError(
            "ceph-volume lvm list không trả về OSD nào trên node có vai OSD — kiểm tra lại cấu hình"
        )
    backups: list[dict] = []
    for block in blocks:
        id_match = re.search(r"^\s*osd id\s+(\S+)\s*$", block, flags=re.MULTILINE)
        fsid_match = re.search(r"^\s*osd fsid\s+(\S+)\s*$", block, flags=re.MULTILINE)
        if not id_match or not fsid_match:
            raise DeployPhaseError(
                "ceph-volume lvm list trả về một entry OSD thiếu osd id/osd fsid — dừng lại, "
                "không backup thiếu"
            )
        backups.append({"osd_id": id_match.group(1), "osd_fsid": fsid_match.group(1)})
    return backups


def _get_node_upgrade_gate_or_raise(
    session, gate_id: str, action_params: dict | None = None
) -> NodeUpgradeGate:
    gate = session.get(NodeUpgradeGate, gate_id)
    if gate is None:
        raise DeployPhaseError(f"Không tìm thấy NodeUpgradeGate id={gate_id!r} — dữ liệu không nhất quán")
    if action_params is not None:
        expected_cluster_id = _gate_cluster_id(action_params)
        if gate.cluster_id != expected_cluster_id:
            raise DeployPhaseError(
                f"NodeUpgradeGate {gate_id!r} không thuộc cluster của Incident "
                f"(gate={gate.cluster_id!r}, incident={expected_cluster_id!r}) — từ chối chạy"
            )
        host = action_params.get("host")
        if not host or host not in {node["host"] for node in _gate_nodes(action_params)}:
            raise DeployPhaseError(
                f"Host {host!r} của NodeUpgradeGate không nằm trong cấu hình cluster — từ chối chạy"
            )
    return gate


def _phase_gate_backup_osd_and_metadata(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-3: backs up every OSD's id+fsid on this host (if it has the OSD
    role), then triggers an on-demand cluster metadata backup
    (worker.backup.metadata, Epic 9) if the last successful one is
    missing/stale. Either half failing stops the WHOLE Prepare (FR-3's own
    consequence text) — no partial state, no continuing to FR-4/FR-5
    without a fresh insurance backup.

    are stubbed here (no-op / never-tripped) — this phase function's own
    signature has no access to cluster_deploy.run()'s real callbacks
    documented as PHASE-level, not finer. Known UX limitation: the
    operator sees this whole phase as one "running" step for as long as
    the nested metadata backup takes, without its own 5-artifact
    sub-progress — acceptable (not a safety gap), out of this story's
    scope to fix.
    """
    host = action_params["host"]
    host_status = [{"host": host, "status": "running"}]
    on_host_update(list(host_status))

    if _osd_role(action_params):
        try:
            output = _gate_execute(host, "ceph-volume lvm list", action_params)
        except ExecutorError as exc:
            host_status[0]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError(f"{host}: ceph-volume lvm list thất bại: {exc}") from exc

        osd_backup = _parse_osd_backup(output)
        with db.SessionLocal() as session:
            gate = _get_node_upgrade_gate_or_raise(
                session, action_params["node_upgrade_gate_id"], action_params
            )
            gate.osd_backup = json.dumps(osd_backup)
            session.commit()

    gate_cluster_id = _gate_cluster_id(action_params)
    if gate_cluster_id is None:
        last_job = backup_metadata.latest_successful_metadata_job()
    else:
        last_job = backup_metadata.latest_successful_metadata_job(gate_cluster_id)
    needs_backup = last_job is None or last_job.created_at < datetime.utcnow() - timedelta(
        hours=_METADATA_BACKUP_FRESHNESS_HOURS
    )
    if needs_backup:
        if gate_cluster_id is None:
            ok = backup_metadata.run(
                action_params["action_pk"],
                {},
                action_params["incident_id"],
                None,
                lambda *_a, **_k: None,
            )
        else:
            ok = backup_metadata.run(
                action_params["action_pk"],
                {},
                action_params["incident_id"],
                gate_cluster_id,
                lambda *_a, **_k: None,
            )
        if not ok:
            host_status[0]["status"] = "failed"
            on_host_update(list(host_status))
            raise DeployPhaseError("Backup metadata cụm on-demand thất bại — dừng Chuẩn bị (FR-3)")

    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_set_maintenance_flags(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-4: only if this node has the OSD role. Idempotency is against
    LIVE CLUSTER FLAG STATE (`ceph osd dump`), not other `NodeUpgradeGate`
    rows — under AD-23/AD-24's strict one-node-at-a-time invariant, two
    `NodeUpgradeGate` rows can never both be non-terminal at once, so a
    check against "another row" would always find nothing (vacuous by
    construction). The PRD's real intent ("đặt cờ 1 lần dùng chung cho cả
    đợt") is about not re-running `ceph osd set` for flags a PREVIOUS node
    in the same multi-node round already set and that haven't been unset
    yet — the cluster's own current flag state is the only information
    that actually answers that question."""
    host = action_params["host"]
    cluster = _gate_cluster(action_params)
    if not _osd_role(action_params):
        on_host_update([{"host": host, "status": "done"}])
        return

    mon_host = _any_configured_mon_host(cluster=cluster)
    host_status = [{"host": mon_host, "status": "running"}]
    on_host_update(list(host_status))
    try:
        current_flags = _read_osd_flags(mon_host, action_params)
        to_set = [f for f in _MAINTENANCE_FLAGS if f not in current_flags]
        if to_set:
            _gate_execute(mon_host, " && ".join(f"ceph osd set {f}" for f in to_set), action_params)
    except (ExecutorError, ValueError) as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{mon_host}: đặt cờ bảo trì thất bại: {exc}") from exc
    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_remove_mon(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-5: only if this node has the MON role. Runs `ceph mon rm <name>`
    (never the deprecated `ceph mon remove` alias) from a DIFFERENT mon,
    then confirms quorum count is exactly previous-count-minus-1 before
    letting Prepare proceed to the next phase — any mismatch stops
    immediately (this IS the "không 2 mon cùng offline" hard invariant's
    enforcement point)."""
    host = action_params["host"]
    cluster = _gate_cluster(action_params)
    if not _mon_role(action_params):
        on_host_update([{"host": host, "status": "done"}])
        return

    other_mon_host = _any_configured_mon_host(exclude=host, cluster=cluster)
    host_status = [{"host": host, "status": "running"}]
    on_host_update(list(host_status))

    try:
        mon_name = _gate_execute(host, "hostname -f 2>/dev/null || hostname", action_params).strip()
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: không lấy được hostname: {exc}") from exc

    try:
        before = json.loads(_gate_execute(other_mon_host, "ceph quorum_status --format json", action_params))
        expected_after = len(before.get("quorum_names") or []) - 1

        _gate_execute(other_mon_host, f"ceph mon rm {shlex.quote(mon_name)}", action_params)

        after = json.loads(_gate_execute(other_mon_host, "ceph quorum_status --format json", action_params))
        actual_after = len(after.get("quorum_names") or [])
    except (ExecutorError, ValueError) as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"Gỡ mon {mon_name} khỏi quorum thất bại: {exc}") from exc

    if actual_after != expected_after:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(
            f"Sau khi gỡ mon {mon_name}, quorum còn {actual_after} mon (kỳ vọng {expected_after}) — "
            "dừng lại ngay, không tiếp tục Chuẩn bị (FR-5)"
        )

    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_mark_prepared(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """DB-only, no SSH — same "final bookkeeping phase" shape as
    _phase_verify_integrity in the restore family above. Does NOT touch
    the CAS lock: PREPARED is non-terminal (AD-18) — the lock stays held
    until Confirm→Recover→DONE (Story 11.4) or Abort→DONE (below)."""
    host = action_params["host"]
    with db.SessionLocal() as session:
        gate = _get_node_upgrade_gate_or_raise(
            session, action_params["node_upgrade_gate_id"], action_params
        )
        gate.state = NodeUpgradeGateState.PREPARED.value
        session.commit()
    on_host_update([{"host": host, "status": "done"}])


_PHASES_BY_ACTION_ID["node_os_gate_prepare"] = [
    ("backup_osd_and_metadata", "Backup OSD id/fsid + metadata cụm", 33, _phase_gate_backup_osd_and_metadata),
    ("set_maintenance_flags", "Đặt cờ bảo trì cấp cụm", 66, _phase_gate_set_maintenance_flags),
    ("remove_mon", "Gỡ mon khỏi quorum", 90, _phase_gate_remove_mon),
    ("mark_prepared", "Đánh dấu node sẵn sàng cài lại OS", 100, _phase_gate_mark_prepared),
]

# --- Epic 11 — shared rejoin-mon helper + node_os_gate_abort (Story 11.3,
# FR-6) -----------------------------------------------------------------


def _rejoin_mon_after_reinstall(
    host: str, mon_name: str, other_mon_host: str, action_params: dict | None = None
) -> None:
    """AD-22's doc-verified sequence (addendum.md, 2 corrections applied):
    fetch the `mon.` keyring + CURRENT monmap (not yet including this mon)
    from a live mon, `mkfs`+start the daemon on `host` with that exact
    monmap/keyring — it self-joins quorum via Paxos. `ceph mon add` does
    NOT appear in Ceph's current official "Adding a Monitor (Manual)"
    procedure at all; an earlier draft fix that added it was itself found
    wrong by a later, doc-verified review pass — do not reintroduce it.

    Reusable by BOTH `node_os_gate_abort` (this story) and
    `node_os_gate_recover` (Story 11.4, FR-14) — keep this function free of
    Abort-specific logic so Story 11.4 can call it verbatim.

    Reuses this file's own `_mkfs_and_start_mon_command`/
    `_read_remote_file_b64`/`_write_remote_file_b64` (same helpers
    `_phase_ceph_deploy_mon_init` already uses for an analogous
    keyring/monmap transfer) rather than inventing new remote-file-transfer
    code — deliberately file-based (`-o <path>` then base64-safe copy), NOT
    `-o -`/stdout capture: `execute_command` UTF-8-decodes stdout, which is
    unsafe for a binary monmap.
    """
    fetch_cmd = (
        f"rm -f {shlex.quote(_REMOTE_MON_KEYRING_PATH)} {shlex.quote(_REMOTE_MONMAP_PATH)} && "
        f"ceph auth get mon. -o {shlex.quote(_REMOTE_MON_KEYRING_PATH)} && "
        f"ceph mon getmap -o {shlex.quote(_REMOTE_MONMAP_PATH)}"
    )
    try:
        _gate_execute(other_mon_host, fetch_cmd, action_params or {})
    except ExecutorError as exc:
        raise DeployPhaseError(f"{other_mon_host}: lấy mon keyring/monmap hiện tại thất bại: {exc}") from exc

    keyring_b64 = _read_remote_file_b64(other_mon_host, _REMOTE_MON_KEYRING_PATH, action_params)
    monmap_b64 = _read_remote_file_b64(other_mon_host, _REMOTE_MONMAP_PATH, action_params)
    _write_remote_file_b64(host, _REMOTE_MON_KEYRING_PATH, keyring_b64, action_params)
    _write_remote_file_b64(host, _REMOTE_MONMAP_PATH, monmap_b64, action_params)

    try:
        _gate_execute(host, _mkfs_and_start_mon_command(mon_name), action_params or {})
    except ExecutorError as exc:
        raise DeployPhaseError(f"{host}: mkfs/start lại ceph-mon@{mon_name} thất bại: {exc}") from exc

    deadline = time.monotonic() + _QUORUM_DEFAULT_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while True:
        try:
            quorum = json.loads(_gate_execute(other_mon_host, "ceph quorum_status --format json", action_params or {}))
            if mon_name in (quorum.get("quorum_names") or []):
                break
        except (ExecutorError, ValueError) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            detail = f" (lỗi gần nhất: {last_error})" if last_error else ""
            raise DeployPhaseError(
                f"mon {mon_name} không rejoin quorum sau {_QUORUM_DEFAULT_TIMEOUT_SECONDS}s{detail}"
            )
        time.sleep(_QUORUM_POLL_INTERVAL_SECONDS)

    _refresh_static_mon_config(host, mon_name, action_params)


def _refresh_static_mon_config(
    rejoined_host: str, rejoined_mon_name: str, action_params: dict | None = None
) -> None:
    """FR-14's own consequence: runtime monmap is correct immediately after
    rejoin, but the STATIC /etc/ceph/ceph.conf is not — a future
    full-cluster restart reads the static file. Refreshes
    `mon_host`/`mon_initial_members` on the rejoined node AND every other
    configured node via a TARGETED `sed` line-replace, NOT a full
    ceph.conf rewrite (`_build_ceph_conf` reconstructs a whole file from
    deploy-time-only action_params this code path doesn't have, and a
    live node's ceph.conf may carry settings that reconstruction doesn't
    know about)."""
    mon_nodes = [n for n in _gate_nodes(action_params or {}) if "MON" in n["roles"]]
    hostnames: dict[str, str] = {}
    for n in mon_nodes:
        ip = n["host"]
        if ip == rejoined_host:
            hostnames[ip] = rejoined_mon_name
            continue
        try:
            hostnames[ip] = _gate_execute(ip, "hostname -f 2>/dev/null || hostname", action_params or {}).strip()
        except ExecutorError as exc:
            raise DeployPhaseError(f"{ip}: không lấy được hostname để cập nhật ceph.conf: {exc}") from exc

    mon_initial_members = ",".join(hostnames[n["host"]] for n in mon_nodes)
    # Dual v1/v2 addressing (addendum.md's own caution against hardcoding
    # :6789) — msgr v2 default port 3300, v1 default port 6789.
    mon_host = ",".join(f"[v2:{n['host']}:3300/0,v1:{n['host']}:6789/0]" for n in mon_nodes)

    sed_cmd = (
        "sed -i "
        f"-e 's/^mon initial members.*/mon initial members = {mon_initial_members}/' "
        f"-e 's/^mon host.*/mon host = {mon_host}/' "
        f"{shlex.quote(_REMOTE_CEPH_CONF_PATH)}"
    )
    for target_host in {n["host"] for n in _gate_nodes(action_params or {})}:
        try:
            _gate_execute(target_host, sed_cmd, action_params or {})
        except ExecutorError as exc:
            raise DeployPhaseError(
                f"{target_host}: cập nhật mon_host/mon_initial_members trong ceph.conf thất bại: {exc}"
            ) from exc


def _phase_gate_abort_rejoin_mon(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-6: reverses node_os_gate_prepare's mon removal — only if
    `NodeUpgradeGate.roles_snapshot` (captured AT PREPARE TIME —
    `action_params["roles"]` here is built from that snapshot, NOT a live
    `configured_nodes()` re-check; see the story's Dev Notes on why
    roles_snapshot is authoritative for Abort/Recover) includes MON. Uses
    the SAME doc-verified full sequence as FR-14 — addendum.md explicitly
    retracts the earlier "just restart the daemon" shortcut assumption,
    since FR-5 already made this node's local monmap stale."""
    host = action_params["host"]
    cluster = _gate_cluster(action_params)
    if "MON" not in (action_params.get("roles") or []):
        on_host_update([{"host": host, "status": "done"}])
        return

    other_mon_host = _any_configured_mon_host(exclude=host, cluster=cluster)
    host_status = [{"host": host, "status": "running"}]
    on_host_update(list(host_status))
    try:
        mon_name = _gate_execute(host, "hostname -f 2>/dev/null || hostname", action_params).strip()
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: không lấy được hostname: {exc}") from exc

    try:
        if _gate_cluster(action_params) is None:
            _rejoin_mon_after_reinstall(host, mon_name, other_mon_host)
        else:
            _rejoin_mon_after_reinstall(host, mon_name, other_mon_host, action_params)
    except DeployPhaseError:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise

    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_abort_maybe_clear_flags(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-6's flag-clear half: only unsets maintenance flags if NO OTHER
    node is mid-flight — checked via `is_node_upgrade_gate_pending`,
    excluding THIS Abort Action's own row (same row-specific exemption
    AD-19 uses in `approve_action_core`; needed here because this gate is
    still non-terminal, `state=ABORTING`, at the moment this phase runs)."""
    host = action_params["host"]
    if "OSD" not in (action_params.get("roles") or []):
        on_host_update([{"host": host, "status": "done"}])
        return

    with db.SessionLocal() as session:
        someone_else_pending = is_node_upgrade_gate_pending(
            session,
            exclude_action_id=action_params["action_pk"],
            cluster_id=_gate_cluster_id(action_params),
        )
    if someone_else_pending:
        on_host_update([{"host": host, "status": "done"}])
        return

    mon_host = _any_configured_mon_host(cluster=_gate_cluster(action_params))
    host_status = [{"host": mon_host, "status": "running"}]
    on_host_update(list(host_status))
    try:
        current_flags = _read_osd_flags(mon_host, action_params)
        to_unset = [f for f in _MAINTENANCE_FLAGS if f in current_flags]
        if to_unset:
            _gate_execute(mon_host, "; ".join(f"ceph osd unset {f}" for f in to_unset), action_params)
    except (ExecutorError, ValueError) as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{mon_host}: gỡ cờ bảo trì thất bại: {exc}") from exc
    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_abort_mark_done(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """DB-only. Terminal state DONE releases the CAS lock (AD-24) —
    ABORTING's own completion counts as DONE, same as a successful
    node_os_gate_recover would."""
    host = action_params["host"]
    with db.SessionLocal() as session:
        gate = _get_node_upgrade_gate_or_raise(
            session, action_params["node_upgrade_gate_id"], action_params
        )
        gate.state = NodeUpgradeGateState.DONE.value
        release_node_upgrade_gate_lock(session, action_params["node_upgrade_gate_id"])
        session.commit()
    on_host_update([{"host": host, "status": "done"}])


_PHASES_BY_ACTION_ID["node_os_gate_abort"] = [
    ("rejoin_mon", "Rejoin mon vào quorum", 50, _phase_gate_abort_rejoin_mon),
    ("maybe_clear_flags", "Gỡ cờ bảo trì (nếu là node cuối cùng)", 90, _phase_gate_abort_maybe_clear_flags),
    ("mark_done", "Đánh dấu Huỷ Chuẩn bị hoàn tất", 100, _phase_gate_abort_mark_done),
]

# --- Epic 11 — node_os_gate_recover (Story 11.4, FR-9..FR-14/FR-16) --------
#
# Confirm & Node Recovery: runs AFTER the operator has manually reinstalled a
# node's OS and clicked "Xác nhận" (dashboard/routes/upgrade.py's re-check,
# AD-20, happens BEFORE this Action is even created — by the time this phase
# list runs, the OS is already known-good). Same action_params contract as
# node_os_gate_prepare/node_os_gate_abort (AD-17): {"host", "target_version",
# "roles" (from NodeUpgradeGate.roles_snapshot, NOT a live configured_nodes()
# re-check — see this story's Dev Notes), "nodes": [host],
# "node_upgrade_gate_id", "action_pk", "incident_id"}.


def _build_base_dependency_install_command(*, install_container_runtime: bool = False) -> str:
    """Shared by `_phase_ceph_deploy_dependencies`/`_phase_ceph_deploy_dependencies_cephadm`
    (fresh deploy, per-node list) and `_phase_gate_configure_base` (Story
    11.4, single host) — same firewalld-stop/SELinux-disable/chrony-install-
    and-start sequence either way, extracted so the call sites can't drift
    apart (same reasoning `_build_ceph_package_repo_command` is already
    shared for two call sites).

    `install_container_runtime` (2026-08-10, default False so the existing
    2 call sites are unchanged) adds podman (RPM/el family — already in the
    base/AppStream repo on CentOS Stream 9, no EPEL needed) or docker.io
    (Debian/Ubuntu family — the in-distro package, no extra Docker repo
    needed) — cephadm's own docs recommend exactly this pairing, one or the
    other, never both. Podman needs no service enable/start (daemonless,
    unlike docker); docker.io does, or `cephadm bootstrap` fails the same
    "no container runtime" preflight check even with the package installed.
    """
    if install_container_runtime:
        container_apt = (
            " && (command -v docker >/dev/null 2>&1 || apt-get install -y docker.io) && "
            "systemctl enable --now docker"
        )
        container_rpm = (
            " && (command -v podman >/dev/null 2>&1 || dnf install -y podman || yum install -y podman)"
        )
    else:
        container_apt = ""
        container_rpm = ""
    apt_snippet = (
        "(command -v python3 >/dev/null 2>&1 || apt-get install -y python3) && "
        "(systemctl stop firewalld 2>/dev/null || true) && "
        "(command -v setenforce >/dev/null 2>&1 && setenforce 0 || true) && "
        "apt-get update -y && apt-get install -y chrony lvm2 && "
        "systemctl enable --now chrony && "
        "(chronyc makestep || true)"
        f"{container_apt}"
    )
    rpm_snippet = (
        "(command -v python3 >/dev/null 2>&1 || (dnf install -y python3 || yum install -y python3)) && "
        "(systemctl stop firewalld 2>/dev/null || true) && "
        "(command -v setenforce >/dev/null 2>&1 && setenforce 0 || true) && "
        "rm -f /etc/yum.repos.d/download.ceph.com_rpm-*.repo && "
        "(dnf install -y chrony epel-release lvm2 || yum install -y chrony epel-release lvm2) && "
        "systemctl enable --now chronyd && "
        "(chronyc makestep || true)"
        f"{container_rpm}"
    )
    return _package_manager_branch({"apt": apt_snippet, "rpm": rpm_snippet})


def _phase_gate_check_disk(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-9: only if this node has the OSD role (else "done" no-op, same
    role-conditional shape every other gate phase uses). READ-ONLY except
    the two explicitly-allowed self-repair commands below — never
    formats/creates anything.

    Known gap this phase exists to close: a fresh OS install's
    `/etc/lvm/devices/system.devices` device-file doesn't know about the
    pre-existing PV signature on the OSD's DATA disk, which physically
    survived the OS reinstall untouched (only the OS disk was wiped) — so
    `pvscan` reports zero PVs even though the data is genuinely still
    there. `lvmdevices --adddev <device>` re-registers it.

    NOT verbatim-sourced from the original runbook (unlike FR-10/11/12) —
    see this story's Dev Notes: verify the exact `pvscan`/`lvmdevices`
    flags against Ceph/LVM docs (or a real reinstalled lab node) before
    trusting this unattended."""
    host = action_params["host"]
    if not _osd_role(action_params):
        on_host_update([{"host": host, "status": "done"}])
        return

    host_status = [{"host": host, "status": "running"}]
    on_host_update(list(host_status))

    check_and_repair_cmd = (
        "lsblk >/tmp/ceph-aiops-lsblk.out 2>&1; "
        "pvscan --cache >/tmp/ceph-aiops-pvscan.out 2>&1; "
        "if ! pvs --reportformat json 2>/dev/null | grep -q '\"pv_name\"'; then "
        "for dev in $(blkid -o device 2>/dev/null); do lvmdevices --adddev \"$dev\" 2>/dev/null || true; done; "
        "pvscan --cache >/tmp/ceph-aiops-pvscan.out 2>&1; "
        "fi; "
        "vgscan >/tmp/ceph-aiops-vgscan.out 2>&1; "
        "lvscan >/tmp/ceph-aiops-lvscan.out 2>&1; "
        "if lvscan 2>/dev/null | grep -q 'inactive'; then vgchange -ay >/dev/null 2>&1; lvscan >/tmp/ceph-aiops-lvscan.out 2>&1; fi; "
        "pvs --reportformat json 2>/dev/null | grep -q '\"pv_name\"' && echo CEPH_AIOPS_PV_OK || echo CEPH_AIOPS_PV_MISSING; "
        # Code-review fix: Task 1 explicitly requires CONFIRMING active state
        # after the vgchange -ay repair attempt, not just running it — this
        # second sentinel is checked below, separately from PV visibility.
        "lvscan 2>/dev/null | grep -q 'inactive' && echo CEPH_AIOPS_LV_INACTIVE || echo CEPH_AIOPS_LV_ALL_ACTIVE"
    )
    try:
        output = _gate_execute(host, check_and_repair_cmd, action_params)
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: kiểm tra đĩa/LVM thất bại: {exc}") from exc

    if "CEPH_AIOPS_PV_OK" not in output:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(
            f"{host}: không thấy Physical Volume nào sau khi đã thử lvmdevices --adddev — "
            f"dừng lại, không chạy lệnh LVM nào tiếp theo (FR-9)"
        )

    if "CEPH_AIOPS_LV_ALL_ACTIVE" not in output:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(
            f"{host}: vẫn còn Logical Volume ở trạng thái inactive sau khi đã thử vgchange -ay — "
            f"dừng lại, không chạy lệnh LVM nào tiếp theo (FR-9)"
        )

    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_configure_base(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-10: ALWAYS runs (no role-conditional skip — hosts file/SELinux/
    firewalld/chrony apply to every node regardless of role, unlike every
    other phase in this Epic 11 section)."""
    host = action_params["host"]
    gate_nodes = _gate_nodes(action_params)
    host_status = [{"host": host, "status": "running"}]
    on_host_update(list(host_status))

    try:
        hosts_lines: list[str] = []
        for n in gate_nodes:
            other_ip = n["host"]
            if other_ip == host:
                other_hostname = _gate_execute(host, "hostname -f 2>/dev/null || hostname", action_params).strip()
            else:
                other_hostname = _gate_execute(other_ip, "hostname -f 2>/dev/null || hostname", action_params).strip()
            hosts_lines.append((other_ip, other_hostname))
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: không lấy được hostname để cập nhật /etc/hosts: {exc}") from exc

    append_hosts_cmd = " && ".join(
        f"(grep -qF {shlex.quote(f'{ip} {hostname}')} /etc/hosts || "
        f"echo {shlex.quote(f'{ip} {hostname}')} >> /etc/hosts)"
        for ip, hostname in hosts_lines
    )
    dependency_cmd = _build_base_dependency_install_command()
    # Code-review fix: each check echoes its OWN failure marker instead of
    # being chained with `&&` — a chained command's failure gives no way to
    # tell SELinux/firewalld/chronyd apart (Task 2's own text: "naming
    # exactly which check failed"). Always exits 0 so ExecutorError itself
    # never fires here; the markers below are parsed in Python instead.
    verify_cmd = (
        "test \"$(getenforce)\" = Disabled || echo CEPH_AIOPS_SELINUX_STILL_ENFORCING; "
        "systemctl is-active --quiet firewalld && echo CEPH_AIOPS_FIREWALLD_STILL_ACTIVE; "
        "systemctl is-active --quiet chronyd || echo CEPH_AIOPS_CHRONYD_NOT_ACTIVE; "
        "true"
    )
    try:
        if append_hosts_cmd:
            _gate_execute(host, append_hosts_cmd, action_params)
        _gate_execute(host, dependency_cmd, action_params)
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: cấu hình node cơ bản thất bại: {exc}") from exc

    try:
        verify_output = _gate_execute(host, verify_cmd, action_params)
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: không chạy được bước xác nhận cấu hình cơ bản: {exc}") from exc

    failed_checks = []
    if "CEPH_AIOPS_SELINUX_STILL_ENFORCING" in verify_output:
        failed_checks.append("SELinux chưa Disabled")
    if "CEPH_AIOPS_FIREWALLD_STILL_ACTIVE" in verify_output:
        failed_checks.append("firewalld vẫn active")
    if "CEPH_AIOPS_CHRONYD_NOT_ACTIVE" in verify_output:
        failed_checks.append("chronyd chưa active")
    if failed_checks:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: xác nhận cấu hình cơ bản thất bại — {', '.join(failed_checks)}")

    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_install_packages(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-11: always runs (Ceph must be installed regardless of role before
    FR-12/13/14 can do anything).

    Code-review fix: `target_version` ultimately originates from
    `POST /upgrade/gate/prepare`'s `Form(...)` field (Story 11.3), which
    does not itself validate its format — and this phase interpolates it
    directly into RPM package names (`f"{pkg}-{version}"`, no
    `shlex.quote`) whenever `pin_exact_version` is true (Nautilus-style
    versions). A strict `x.y.z`-only format check here, before ANY shell
    command is built, closes that regardless of what the Dashboard route
    does or doesn't validate — same `_TARGET_VERSION_RE` shape
    `dashboard/routes/upgrade.py` already uses for this exact string."""
    host = action_params["host"]
    version = action_params["target_version"]
    host_status = [{"host": host, "status": "running"}]
    on_host_update(list(host_status))

    if not _TARGET_VERSION_RE.match(version):
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"target_version {version!r} không đúng định dạng x.y.z")
    if codename_for_version(version) is None:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"Không tìm thấy mã tên release Ceph cho phiên bản {version!r}")

    repo_cmd = _build_ceph_package_repo_command(version)
    epel_and_devel_repo_cmd = (
        "(dnf install -y epel-release || yum install -y epel-release || true); "
        "rhel_ver=$(rpm -E %rhel); "
        "if [ \"$rhel_ver\" = 8 ]; then "
        "(dnf config-manager --set-enabled powertools 2>/dev/null "
        "|| dnf config-manager --set-enabled PowerTools 2>/dev/null || true); "
        "else "
        "(dnf config-manager --set-enabled crb 2>/dev/null || true); "
        "fi"
    )

    roles = [r.lower() for r in (action_params.get("roles") or [])]
    apt_packages = sorted({pkg for role in roles for pkg in _ROLE_TO_PACKAGES_APT.get(role, ())})
    rpm_packages = sorted({pkg for role in roles for pkg in _ROLE_TO_PACKAGES_RPM.get(role, ())})
    pin_exact_version = repo_path_version(version) != version
    mandatory_rpm = ["fmt", "python3", "python3-libs"] + (
        [f"{pkg}-{version}" for pkg in rpm_packages] if pin_exact_version else list(rpm_packages)
    )
    fallback_rpm = [
        "boost-random",
        "boost-thread",
        "boost-iostreams",
        "boost-python3",
        "snappy",
        "leveldb",
        "libbabeltrace",
        "lttng-ust",
        "userspace-rcu",
        "gperftools-libs",
    ]
    apt_package_list = " ".join(apt_packages)
    mandatory_rpm_list = " ".join(mandatory_rpm)
    fallback_rpm_list = " ".join(mandatory_rpm + fallback_rpm)
    apt_install_snippet = f"apt-get install -y {apt_package_list}"
    rpm_install_snippet = (
        f"(dnf install -y {mandatory_rpm_list} || yum install -y {mandatory_rpm_list} "
        f"|| dnf install -y {fallback_rpm_list} || yum install -y {fallback_rpm_list})"
    )
    install_cmd = _package_manager_branch({"apt": apt_install_snippet, "rpm": rpm_install_snippet})

    try:
        _gate_execute(host, repo_cmd, action_params)
        _gate_execute(host, epel_and_devel_repo_cmd, action_params)
        _gate_execute(host, install_cmd, action_params)
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: cài lại gói Ceph {version} thất bại: {exc}") from exc

    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_restore_config_and_keyring(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-12: always runs. `ceph -s` must succeed from `host` itself
    IMMEDIATELY after the admin keyring is copied, before the bootstrap-osd
    step — FR-12's own testable consequence, no inferring success via a
    later phase."""
    host = action_params["host"]
    other_mon_host = _any_configured_mon_host(exclude=host, cluster=_gate_cluster(action_params))
    host_status = [{"host": other_mon_host, "status": "running"}]
    on_host_update(list(host_status))

    try:
        conf_b64 = _read_remote_file_b64(other_mon_host, _REMOTE_CEPH_CONF_PATH, action_params)
        admin_keyring_b64 = _read_remote_file_b64(other_mon_host, _REMOTE_ADMIN_KEYRING_PATH, action_params)
        _write_remote_file_b64(host, _REMOTE_CEPH_CONF_PATH, conf_b64, action_params)
        _write_remote_file_b64(host, _REMOTE_ADMIN_KEYRING_PATH, admin_keyring_b64, action_params)
    except DeployPhaseError:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise

    host_status = [{"host": host, "status": "running"}]
    on_host_update(list(host_status))
    try:
        _gate_execute(host, "ceph -s", action_params)
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(
            f"{host}: 'ceph -s' thất bại ngay sau khi copy ceph.conf/admin keyring — dừng lại, "
            f"không tiếp tục phục hồi bootstrap-osd keyring: {exc}"
        ) from exc

    try:
        _gate_execute(
            host,
            f"mkdir -p {shlex.quote(os.path.dirname(_REMOTE_BOOTSTRAP_OSD_KEYRING_PATH))} && "
            f"ceph auth get client.bootstrap-osd -o {shlex.quote(_REMOTE_BOOTSTRAP_OSD_KEYRING_PATH)}",
            action_params,
        )
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: phục hồi bootstrap-osd keyring thất bại: {exc}") from exc

    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_activate_osd(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-13: only if this node has the OSD role."""
    host = action_params["host"]
    if not _osd_role(action_params):
        on_host_update([{"host": host, "status": "done"}])
        return

    host_status = [{"host": host, "status": "running"}]
    on_host_update(list(host_status))

    with db.SessionLocal() as session:
        gate = _get_node_upgrade_gate_or_raise(
            session, action_params["node_upgrade_gate_id"], action_params
        )
        backed_up = json.loads(gate.osd_backup or "[]")
    expected_ids = sorted({str(entry["osd_id"]) for entry in backed_up})
    if not expected_ids:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(
            f"{host}: không có dữ liệu osd_backup từ Chuẩn bị (Story 11.3) — không biết cần "
            f"kích hoạt lại OSD id nào"
        )

    start_cmds = " && ".join(f"systemctl enable --now ceph-osd@{shlex.quote(i)}" for i in expected_ids)
    try:
        _gate_execute(host, "ceph-volume lvm activate --all", action_params)
        _gate_execute(host, start_cmds, action_params)
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: kích hoạt lại OSD thất bại: {exc}") from exc

    try:
        tree = json.loads(_gate_execute(host, "ceph osd tree --format json", action_params))
    except (ExecutorError, ValueError) as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: không đọc được ceph osd tree để xác nhận: {exc}") from exc

    up_ids = sorted(
        {
            str(n["id"])
            for n in (tree.get("nodes") or [])
            if n.get("type") == "osd" and str(n.get("id")) in expected_ids and n.get("status") == "up"
        }
    )
    if up_ids != expected_ids:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(
            f"{host}: sau khi kích hoạt, OSD 'up' là {up_ids}, kỳ vọng đúng {expected_ids} "
            f"(đã backup ở Story 11.3) — dừng lại"
        )

    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_rejoin_mon(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-14: only if this node has the MON role. Reuses
    `_rejoin_mon_after_reinstall` (Story 11.3) UNCHANGED — its docstring
    already names this phase as its second intended caller; do not write a
    second copy of the mkfs/start/poll/`_refresh_static_mon_config`
    sequence (AD-22)."""
    host = action_params["host"]
    if not _mon_role(action_params):
        on_host_update([{"host": host, "status": "done"}])
        return

    other_mon_host = _any_configured_mon_host(exclude=host, cluster=_gate_cluster(action_params))
    host_status = [{"host": host, "status": "running"}]
    on_host_update(list(host_status))
    try:
        mon_name = _gate_execute(host, "hostname -f 2>/dev/null || hostname", action_params).strip()
    except ExecutorError as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{host}: không lấy được hostname: {exc}") from exc

    try:
        if _gate_cluster(action_params) is None:
            _rejoin_mon_after_reinstall(host, mon_name, other_mon_host)
        else:
            _rejoin_mon_after_reinstall(host, mon_name, other_mon_host, action_params)
    except DeployPhaseError:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise

    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_maybe_clear_flags(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """FR-16: only if this node has the OSD role. Identical shape to Story
    11.3's `_phase_gate_abort_maybe_clear_flags` — only unsets maintenance
    flags if NO OTHER node is mid-flight."""
    host = action_params["host"]
    if not _osd_role(action_params):
        on_host_update([{"host": host, "status": "done"}])
        return

    with db.SessionLocal() as session:
        someone_else_pending = is_node_upgrade_gate_pending(
            session,
            exclude_action_id=action_params["action_pk"],
            cluster_id=_gate_cluster_id(action_params),
        )
    if someone_else_pending:
        on_host_update([{"host": host, "status": "done"}])
        return

    mon_host = _any_configured_mon_host(cluster=_gate_cluster(action_params))
    host_status = [{"host": mon_host, "status": "running"}]
    on_host_update(list(host_status))
    try:
        current_flags = _read_osd_flags(mon_host, action_params)
        to_unset = [f for f in _MAINTENANCE_FLAGS if f in current_flags]
        if to_unset:
            _gate_execute(mon_host, "; ".join(f"ceph osd unset {f}" for f in to_unset), action_params)
    except (ExecutorError, ValueError) as exc:
        host_status[0]["status"] = "failed"
        on_host_update(list(host_status))
        raise DeployPhaseError(f"{mon_host}: gỡ cờ bảo trì thất bại: {exc}") from exc
    host_status[0]["status"] = "done"
    on_host_update(list(host_status))


def _phase_gate_mark_recovered(nodes: list[dict], action_params: dict, on_host_update) -> None:
    """DB-only. DONE is DONE regardless of which arc (Recover or Abort)
    reached it — identical shape to `_phase_gate_abort_mark_done`."""
    host = action_params["host"]
    with db.SessionLocal() as session:
        gate = _get_node_upgrade_gate_or_raise(
            session, action_params["node_upgrade_gate_id"], action_params
        )
        gate.state = NodeUpgradeGateState.DONE.value
        release_node_upgrade_gate_lock(session, action_params["node_upgrade_gate_id"])
        session.commit()
    on_host_update([{"host": host, "status": "done"}])


_PHASES_BY_ACTION_ID["node_os_gate_recover"] = [
    ("check_disk", "Kiểm tra đĩa/LVM", 14, _phase_gate_check_disk),
    ("configure_base", "Cấu hình node cơ bản", 28, _phase_gate_configure_base),
    ("install_packages", "Cài lại gói Ceph", 42, _phase_gate_install_packages),
    ("restore_config_and_keyring", "Phục hồi config + keyring", 56, _phase_gate_restore_config_and_keyring),
    ("activate_osd", "Kích hoạt lại OSD", 70, _phase_gate_activate_osd),
    ("rejoin_mon", "Rejoin mon vào quorum", 84, _phase_gate_rejoin_mon),
    ("maybe_clear_flags", "Gỡ cờ bảo trì (nếu là node OSD cuối cùng)", 92, _phase_gate_maybe_clear_flags),
    ("mark_recovered", "Đánh dấu node đã phục hồi", 100, _phase_gate_mark_recovered),
]

# Deploy vs delete post-phase env-config writes go opposite directions
# (populate vs clear) — this set is how run() tells them apart without a
# separate parameter threaded through every call site.
_DELETE_CLUSTER_ACTION_IDS = frozenset({"delete_cluster_cephadm", "delete_cluster_manual"})

# AD-17: gate actions never touch .env — they never change cluster
# topology (action_params["nodes"] is a single bare host string, not the
# dict-list _write_cluster_config's _node_ips_with_role expects, so
# reaching that call for these action_ids would crash, not just be
# pointless). node_os_gate_recover is included now even though Story 11.4
# hasn't added its phase list yet — harmless, and matches AD-17's own
# frozenset literal verbatim so that story doesn't need to touch this set.
_SKIP_CONFIG_EPILOGUE_ACTION_IDS = frozenset(
    {"node_os_gate_prepare", "node_os_gate_recover", "node_os_gate_abort"}
)


def _fail_node_upgrade_gate(action_params: dict) -> None:
    """Best-effort cleanup for a node_os_gate_* action that failed (kill-
    switch blocked before a phase, or a phase raised) — marks the
    NodeUpgradeGate FAILED and releases the CAS lock (AD-24) so a LATER
    Prepare attempt (this node retried, or a different node) is not
    permanently blocked. Deliberately swallows its own exceptions: a DB
    hiccup while recording the ORIGINAL failure must never mask that
    failure or raise a second, more confusing one out of run(). If this
    cleanup itself fails, the lock stays held until an operator manually
    intervenes (AD-18: no automated path out of FAILED) — logged at
    .exception() level specifically so that is never silent."""
    gate_id = action_params.get("node_upgrade_gate_id")
    if not gate_id:
        return
    try:
        with db.SessionLocal() as session:
            gate = session.get(NodeUpgradeGate, gate_id)
            if gate is not None and gate.state not in (
                NodeUpgradeGateState.DONE.value,
                NodeUpgradeGateState.FAILED.value,
            ):
                gate.state = NodeUpgradeGateState.FAILED.value
                release_node_upgrade_gate_lock(session, gate_id)
                session.commit()
    except Exception:
        logger.exception(
            "cluster_deploy._fail_node_upgrade_gate: could not mark gate %s FAILED / release lock — "
            "manual DB intervention may be needed",
            gate_id,
        )


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
        # RGW is opt-in (see _phase_ceph_deploy_rgw_create's own docstring)
        # — an empty string here is correct and expected for a cluster with
        # no RGW nodes, same "unconditionally overwrite" posture as mon/mgr/
        # osd above (this .env shortcut always reflects the CURRENT node
        # list, never a stale one from an earlier deploy/convert).
        env_config.CLUSTER_ENV_NAMES["ceph_rgw_nodes"]: ",".join(_node_ips_with_role(nodes, "rgw")),
        env_config.CLUSTER_ENV_NAMES["ceph_exec_mode"]: exec_mode,
        # Every deploy method writes the admin keyring at this canonical
        # location; prefill Settings so the freshly deployed cluster can be
        # connected without making the operator retype it.
        env_config.CLUSTER_ENV_NAMES["ceph_keyring_path"]: _REMOTE_ADMIN_KEYRING_PATH,
    }
    env_config.update_env_file_batch(fields)


def run(
    action_pk: str,
    action_id: str,
    action_params: dict,
    incident_id: str,
    write_progress,
    *_unused,
) -> bool:
    """Executes the ordered phase sequence for `action_id`, checking the
    progress via `write_progress(action_pk, progress)` after every status
    change — same callback `worker/llm/router_client.py::_write_action_progress`
    already provides, reused unchanged.

    Returns True only if every phase completed successfully (the deploy is
    healthy) — the caller (`_execute_approved_action`) turns this into
    Action.status EXECUTED/FAILED the same way it already does for the
    generic per-host loop's own True/False result.
    """
    if action_id in _SKIP_CONFIG_EPILOGUE_ACTION_IDS:
        # The persisted action_params intentionally contains only JSON data;
        # resolve the Incident's cluster here in the Worker and keep the ORM
        # object in this in-memory copy. This prevents a gate created for a
        # non-default cluster from falling back to default SSH/settings.
        action_params = dict(action_params)
        try:
            action_params["_gate_cluster"] = _cluster_for_gate_action(incident_id)
        except DeployPhaseError as exc:
            logger.error("cluster_deploy.run: refusing unscoped OS gate action %s: %s", action_pk, exc)
            _fail_node_upgrade_gate(action_params)
            return False

    nodes = action_params.get("nodes") or []
    phases = _PHASES_BY_ACTION_ID.get(action_id)
    if not phases:
        logger.error("cluster_deploy.run: no phase sequence registered for action_id=%s", action_id)
        return False

    progress = [_make_step(key, label, pct) for key, label, pct, _fn in phases]
    write_progress(action_pk, progress)

    expected_fingerprint = action_params.get("_cluster_config_fingerprint")
    if expected_fingerprint and expected_fingerprint != env_config.current_cluster_config_fingerprint():
        progress[0]["status"] = "failed"
        progress[0]["message"] = "Cấu hình cụm đã thay đổi sau khi đề xuất; tạo proposal mới trước khi chạy."
        progress[0]["finished_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)
        logger.warning("cluster_deploy.run: stale lifecycle proposal rejected: action=%s", action_pk)
        return False

    for index, (step_key, _label, _pct, fn) in enumerate(phases):
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
            if action_id in _SKIP_CONFIG_EPILOGUE_ACTION_IDS:
                _fail_node_upgrade_gate(action_params)
            return False
        except Exception as exc:
            progress[index]["status"] = "failed"
            progress[index]["message"] = f"Lỗi không mong đợi: {exc}"
            progress[index]["finished_at"] = datetime.utcnow().isoformat()
            write_progress(action_pk, progress)
            logger.exception(
                "cluster_deploy.run: unexpected error in phase %s for action %s", step_key, action_pk
            )
            if action_id in _SKIP_CONFIG_EPILOGUE_ACTION_IDS:
                _fail_node_upgrade_gate(action_params)
            return False

        progress[index]["status"] = "done"
        progress[index]["finished_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)

    try:
        if action_id in _DELETE_CLUSTER_ACTION_IDS:
            _clear_cluster_config()
        elif action_id in _SKIP_CONFIG_EPILOGUE_ACTION_IDS:
            pass
        else:
            _write_cluster_config(action_params, action_id)
        with db.SessionLocal() as session:
            sync_default_cluster_from_env(session)
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
