import json
import logging
import os
import re
import shlex

import paramiko

from config.settings import settings
from shared import ceph_releases

logger = logging.getLogger(__name__)

CEPH_HEALTH_INNER_COMMAND = "ceph health detail --format json"
CONNECT_TIMEOUT_SECONDS = 2
COMMAND_TIMEOUT_SECONDS = 5
# cephadm shell spins up a fresh container per invocation (infers fsid/config/
# keyring itself) rather than exec-ing into an already-running one — measured
# ~2.5s against a real cephadm/reef cluster, comfortably under this but with
# headroom for a cold image pull or a slower link.
CEPHADM_COMMAND_TIMEOUT_SECONDS = 15
KNOWN_HOSTS_PATH = os.path.expanduser("~/.ssh/ceph_lab_known_hosts")
VALID_STATUSES = {"HEALTH_OK", "HEALTH_WARN", "HEALTH_ERR"}
VALID_EXEC_MODES = {"docker", "podman", "cephadm", "none"}

# Dashboard Nodes-page CLI (read-only diagnostics): command_id -> the exact
# `ceph`-family command run. This is the ONLY set of commands that feature
# can ever execute — command_id is a dict key looked up server-side, never
# a client-supplied command string, so there is no path from that UI to
# arbitrary shell (see run_diagnostic_command below).
DIAGNOSTIC_COMMANDS: dict[str, str] = {
    "ceph_status": "ceph -s",
    "ceph_health_detail": "ceph health detail",
    "ceph_osd_tree": "ceph osd tree",
    "ceph_osd_df": "ceph osd df",
    "ceph_df": "ceph df",
    "ceph_versions": "ceph versions",
}
# An operator-facing audit record, not a log store — long output (e.g. a
# large `ceph osd tree`) is truncated rather than growing the DB unbounded.
DIAGNOSTIC_OUTPUT_MAX_CHARS = 8000

# mcp_ceph_server.py's read-only cluster-query tools (Chat-with-AI, Part 2) —
# same non-cephadm/cephadm split as COMMAND_TIMEOUT_SECONDS/
# CEPHADM_COMMAND_TIMEOUT_SECONDS above, just a slightly longer default
# (these are chat-triggered, on-demand queries, not the Watcher's tight
# poll loop, so a couple more seconds of headroom costs nothing real).
MCP_COMMAND_TIMEOUT_SECONDS = 10


def build_exec_command(exec_mode: str, container: str, inner_command: str) -> str:
    """Wraps `inner_command` for how this cluster is actually deployed —
    not every Ceph cluster is a plain `docker run` the way this lab's is:

    - "docker"  — `docker exec {container} <cmd>` (a plain `docker run` cluster)
    - "podman"  — `podman exec {container} <cmd>` (a plain `podman run` cluster
      with one fixed, known container name — NOT cephadm, see below)
    - "cephadm" — `cephadm shell -- <cmd>` — cephadm infers the right fsid/
      config/keyring itself; `container` is ignored, no name needed at all.
      This is NOT the same as "podman": a real cephadm mon container has no
      admin keyring mounted (`ceph` run directly inside it fails with
      "unable to find a keyring") and its name is auto-generated/per-host
      (e.g. `ceph-<fsid>-mon-<hostname>`) rather than one fixed name — both
      verified against a real cephadm/reef cluster.
    - "none"    — `<cmd>` run directly, no container at all (ceph-deploy or a
      package install with `ceph` native on the host) — `container` is
      ignored in this mode.

    Raises ValueError on an unrecognized mode rather than silently falling
    back to one wrapping style — a typo'd mode must fail loudly, not quietly
    run the wrong command shape against a real cluster.
    """
    if exec_mode not in VALID_EXEC_MODES:
        raise ValueError(f"unknown ceph_exec_mode: {exec_mode!r} (expected one of {sorted(VALID_EXEC_MODES)})")
    if exec_mode == "none":
        return inner_command
    if exec_mode == "cephadm":
        return f"cephadm shell -- {inner_command}"
    return f"{exec_mode} exec {shlex.quote(container)} {inner_command}"

# The MON node that last answered query_cluster_health() successfully — used
# by watcher/collector.py as a better fallback than "always the first
# configured node" when a check's detail text names no specific mon.
last_successful_mon_node: str | None = None


class CephQueryError(Exception):
    """Raised when no MON node could be reached and queried successfully."""


def get_mon_nodes() -> list[str]:
    return [h.strip() for h in settings.ceph_mon_nodes.split(",") if h.strip()]


def ssh_key_path_error(ssh_key_path: str) -> str | None:
    """Story 5.1: checked by the Dashboard's cluster-connection form BEFORE
    attempting an SSH connection, so a bad path fails with a clear message
    instead of a confusing paramiko error. Returns None if the path is
    usable, else a human-readable reason."""
    if not os.path.exists(ssh_key_path):
        return f"SSH key path không tồn tại trên server: {ssh_key_path}"
    if not os.path.isfile(ssh_key_path):
        return f"SSH key path không phải là file hợp lệ (có thể là thư mục): {ssh_key_path}"
    if not os.access(ssh_key_path, os.R_OK):
        return f"SSH key tồn tại nhưng không đọc được (kiểm tra quyền file): {ssh_key_path}"
    return None


def read_public_key(ssh_key_path: str) -> str | None:
    """Reads the paired `<ssh_key_path>.pub` file (the standard ssh-keygen
    convention) so the Dashboard can show the operator exactly what to add
    to `authorized_keys` on a NEW cluster's nodes, instead of requiring them
    to go find/cat the file themselves over a separate shell session.
    Returns None if `ssh_key_path` itself is invalid or no `.pub` file
    exists/is readable alongside it — a display nicety, not a hard error
    (the private key path's own validity is `ssh_key_path_error()`'s job)."""
    if ssh_key_path_error(ssh_key_path) is not None:
        return None
    pub_path = ssh_key_path + ".pub"
    if not os.path.exists(pub_path) or not os.access(pub_path, os.R_OK):
        return None
    try:
        return open(pub_path).read().strip()
    except OSError:
        return None


def _run_remote_command(host: str, command: str, command_timeout: int = COMMAND_TIMEOUT_SECONDS) -> str:
    return _run_remote_command_with(
        host, command, settings.ssh_user, settings.ssh_key_path, command_timeout
    )


def _run_remote_command_with(
    host: str,
    command: str,
    ssh_user: str,
    ssh_key_path: str,
    command_timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> str:
    """Story 5.1: parameterized core so a Dashboard form's not-yet-saved
    values can be tested for real before being written to `.env` —
    `_run_remote_command` above is a thin wrapper over this using current
    `settings`, preserving its existing signature/behavior for
    `run_command_on_node` (watcher/collector.py) unchanged.

    `command_timeout` defaults to the module constant (used by every
    existing caller) but is overridable — watcher/node_metrics.py's
    /proc sampling script sleeps ~1s on the remote end, which the fixed
    5s default leaves too little headroom for over a slow link."""
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    # Trust-on-first-use: an unseen host's key is accepted and persisted below;
    # a *changed* key for an already-known host is rejected by paramiko's
    # Transport itself (BadHostKeyException), regardless of this policy —
    # that mismatch check is what actually guards against a swapped/MITM'd
    # node, not this policy object.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            username=ssh_user,
            key_filename=ssh_key_path,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        client.save_host_keys(KNOWN_HOSTS_PATH)
        _stdin, stdout, stderr = client.exec_command(command, timeout=command_timeout)
        # Read output fully BEFORE checking exit status: if the remote command
        # writes more than the channel buffer holds, it blocks on write until
        # someone drains stdout — calling recv_exit_status() first would wait
        # for a process exit that can't happen until stdout is read. Classic
        # SSH deadlock.
        output = stdout.read().decode()
        error_output = stderr.read().decode()
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            raise CephQueryError(f"{host}: command exited {exit_status}: {error_output}")
        return output
    finally:
        try:
            client.close()
        except Exception:
            logger.warning("_run_remote_command: error closing SSH connection to %s", host)


def run_command_on_node(host: str, command: str, timeout: int = COMMAND_TIMEOUT_SECONDS) -> str:
    """Public wrapper around `_run_remote_command` for other watcher modules
    (e.g. `collector.py`, `node_metrics.py`) that need to run an arbitrary
    read-only command on a specific node — reuses the same SSH connection +
    host-key-pinning logic, no duplicated paramiko setup."""
    return _run_remote_command(host, command, timeout)


def run_diagnostic_command(host: str, command_id: str) -> str:
    """Runs one WHITELISTED read-only command on `host`, wrapped for however
    this cluster is deployed (settings.ceph_exec_mode). `command_id` must be
    a key of DIAGNOSTIC_COMMANDS — raises ValueError otherwise, same
    fail-loud posture as build_exec_command's exec_mode check.

    This is the only place the Dashboard's Nodes-page CLI ever calls to run
    something — there is no path from that feature to an arbitrary shell
    string, by construction.
    """
    if command_id not in DIAGNOSTIC_COMMANDS:
        raise ValueError(f"unknown diagnostic command_id: {command_id!r}")
    inner_command = DIAGNOSTIC_COMMANDS[command_id]
    command = build_exec_command(settings.ceph_exec_mode, settings.ceph_container_name, inner_command)
    command_timeout = (
        CEPHADM_COMMAND_TIMEOUT_SECONDS if settings.ceph_exec_mode == "cephadm" else COMMAND_TIMEOUT_SECONDS
    )
    output = _run_remote_command(host, command, command_timeout)
    return output[:DIAGNOSTIC_OUTPUT_MAX_CHARS]


def run_ceph_json_command(inner_command: str) -> tuple[str, dict | list]:
    """Runs `inner_command` (a `ceph ...` subcommand WITHOUT `--format json`
    — this appends it) against each configured MON node in turn until one
    succeeds, parsing the output as JSON. Same multi-MON fallback posture as
    `query_cluster_health_with` (FR1: keep answering even if one MON node is
    temporarily unreachable).

    Powers dashboard/ceph_tools.py's Chat-with-AI query tools — for
    FIXED_TOOL_COMMANDS, `inner_command` is a fixed literal string, same
    AD-5 closed-enum posture as DIAGNOSTIC_COMMANDS/action_id elsewhere in
    this codebase; run_ceph_command_tool is the one deliberate exception
    (an explicitly requested, denylist-gated escape hatch for an arbitrary
    read-only `ceph ...` command — see that function's docstring for why
    this is a real, accepted gap rather than the closed-enum guarantee the
    fixed tools get).

    Returns `(host, parsed)` — `parsed` is the decoded JSON value (a dict for
    most subcommands; `ceph osd pool ls detail --format json` returns a bare
    list when there's more than one pool, verified against a real
    cephadm/reef cluster), or `{"raw_output": <text>}` if some future ceph
    version's `--format json` support for a subcommand stops round-tripping
    as valid JSON. Raises `CephQueryError` if every configured MON node
    failed.
    """
    nodes = get_mon_nodes()
    if not nodes:
        raise CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
    command = build_exec_command(
        settings.ceph_exec_mode, settings.ceph_container_name, f"{inner_command} --format json"
    )
    command_timeout = (
        CEPHADM_COMMAND_TIMEOUT_SECONDS
        if settings.ceph_exec_mode == "cephadm"
        else MCP_COMMAND_TIMEOUT_SECONDS
    )
    errors = []
    for host in nodes:
        try:
            output = _run_remote_command(host, command, command_timeout)
        except Exception as exc:
            logger.warning("run_ceph_json_command: %s failed: %s", host, exc)
            errors.append(f"{host}: {exc}")
            continue
        try:
            parsed = json.loads(output)
        except (TypeError, ValueError):
            parsed = {"raw_output": output}
        return host, parsed
    raise CephQueryError(f"All MON nodes failed: {'; '.join(errors)}")


def _parse_health_payload(raw_output: str) -> dict:
    payload = json.loads(raw_output)
    if not isinstance(payload, dict) or payload.get("status") not in VALID_STATUSES:
        raise CephQueryError(f"unexpected health payload shape: {raw_output[:200]!r}")
    return payload


def query_cluster_health() -> dict:
    """Query `ceph health detail --format json` via SSH, wrapped for however
    this cluster is deployed (settings.ceph_exec_mode — see
    `build_exec_command`), using the currently configured `settings`.

    Tries each configured MON node in turn until one succeeds — Watcher must
    keep monitoring even if one MON node is temporarily unreachable (FR1).
    """
    nodes = get_mon_nodes()
    if not nodes:
        raise CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
    return query_cluster_health_with(
        nodes,
        settings.ceph_container_name,
        settings.ssh_user,
        settings.ssh_key_path,
        settings.ceph_exec_mode,
    )


def query_cluster_health_with(
    mon_nodes: list[str],
    container_name: str,
    ssh_user: str,
    ssh_key_path: str,
    exec_mode: str = "docker",
) -> dict:
    """Story 5.1: same fallback-across-MON-nodes logic as `query_cluster_health()`,
    but takes every connection parameter explicitly instead of reading
    `settings` — lets the Dashboard test a cluster-connection form's
    not-yet-saved values for real before writing them to `.env`.

    `exec_mode` defaults to "docker" so every pre-existing caller (before
    multi-deploy-mode support existed) keeps its exact original behavior."""
    if not mon_nodes:
        raise CephQueryError("no MON nodes configured")

    command = build_exec_command(exec_mode, container_name, CEPH_HEALTH_INNER_COMMAND)
    command_timeout = (
        CEPHADM_COMMAND_TIMEOUT_SECONDS if exec_mode == "cephadm" else COMMAND_TIMEOUT_SECONDS
    )
    global last_successful_mon_node
    errors = []
    for host in mon_nodes:
        try:
            output = _run_remote_command_with(host, command, ssh_user, ssh_key_path, command_timeout)
            payload = _parse_health_payload(output)
            last_successful_mon_node = host
            return payload
        except Exception as exc:
            logger.warning("query_cluster_health_with: %s failed: %s", host, exc)
            errors.append(f"{host}: {exc}")
    raise CephQueryError(f"All MON nodes failed: {'; '.join(errors)}")


# --- Cluster Upgrade feature (2026-07-23) ----------------------------------
#
# dashboard/routes/upgrade.py-only — read-only version detection + upgrade
# progress, plus two narrow write commands (pause/resume) an operator uses
# to intervene in an upgrade already in flight. Unlike every remediation
# command (worker/executor/), these two run directly from the Dashboard
# process rather than through the Action/approval pipeline: once
# `ceph orch upgrade start` has been issued (see
# worker/executor/commands.py::_upgrade_ceph_cluster_command), cephadm's own
# mgr module drives the rest of the upgrade in the background — it is NOT
# gated by this codebase's kill-switch or by the Worker process being alive
# at all, so the kill-switch's normal "checked fresh before every
# remediation command" guarantee (AD-4) does not apply to it once started.
# Pause/resume are the operator's actual off-switch for an in-flight
# upgrade; they are read as directly-actionable admin commands (like the
# Nodes page's read-only diagnostics), not as a new Action row.
_VERSION_RE = re.compile(r"ceph version (\d+\.\d+\.\d+)")


def propose_next_version(current_version: str) -> str | None:
    """Best-effort "what's the next release" suggestion shown on the
    Upgrade page — the operator can always type a different target version
    instead. Returns None (no guess) if `current_version`'s major isn't in
    shared/ceph_releases.py's table — already on the newest release it
    knows about, or an unparseable string — rather than fabricating a
    suggestion."""
    return ceph_releases.next_min_version(current_version)


def summarize_cluster_versions() -> dict:
    """Runs `ceph versions` (already whitelisted in DIAGNOSTIC_COMMANDS for
    the Nodes page) and summarizes it for the Upgrade page: per-daemon-type
    version(s), the set of distinct versions cluster-wide, and whether
    they're mixed (e.g. a previous upgrade only partially completed).

    `current_version` is only set when every daemon reports the exact same
    single version — a mixed cluster has no one "current version" to
    propose an upgrade FROM, so the Upgrade page must show the raw
    breakdown instead of a single-version summary in that case.

    Raises CephQueryError if no MON node could be reached (same as
    query_cluster_health/run_ceph_json_command).
    """
    _, payload = run_ceph_json_command("ceph versions")
    per_type: dict[str, list[str]] = {}
    distinct: set[str] = set()
    if isinstance(payload, dict):
        for daemon_type, version_counts in payload.items():
            if daemon_type == "overall" or not isinstance(version_counts, dict):
                continue
            versions_for_type = set()
            for version_string in version_counts:
                match = _VERSION_RE.search(version_string)
                if match:
                    versions_for_type.add(match.group(1))
                    distinct.add(match.group(1))
            per_type[daemon_type] = sorted(versions_for_type)
    return {
        "raw": payload,
        "per_type": per_type,
        "distinct_versions": sorted(distinct),
        "is_mixed": len(distinct) > 1,
        "current_version": next(iter(distinct)) if len(distinct) == 1 else None,
    }


def get_upgrade_status() -> dict:
    """`ceph orch upgrade status` — live progress of an in-flight (or just-
    finished/never-started) upgrade, queried directly from the cluster each
    call, independent of any Action/Incident row in this app's own DB (an
    upgrade cephadm is driving in the background is real regardless of
    whether THIS app's DB still thinks an Action is EXECUTED/pending).

    Requires ceph_exec_mode=cephadm — `ceph orch` commands only work at all
    on a cephadm-managed cluster.
    """
    if settings.ceph_exec_mode != "cephadm":
        raise CephQueryError("ceph orch upgrade status requires ceph_exec_mode=cephadm")
    _, payload = run_ceph_json_command("ceph orch upgrade status")
    return payload if isinstance(payload, dict) else {"raw_output": payload}


def _run_upgrade_control_command(inner_command: str) -> None:
    if settings.ceph_exec_mode != "cephadm":
        raise CephQueryError(f"{inner_command} requires ceph_exec_mode=cephadm")
    nodes = get_mon_nodes()
    if not nodes:
        raise CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
    command = build_exec_command(settings.ceph_exec_mode, settings.ceph_container_name, inner_command)
    errors = []
    for host in nodes:
        try:
            _run_remote_command(host, command, CEPHADM_COMMAND_TIMEOUT_SECONDS)
            return
        except Exception as exc:
            logger.warning("_run_upgrade_control_command: %s failed: %s", host, exc)
            errors.append(f"{host}: {exc}")
    raise CephQueryError(f"All MON nodes failed: {'; '.join(errors)}")


def pause_upgrade() -> None:
    """Operator's off-switch for an in-flight upgrade (see module note
    above on why this isn't gated by the kill-switch) — pauses cephadm's
    own upgrade loop after whichever daemon it's currently mid-upgrading
    finishes; does not roll anything back."""
    _run_upgrade_control_command("ceph orch upgrade pause")


def resume_upgrade() -> None:
    _run_upgrade_control_command("ceph orch upgrade resume")
