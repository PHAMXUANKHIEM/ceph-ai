import re
import shlex

from worker.executor.ssh_executor import ExecutorError, execute_command

# v1 (Story 3.2): the lab cluster was torn down mid-development, so this
# couldn't be verified against a real host's NTP setup at write time. Written
# defensively — tries the common tools in order rather than assuming one —
# and MUST be re-verified with `pytest -m live` once a lab cluster exists
# again. If it turns out to be wrong, only this dict needs to change.
#
# `pg_repair_force` is deliberately NOT added: a real `ceph pg repair`
# needs a specific PG id, and nothing in this codebase extracts one from a
# detected Incident — guessing one would be worse than having no command at
# all for a RISKY action. `investigate_manually` intentionally has no
# command either (see action_policy.yaml's own comment: "no automated
# action applies"). Both fall through `get_command()`'s ExecutorError, which
# worker/llm/router_client.py's approved-execution path already turns into a
# clear FAILED status instead of guessing — see deferred-work.md.
COMMANDS: dict[str, str] = {
    "resync_ntp": (
        "if command -v chronyc >/dev/null 2>&1; then chronyc -a makestep; "
        "elif command -v ntpdate >/dev/null 2>&1; then ntpdate -u pool.ntp.org; "
        "elif systemctl list-unit-files 2>/dev/null | grep -q systemd-timesyncd; then "
        "timedatectl set-ntp true && systemctl restart systemd-timesyncd; "
        "else echo 'no supported NTP tool found' >&2; exit 1; fi"
    ),
}

# Matches the FIRST column of a `systemctl` list-units line (the unit name),
# e.g. "ceph-<fsid>@osd.0.service" (cephadm) or "ceph-osd@0.service"
# (traditional/bare-metal package install) — both end in ".service" and
# have no leading whitespace once matched.
_SYSTEMCTL_UNIT_RE = re.compile(r"^\s*(\S+\.service)\b")

# Substring match, not a strict positional parse — deliberately, because
# where the daemon type sits in the unit name differs by deployment style
# (cephadm: "ceph-<fsid>@osd.0.service" — after the "@"; traditional:
# "ceph-osd@0.service" — before it). Verified against a real cephadm/reef
# cluster this doesn't false-positive on its other daemon units (grafana,
# prometheus, node-exporter, ceph-exporter, alertmanager, crash — none of
# them contain "osd"/"mon"/"mgr").
_UNIT_TYPE_MARKERS = ("osd", "mon", "mgr")


def _classify_ceph_unit(unit_name: str) -> str | None:
    lowered = unit_name.lower()
    for daemon_type in _UNIT_TYPE_MARKERS:
        if daemon_type in lowered:
            return daemon_type
    return None


def _discover_ceph_units(host: str) -> dict[str, list[str]]:
    """Discovers this host's Ceph systemd units via `systemctl | grep ceph`,
    classified by daemon type (osd/mon/mgr) — works regardless of HOW this
    cluster is deployed (cephadm, a traditional systemd package install,
    docker/podman wrapped in a systemd unit, ...), since every one of those
    ultimately runs its daemons as systemd units on the host. Replaces the
    earlier cephadm-only discovery (`cephadm ls --no-detail`), which failed
    outright for any non-cephadm deployment.

    `|| true` because `grep` exits 1 (not an error) when a host happens to
    have zero matching lines — without it, execute_command would raise
    ExecutorError for a perfectly legitimate "no ceph units here" result.
    """
    output = execute_command(host, "systemctl | grep ceph || true")
    units: dict[str, list[str]] = {"osd": [], "mon": [], "mgr": []}
    for line in output.splitlines():
        match = _SYSTEMCTL_UNIT_RE.match(line)
        if not match:
            continue
        unit_name = match.group(1)
        daemon_type = _classify_ceph_unit(unit_name)
        if daemon_type in units:
            units[daemon_type].append(unit_name)
    return units


def _restart_osd_daemon_command(host: str | None) -> str:
    if host is None:
        raise ExecutorError(
            "restart_osd_daemon needs a specific host to discover its OSD systemd unit(s) via "
            "`systemctl` — no host given"
        )
    osd_units = _discover_ceph_units(host)["osd"]
    if not osd_units:
        raise ExecutorError(f"{host}: no ceph osd systemd unit found via `systemctl | grep ceph`")
    return " && ".join(f"systemctl restart {shlex.quote(name)}" for name in osd_units)


# --- Management actions (2026-07-23) --------------------------------------
#
# Chat-with-AI-only (dashboard/chat_client.py) — see action_policy.yaml's
# `management_action_ids:` comment for why these are a separate family from
# COMMANDS above. Unlike COMMANDS' static strings, each of these needs a
# caller-supplied parameter (pool name, pg_num, size, osd id); AD-5's "never
# parse free text" posture applies here exactly as it does to action_id
# itself — every parameter is type/range validated BEFORE it ever reaches a
# command string, and shlex.quote()'d into it, so a hallucinated or hostile
# value can fail loudly (ExecutorError) but can never inject a second shell
# command or flag.
#
# Ceph pool names aren't formally restricted to this charset, but requiring
# "starts with alnum, then alnum/underscore/dot/dash" rules out a name that
# would parse as a CLI flag (e.g. a pool literally named "--yes-i-really-
# really-mean-it") while still covering every realistic pool name.
_POOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

# Bounds are deliberately generous (not tuned to any specific cluster's real
# capacity) — they exist only to reject an obviously-wrong hallucinated
# value (e.g. pg_num=99999999999), not to second-guess a legitimate
# operator-approved request. Cluster-side, Ceph enforces its own real limits
# regardless (e.g. mon_max_pg_per_osd) and this command still fails loudly
# via ExecutorError's non-zero-exit handling if that's exceeded.
_PG_NUM_RANGE = (1, 65536)
_POOL_SIZE_RANGE = (1, 10)
_OSD_ID_RANGE = (0, 9999)


def _require_pool_name(params: dict) -> str:
    pool_name = params.get("pool_name")
    if not isinstance(pool_name, str) or not _POOL_NAME_RE.match(pool_name):
        raise ExecutorError(f"invalid or missing pool_name: {pool_name!r}")
    return pool_name


def _require_int(params: dict, key: str, bounds: tuple[int, int]) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ExecutorError(f"invalid or missing {key}: {value!r} (must be an integer)")
    low, high = bounds
    if not (low <= value <= high):
        raise ExecutorError(f"{key}={value} out of allowed range [{low}, {high}]")
    return value


def _create_pool_command(params: dict) -> str:
    pool_name = _require_pool_name(params)
    pg_num = _require_int(params, "pg_num", _PG_NUM_RANGE)
    return f"ceph osd pool create {shlex.quote(pool_name)} {pg_num}"


def _delete_pool_command(params: dict) -> str:
    """2026-07-23: Ceph itself refuses `ceph osd pool delete` outright
    (`Error EPERM: pool deletion is disabled`) unless the cluster-wide
    `mon_allow_pool_delete` config is `true` — verified live, this is what
    made the first real delete_pool attempt fail. Per operator's explicit
    request: this command now brackets the actual delete with enabling that
    flag just before and disabling it again right after, rather than
    requiring it to be left permanently on. Structure (single command
    string, sent as-is to `execute_command`, same as
    `_restart_osd_daemon_command`'s multi-clause shell strings):

    1. `ceph config set mon mon_allow_pool_delete true` — if THIS fails
       (e.g. permission issue), `&&` short-circuits: delete is never
       attempted and the flag is never touched, so nothing needs resetting.
    2. A subshell that runs the real delete, captures its exit code,
       ALWAYS resets the flag back to `false` regardless of whether the
       delete itself succeeded or failed (a failed delete must not leave
       the cluster-wide safety flag stuck on), and re-exits with the
       delete's own exit code — so ExecutorError's success/failure
       detection (worker/executor/ssh_executor.py) still reflects the
       delete itself, not the housekeeping around it.
    """
    pool_name = _require_pool_name(params)
    quoted = shlex.quote(pool_name)
    delete_cmd = f"ceph osd pool delete {quoted} {quoted} --yes-i-really-really-mean-it"
    return (
        "ceph config set mon mon_allow_pool_delete true && "
        f"({delete_cmd}; rc=$?; "
        "ceph config set mon mon_allow_pool_delete false || "
        "echo 'WARNING: failed to reset mon_allow_pool_delete to false' >&2; "
        "exit $rc)"
    )


def _set_pool_size_command(params: dict) -> str:
    pool_name = _require_pool_name(params)
    size = _require_int(params, "size", _POOL_SIZE_RANGE)
    return f"ceph osd pool set {shlex.quote(pool_name)} size {size}"


def _set_pool_pg_num_command(params: dict) -> str:
    pool_name = _require_pool_name(params)
    pg_num = _require_int(params, "pg_num", _PG_NUM_RANGE)
    return f"ceph osd pool set {shlex.quote(pool_name)} pg_num {pg_num}"


def _mark_osd_command(verb: str, params: dict) -> str:
    osd_id = _require_int(params, "osd_id", _OSD_ID_RANGE)
    return f"ceph osd {verb} {osd_id}"


# Same charset as pool names — Ceph app names are conventionally short
# lowercase tokens (rbd/cephfs/rgw) but also accept arbitrary custom names;
# this only rules out something that would parse as a flag.
_APP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def _require_app_name(params: dict) -> str:
    app_name = params.get("app_name")
    if not isinstance(app_name, str) or not _APP_NAME_RE.match(app_name):
        raise ExecutorError(f"invalid or missing app_name: {app_name!r}")
    return app_name


def _enable_pool_application_command(params: dict) -> str:
    pool_name = _require_pool_name(params)
    app_name = _require_app_name(params)
    # --yes-i-really-mean-it is required by Ceph only for a non-standard
    # app name (anything other than rbd/cephfs/rgw) — harmless to always
    # include, since Ceph does not reject the flag for a standard name.
    return (
        f"ceph osd pool application enable "
        f"{shlex.quote(pool_name)} {shlex.quote(app_name)} --yes-i-really-mean-it"
    )


_MANAGEMENT_COMMAND_BUILDERS = {
    "create_pool": _create_pool_command,
    "delete_pool": _delete_pool_command,
    "set_pool_size": _set_pool_size_command,
    "set_pool_pg_num": _set_pool_pg_num_command,
    "mark_osd_out": lambda params: _mark_osd_command("out", params),
    "mark_osd_in": lambda params: _mark_osd_command("in", params),
    "mark_osd_down": lambda params: _mark_osd_command("down", params),
    "enable_pool_application": _enable_pool_application_command,
}


def get_command(action_id: str, host: str | None = None, params: dict | None = None) -> str:
    """No command defined -> ExecutorError, never a silent no-op or a guess
    at what to run — an unrecognized action_id must never execute anything.

    `host` is only actually used by restart_osd_daemon (systemd unit names
    must be discovered per-host, see `_restart_osd_daemon_command`) — every
    other action_id's command is the same regardless of host, so callers
    that don't have a specific host yet (e.g. building a preview string
    before any node is chosen) may omit it.

    `params` is only used by the management actions (see
    `_MANAGEMENT_COMMAND_BUILDERS` above) — missing/invalid required keys
    raise ExecutorError the same way a missing host does for
    restart_osd_daemon, never a guess.
    """
    if action_id == "restart_osd_daemon":
        return _restart_osd_daemon_command(host)
    if action_id in _MANAGEMENT_COMMAND_BUILDERS:
        return _MANAGEMENT_COMMAND_BUILDERS[action_id](params or {})
    if action_id not in COMMANDS:
        raise ExecutorError(f"no Command defined for action_id={action_id!r}")
    return COMMANDS[action_id]


def has_command(action_id: str) -> bool:
    """Whether `action_id` is EVER capable of resolving to a real command —
    independent of host/params validity. False only for an action_id that
    deliberately has no automated remediation at all (investigate_manually,
    pg_repair_force — see COMMANDS' module docstring above for why).

    2026-07-23: added for dashboard/routes/actions.py::approve_action —
    approving investigate_manually/pg_repair_force previously always routed
    to Worker execution, which always raised ExecutorError (nothing to
    run), marking the Action FAILED and the Incident FAILED right along
    with it — which made the Dashboard's cluster-status badge report "ERR"
    for something that was never a real execution failure, just "no
    automated fix exists". This lets that call site short-circuit BEFORE
    ever creating a doomed-to-fail APPROVED Action.
    """
    return (
        action_id == "restart_osd_daemon"
        or action_id in _MANAGEMENT_COMMAND_BUILDERS
        or action_id in COMMANDS
    )
