import base64
import re
import shlex

from config.settings import settings
from shared.cluster_nodes import configured_nodes
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
    # Hard reboot is deliberately RISKY and approval-gated. Redirecting all
    # file descriptors lets SSH return before systemd tears down the node.
    "hard_reboot_node": "nohup systemctl reboot --force --force >/dev/null 2>&1 &",
    "resync_ntp": (
        "if command -v chronyc >/dev/null 2>&1; then "
        "(systemctl start chronyd 2>/dev/null || systemctl start chrony 2>/dev/null || true) && "
        "chronyc -a makestep; "
        "elif command -v ntpdate >/dev/null 2>&1; then ntpdate -u pool.ntp.org; "
        "elif systemctl list-unit-files 2>/dev/null | grep -q systemd-timesyncd; then "
        "timedatectl set-ntp true && systemctl restart systemd-timesyncd; "
        "else echo 'no supported NTP tool found' >&2; exit 1; fi"
    ),
    # Idempotent cluster-wide repair for MON_MSGR2_NOT_ENABLED.  Ceph keeps
    # the existing v1 listener, so enabling v2 does not strand v1 clients.
    "enable_mon_msgr2": "ceph mon enable-msgr2",
    # Story A (Crash-module visibility, 2026-08-01): remediates RECENT_CRASH
    # — unlike pg_repair_force (deliberately NOT given a command, see this
    # dict's own comment above), `ceph crash archive-all` takes NO id
    # argument at all, so there's nothing to guess: it just acknowledges
    # every currently-pending crash report cluster-wide. Runs bare (no
    # container/exec-mode wrapping) same as every other COMMANDS/
    # management-action entry here — this codebase assumes `ceph` is
    # reachable directly via SSH on whichever host it's targeted at
    # (watcher/collector.py::_collect_recent_crash_excerpt resolves that
    # host to a MON node already verified reachable, same as
    # run_ceph_json_command's other callers).
    "crash_archive_all": "ceph crash archive-all",
}

# Matches the FIRST column of a `systemctl` list-units line (the unit name),
# e.g. "ceph-<fsid>@osd.0.service" (cephadm) or "ceph-osd@0.service"
# (traditional/bare-metal package install) — both end in ".service" and
# have no leading whitespace once matched.
_SYSTEMCTL_UNIT_RE = re.compile(r"^\s*(\S+\.service)\b")
_OSD_UNIT_ID_RE = re.compile(r"osd[.@](\d+)\b", re.IGNORECASE)
_RGW_SECTION_RE = re.compile(r"^client\.rgw\.[A-Za-z0-9_.-]{1,180}$")
_RGW_DAEMON_RE = re.compile(r"^rgw\.[A-Za-z0-9_.-]{1,180}$")


def _remove_invalid_rgw_default_key_command(params: dict) -> str:
    section = str(params.get("section") or "")
    daemon_names = params.get("daemon_names")
    if not _RGW_SECTION_RE.fullmatch(section):
        raise ExecutorError("invalid or missing RGW config section")
    if not isinstance(daemon_names, list) or not daemon_names or not all(
        isinstance(name, str) and _RGW_DAEMON_RE.fullmatch(name) for name in daemon_names
    ):
        raise ExecutorError("invalid or missing RGW daemon names")
    quoted_section = shlex.quote(section)
    checks = (
        f"test \"$(ceph config get {quoted_section} rgw_crypt_sse_s3_backend)\" = vault && "
        f"ceph config rm {quoted_section} rgw_crypt_default_encryption_key"
    )
    restarts = " && ".join(
        f"ceph orch daemon restart {shlex.quote(name)}" for name in daemon_names
    )
    return f"{checks} && {restarts}"

# Substring match, not a strict positional parse — deliberately, because
# where the daemon type sits in the unit name differs by deployment style
# (cephadm: "ceph-<fsid>@osd.0.service" — after the "@"; traditional:
# "ceph-osd@0.service" — before it). Verified against a real cephadm/reef
# cluster this doesn't false-positive on its other daemon units (grafana,
# prometheus, node-exporter, ceph-exporter, alertmanager, crash — none of
# them contain "osd"/"mon"/"mgr").
#
# 2026-07-23: "mds"/"rgw" added for the package-based (ceph-deploy) upgrade
# path (worker/executor/commands.py's _package_upgrade_and_restart_command
# below) — restart_osd_daemon itself still only ever reads units["osd"], so
# this extension changes nothing for it.
_UNIT_TYPE_MARKERS = ("osd", "mon", "mgr", "mds", "rgw")


def _classify_ceph_unit(unit_name: str) -> str | None:
    lowered = unit_name.lower()
    for daemon_type in _UNIT_TYPE_MARKERS:
        if daemon_type in lowered:
            return daemon_type
    return None


def _discover_ceph_units(
    host: str, user: str | None = None, key_path: str | None = None
) -> dict[str, list[str]]:
    """Discovers this host's Ceph systemd units via `systemctl --all | grep
    ceph`, classified by daemon type (osd/mon/mgr) — works regardless of HOW
    this cluster is deployed (cephadm, a traditional systemd package
    install, docker/podman wrapped in a systemd unit, ...), since every one
    of those ultimately runs its daemons as systemd units on the host.
    Replaces the earlier cephadm-only discovery (`cephadm ls --no-detail`),
    which failed outright for any non-cephadm deployment.

    `--all` is required, not optional: bare `systemctl` (equivalent to
    `systemctl list-units`) hides any unit that is currently INACTIVE,
    showing only active/failed ones. A package upgrade's own RPM/APT
    postinst/preun scriptlet routinely stops a running templated unit
    (`ceph-mgr@<instance>.service`) before swapping its binary and does NOT
    restart it (it has no way to know which instance was running) — exactly
    the moment this function is called, right after install. Verified live
    (2026-08-05): a real cluster's `ceph-mgr@...` unit was correctly
    installed and present but stopped at that instant, so bare `systemctl`
    found zero "mgr" units and the restart step silently skipped it as "no
    unit found" even though the unit existed and only needed a `start`.

    `|| true` because `grep` exits 1 (not an error) when a host happens to
    have zero matching lines — without it, execute_command would raise
    ExecutorError for a perfectly legitimate "no ceph units here" result.
    """
    if user is None and key_path is None:
        output = execute_command(host, "systemctl --all | grep ceph || true")
    else:
        output = execute_command(host, "systemctl --all | grep ceph || true", user, key_path)
    units: dict[str, list[str]] = {"osd": [], "mon": [], "mgr": [], "mds": [], "rgw": []}
    for line in output.splitlines():
        match = _SYSTEMCTL_UNIT_RE.match(line)
        if not match:
            continue
        unit_name = match.group(1)
        daemon_type = _classify_ceph_unit(unit_name)
        if daemon_type in units:
            units[daemon_type].append(unit_name)
    return units


def _restart_osd_daemon_command(host: str | None, params: dict | None = None) -> str:
    if host is None:
        raise ExecutorError(
            "restart_osd_daemon needs a specific host to discover its OSD systemd unit(s) via "
            "`systemctl` — no host given"
        )
    cephadm_ids = (params or {}).get("cephadm_osd_ids")
    if cephadm_ids:
        ids = sorted({int(value) for value in cephadm_ids})
        return " && ".join(f"ceph orch daemon restart osd.{osd_id}" for osd_id in ids)
    osd_units = _discover_ceph_units(host)["osd"]
    ids_by_host = (params or {}).get("osd_ids_by_host") or {}
    requested = ids_by_host.get(host) if isinstance(ids_by_host, dict) else None
    if requested is not None:
        requested_ids = {int(value) for value in requested}
        osd_units = [
            unit for unit in osd_units
            if any(int(value) in requested_ids for value in _OSD_UNIT_ID_RE.findall(unit))
        ]
        found_ids = {
            int(value) for unit in osd_units for value in _OSD_UNIT_ID_RE.findall(unit)
        }
        if found_ids != requested_ids:
            missing = sorted(requested_ids - found_ids)
            raise ExecutorError(f"{host}: verified OSD unit(s) not found for ids {missing}")
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


# RBD trash entry ids are hex-ish tokens `rbd trash ls` itself generates
# (e.g. "1234567890ab") — same "must start with alnum, reject anything that
# could parse as a CLI flag" reasoning as _POOL_NAME_RE above (a bare
# `[A-Za-z0-9_-]` class alone would still accept "--force", since every one
# of those characters is individually allowed).
_TRASH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_RBD_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_RBD_SIZE_MIB_RANGE = (1, 64 * 1024 * 1024)


def _require_trash_id(params: dict) -> str:
    trash_id = params.get("trash_id")
    if not isinstance(trash_id, str) or not _TRASH_ID_RE.match(trash_id):
        raise ExecutorError(f"invalid or missing trash_id: {trash_id!r}")
    return trash_id


def _require_pool_name(params: dict) -> str:
    pool_name = params.get("pool_name")
    if not isinstance(pool_name, str) or not _POOL_NAME_RE.match(pool_name):
        raise ExecutorError(f"invalid or missing pool_name: {pool_name!r}")
    return pool_name


def _require_rbd_image(params: dict) -> str:
    image = params.get("image")
    if not isinstance(image, str) or not _RBD_IMAGE_RE.fullmatch(image):
        raise ExecutorError(f"invalid or missing RBD image: {image!r}")
    return image


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
    command = f"ceph osd pool create {shlex.quote(pool_name)} {pg_num}"
    app_name = params.get("app_name")
    if app_name is not None:
        if app_name not in {"rbd", "cephfs", "rgw"}:
            raise ExecutorError(f"invalid app_name: {app_name!r}")
        command += (
            " && ceph osd pool application enable "
            f"{shlex.quote(pool_name)} {shlex.quote(app_name)}"
        )
    return command


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
    adjustments = params.get("adjustments") if isinstance(params, dict) else None
    if adjustments is not None:
        if not isinstance(adjustments, list) or not adjustments:
            raise ExecutorError("adjustments must be a non-empty list")
        commands = []
        for adjustment in adjustments:
            if not isinstance(adjustment, dict):
                raise ExecutorError(f"invalid pool PG adjustment: {adjustment!r}")
            pool_name = _require_pool_name(adjustment)
            pg_num = _require_int(adjustment, "pg_num", _PG_NUM_RANGE)
            commands.append(f"ceph osd pool set {shlex.quote(pool_name)} pg_num {pg_num}")
        return " && ".join(commands)

    pool_name = _require_pool_name(params)
    pg_num = _require_int(params, "pg_num", _PG_NUM_RANGE)
    return f"ceph osd pool set {shlex.quote(pool_name)} pg_num {pg_num}"


def _enable_pool_pg_autoscaler_command(params: dict) -> str:
    pools = params.get("pools") if isinstance(params, dict) else None
    if not isinstance(pools, list) or not pools:
        raise ExecutorError("pools must be a non-empty list")
    names = []
    for value in pools:
        names.append(_require_pool_name({"pool_name": value}))
    return " && ".join(
        f"ceph osd pool set {shlex.quote(name)} pg_autoscale_mode on" for name in names
    )


def _reshard_rgw_bucket_command(params: dict) -> str:
    bucket = params.get("bucket_name") if isinstance(params, dict) else None
    if not isinstance(bucket, str) or not bucket or len(bucket) > 255 or bucket.startswith("-"):
        raise ExecutorError(f"invalid or missing bucket_name: {bucket!r}")
    shards = _require_int(params, "num_shards", (2, 19999))
    command = (
        "radosgw-admin bucket reshard "
        f"--bucket={shlex.quote(bucket)} --num-shards={shards}"
    )
    pg_id = params.get("pg_id")
    if pg_id is not None:
        if not isinstance(pg_id, str) or not re.fullmatch(r"[0-9]+\.[0-9a-fA-F]+", pg_id):
            raise ExecutorError(f"invalid pg_id: {pg_id!r}")
        command += f" && ceph pg {shlex.quote(pg_id)} deep_scrub"
    return command


def _deep_scrub_omap_pg_command(params: dict) -> str:
    pg_ids = params.get("pg_ids") if isinstance(params, dict) else None
    if pg_ids is not None:
        if not isinstance(pg_ids, list) or not 1 <= len(pg_ids) <= 4:
            raise ExecutorError("pg_ids must contain between 1 and 4 PG ids")
        commands = []
        for pg_id in pg_ids:
            if not isinstance(pg_id, str) or not re.fullmatch(r"[0-9]+\.[0-9a-fA-F]+", pg_id):
                raise ExecutorError(f"invalid pg_id: {pg_id!r}")
            commands.append(f"ceph pg {shlex.quote(pg_id)} deep_scrub")
        return " && ".join(commands)
    pg_id = params.get("pg_id") if isinstance(params, dict) else None
    if not isinstance(pg_id, str) or not re.fullmatch(r"[0-9]+\.[0-9a-fA-F]+", pg_id):
        raise ExecutorError(f"invalid or missing pg_id: {pg_id!r}")
    return f"ceph pg {shlex.quote(pg_id)} deep_scrub"


def _edit_pool_command(params: dict) -> str:
    pool_name = _require_pool_name(params)
    size = _require_int(params, "size", _POOL_SIZE_RANGE)
    pg_num = _require_int(params, "pg_num", _PG_NUM_RANGE)
    quoted = shlex.quote(pool_name)
    return f"ceph osd pool set {quoted} size {size} && ceph osd pool set {quoted} pg_num {pg_num}"


def _scrub_pool_command(params: dict) -> str:
    return f"ceph osd pool scrub {shlex.quote(_require_pool_name(params))}"


def _set_pool_protection_command(params: dict) -> str:
    pool_name = _require_pool_name(params)
    protected = params.get("protected")
    if not isinstance(protected, bool):
        raise ExecutorError(f"invalid or missing protected: {protected!r} (must be boolean)")
    return f"ceph osd pool set {shlex.quote(pool_name)} nodelete {'true' if protected else 'false'}"


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


def _finalize_pacific_osd_release_command(params: dict) -> str:
    """Raise the cluster compatibility floor only after all OSDs are Pacific+.

    The precondition is intentionally checked from live cluster evidence by
    Chat before proposal. Ceph performs its own authoritative validation when
    this command runs, and the action remains RISKY/approval-gated.
    """
    return "ceph osd require-osd-release pacific --yes-i-really-mean-it"


_CEPH_RELEASE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


def _finalize_osd_release_command(params: dict) -> str:
    """Build the approval-gated OSD compatibility-floor command."""
    release = params.get("release")
    if not isinstance(release, str) or not _CEPH_RELEASE_RE.fullmatch(release):
        raise ExecutorError(f"invalid or missing Ceph release: {release!r}")
    return f"ceph osd require-osd-release {shlex.quote(release)}"


def _rbd_trash_remove_command(params: dict) -> str:
    """Permanently deletes an RBD image an operator already soft-deleted
    into a pool's trash (dashboard/routes/volumes.py's "Xoá" button on the
    /volumes page's Trash section — watcher/ceph_client.py::query_rbd_trash
    lists what's in there). Deliberately a bare `rbd trash rm`, no `--force`
    — an image trash still has an active watcher (e.g. a VM still has it
    mapped) refuses to remove with a clear error rather than this command
    silently forcing it out from under a running VM."""
    pool_name = _require_pool_name(params)
    trash_id = _require_trash_id(params)
    return f"rbd trash rm {shlex.quote(pool_name)}/{shlex.quote(trash_id)}"


def _rbd_create_volume_command(params: dict) -> str:
    pool = _require_pool_name(params)
    image = _require_rbd_image(params)
    size_mib = _require_int(params, "size_mib", _RBD_SIZE_MIB_RANGE)
    spec = shlex.quote(f"{pool}/{image}")
    return f"rbd create --size {size_mib} {spec} && rbd info {spec} --format json"


def _rbd_resize_volume_command(params: dict) -> str:
    pool = _require_pool_name(params)
    image = _require_rbd_image(params)
    size_mib = _require_int(params, "size_mib", _RBD_SIZE_MIB_RANGE)
    spec = shlex.quote(f"{pool}/{image}")
    # Deliberately no --allow-shrink: even if the image changes between
    # proposal and approval, Ceph itself refuses any accidental shrink.
    return f"rbd resize --size {size_mib} {spec} && rbd info {spec} --format json"


def _rbd_rename_volume_command(params: dict) -> str:
    pool = _require_pool_name(params)
    image = _require_rbd_image(params)
    new_image = params.get("new_image")
    if not isinstance(new_image, str) or not _RBD_IMAGE_RE.fullmatch(new_image):
        raise ExecutorError(f"invalid or missing destination RBD image: {new_image!r}")
    if new_image == image:
        raise ExecutorError("destination RBD image must differ from source")
    source = shlex.quote(f"{pool}/{image}")
    destination = shlex.quote(f"{pool}/{new_image}")
    return f"rbd mv {source} {destination} && rbd info {destination} --format json"


def _rbd_trash_move_volume_command(params: dict) -> str:
    pool = _require_pool_name(params)
    image = _require_rbd_image(params)
    spec = shlex.quote(f"{pool}/{image}")
    return f"rbd trash mv {spec} && rbd trash ls {shlex.quote(pool)} --format json"


def _rbd_trash_restore_volume_command(params: dict) -> str:
    pool = _require_pool_name(params)
    trash_id = _require_trash_id(params)
    image = _require_rbd_image(params)
    trash_spec = shlex.quote(f"{pool}/{trash_id}")
    restored_spec = shlex.quote(f"{pool}/{image}")
    return f"rbd trash restore {trash_spec} --image {shlex.quote(image)} && rbd info {restored_spec} --format json"


def _rbd_trash_purge_all_command(params: dict) -> str:
    pool = _require_pool_name(params)
    trash_ids = params.get("trash_ids")
    if not isinstance(trash_ids, list) or not trash_ids or len(trash_ids) > 500:
        raise ExecutorError("trash_ids must be a non-empty list with at most 500 entries")
    validated: list[str] = []
    for trash_id in trash_ids:
        validated.append(_require_trash_id({"trash_id": trash_id}))
    if len(set(validated)) != len(validated):
        raise ExecutorError("trash_ids must not contain duplicates")
    removals = [
        f"rbd trash rm {shlex.quote(pool)}/{shlex.quote(trash_id)} --force"
        for trash_id in validated
    ]
    return " && ".join([*removals, f"rbd trash ls {shlex.quote(pool)} --format json"])


_OPENSTACK_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)


def _cinder_attachment_command(params: dict, operation: str) -> str:
    volume_id = params.get("volume_id")
    server_id = params.get("server_id")
    openrc_path = params.get("openrc_path")
    if not isinstance(volume_id, str) or not _OPENSTACK_UUID_RE.fullmatch(volume_id):
        raise ExecutorError(f"invalid or missing Cinder volume ID: {volume_id!r}")
    if not isinstance(server_id, str) or not _OPENSTACK_UUID_RE.fullmatch(server_id):
        raise ExecutorError(f"invalid or missing Nova server ID: {server_id!r}")
    if not isinstance(openrc_path, str) or not openrc_path.startswith("/") or "\x00" in openrc_path:
        raise ExecutorError("openrc_path must be an absolute path")
    verb = "add" if operation == "attach" else "remove"
    inner = (
        f". {shlex.quote(openrc_path)} >/dev/null 2>&1 && "
        f"openstack server {verb} volume {shlex.quote(server_id)} {shlex.quote(volume_id)} && "
        f"openstack volume show {shlex.quote(volume_id)} -f json"
    )
    return "sh -c " + shlex.quote(inner)


def _cinder_attach_volume_command(params: dict) -> str:
    return _cinder_attachment_command(params, "attach")


def _cinder_detach_volume_command(params: dict) -> str:
    return _cinder_attachment_command(params, "detach")


_CINDER_SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _cinder_create_snapshot_command(params: dict) -> str:
    volume_id = params.get("volume_id")
    snapshot_name = params.get("snapshot_name")
    openrc_path = params.get("openrc_path")
    if not isinstance(volume_id, str) or not _OPENSTACK_UUID_RE.fullmatch(volume_id):
        raise ExecutorError(f"invalid or missing Cinder volume ID: {volume_id!r}")
    if not isinstance(snapshot_name, str) or not _CINDER_SNAPSHOT_NAME_RE.fullmatch(snapshot_name):
        raise ExecutorError(f"invalid or missing Cinder snapshot name: {snapshot_name!r}")
    if not isinstance(openrc_path, str) or not openrc_path.startswith("/") or "\x00" in openrc_path:
        raise ExecutorError("openrc_path must be an absolute path")
    force = " --force" if params.get("force") is True else ""
    inner = (
        f". {shlex.quote(openrc_path)} >/dev/null 2>&1 && "
        f"openstack volume snapshot create --volume {shlex.quote(volume_id)}{force} "
        f"{shlex.quote(snapshot_name)} -f json >/dev/null && "
        f"openstack volume snapshot list --volume {shlex.quote(volume_id)} -f json"
    )
    return "sh -c " + shlex.quote(inner)


def _execute_node_command(params: dict) -> str:
    command = params.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > 2000:
        raise ExecutorError("command must be a non-empty string of at most 2000 characters")
    if "\x00" in command or "\n" in command or "\r" in command:
        raise ExecutorError("command must be a single shell line without NUL/newline characters")
    return command.strip()


_MANAGEMENT_COMMAND_BUILDERS = {
    "edit_pool": _edit_pool_command,
    "scrub_pool": _scrub_pool_command,
    "set_pool_protection": _set_pool_protection_command,
    "execute_node_command": _execute_node_command,
    "create_pool": _create_pool_command,
    "delete_pool": _delete_pool_command,
    "set_pool_size": _set_pool_size_command,
    "set_pool_pg_num": _set_pool_pg_num_command,
    "enable_pool_pg_autoscaler": _enable_pool_pg_autoscaler_command,
    "reshard_rgw_bucket": _reshard_rgw_bucket_command,
    "deep_scrub_omap_pg": _deep_scrub_omap_pg_command,
    "mark_osd_out": lambda params: _mark_osd_command("out", params),
    "mark_osd_in": lambda params: _mark_osd_command("in", params),
    "mark_osd_down": lambda params: _mark_osd_command("down", params),
    "enable_pool_application": _enable_pool_application_command,
    "finalize_pacific_osd_release": _finalize_pacific_osd_release_command,
    "rbd_trash_remove": _rbd_trash_remove_command,
    "rbd_create_volume": _rbd_create_volume_command,
    "rbd_resize_volume": _rbd_resize_volume_command,
    "rbd_rename_volume": _rbd_rename_volume_command,
    "rbd_trash_move_volume": _rbd_trash_move_volume_command,
    "rbd_trash_restore_volume": _rbd_trash_restore_volume_command,
    "rbd_trash_purge_all": _rbd_trash_purge_all_command,
    "cinder_attach_volume": _cinder_attach_volume_command,
    "cinder_detach_volume": _cinder_detach_volume_command,
    "cinder_create_snapshot": _cinder_create_snapshot_command,
}

_INCIDENT_PARAMETER_COMMAND_BUILDERS = {
    "finalize_osd_release": _finalize_osd_release_command,
    "remove_invalid_rgw_default_key": _remove_invalid_rgw_default_key_command,
}

# 2026-08-01 (Story C, DeviceHealth-driven evacuation): a SEPARATE dict from
# _MANAGEMENT_COMMAND_BUILDERS above even though the command is identical to
# mark_osd_out's — semantic ownership matters here, not just the resulting
# shell string: management_action_ids: is Chat-with-AI's own closed enum
# (operator types the osd_id, sees the preview, confirms), while this one is
# watcher/device_health_monitor.py's own family (system-proposed, osd_id
# resolved deterministically from `ceph device ls`, always RISKY — see that
# module's docstring for why reusing mark_osd_out's action_id directly would
# have been wrong: it's classified SAFE for the chat flow's own reasons,
# which must never apply to an unattended, system-generated proposal).
_DEVICE_HEALTH_COMMAND_BUILDERS = {
    "evacuate_predicted_failing_osd": lambda params: _mark_osd_command("out", params),
}


# --- BlueStore per-pool omap quick-fix (2026-08-04) -------------------------
#
# Fixes the BLUESTORE_NO_PER_POOL_OMAP health warning ("OSD(s) reporting
# legacy (not per-pool) BlueStore omap usage stats") — stop the OSD, run
# `ceph-bluestore-tool quick-fix` against its data path, start it again.
# Verified against Ceph's own C++ source (src/os/bluestore/bluestore_tool.cc)
# that "quick-fix" is a real, distinct action from "repair" (calls
# bluestore.quick_fix(), not bluestore.repair()) — commonly recommended for
# exactly this warning in vendor/community guides, even though docs.ceph.com's
# own health-checks page currently shows "repair" instead; both are valid.
#
# NOT a "safe, run it and forget" operation — a historical ceph-bluestore-
# tool bug (multi-threaded quick-fix/repair race, ceph#41749/#41613,
# backported to nautilus/octopus) could corrupt the repair transaction
# batch, and a SEPARATE known issue could corrupt OMAP keys if invoked
# during a pre-Pacific cluster's omap-format upgrade window. Always RISKY
# (worker/policy/action_policy.yaml), never auto-executed, and — unlike
# every other per-OSD action in this file — proposed from its own dedicated
# Dashboard picker (dashboard/routes/nodes.py), not Chat-with-AI free text,
# specifically so the operator picks a REAL osd_id from `ceph osd tree`
# (watcher/ceph_client.py::list_osds()) instead of typing one that might
# not exist.
#
# Only ceph_exec_mode in (cephadm, none) are supported — see the
# ExecutorError below for why docker/podman aren't: this app tracks only a
# FIXED container NAME for those modes, not the original `docker run`
# volume-mount flags a temporary replacement container would need to
# safely touch the same OSD data while the real daemon container is
# stopped, unlike cephadm (`cephadm shell --name osd.<id>` reconstructs
# the right mounts itself) or "none" (bare-metal, no container at all).
def _bluestore_omap_quick_fix_command(host: str | None, params: dict) -> str:
    if host is None:
        raise ExecutorError("bluestore_omap_quick_fix needs a specific host")
    osd_id = _require_int(params, "osd_id", _OSD_ID_RANGE)
    path = f"/var/lib/ceph/osd/ceph-{osd_id}"
    quick_fix = f"ceph-bluestore-tool quick-fix --path {shlex.quote(path)}"

    if settings.ceph_exec_mode == "cephadm":
        daemon_name = shlex.quote(f"osd.{osd_id}")
        return " && ".join(
            [
                f"cephadm unit --name {daemon_name} stop",
                f"cephadm shell --name {daemon_name} -- {quick_fix}",
                f"cephadm unit --name {daemon_name} start",
            ]
        )
    if settings.ceph_exec_mode == "none":
        unit = shlex.quote(f"ceph-osd@{osd_id}.service")
        return " && ".join([f"systemctl stop {unit}", quick_fix, f"systemctl start {unit}"])
    raise ExecutorError(
        f"bluestore_omap_quick_fix requires ceph_exec_mode=cephadm or none (currently "
        f"{settings.ceph_exec_mode!r}) — a docker/podman deployment would need this app to "
        "know the OSD container's original volume-mount flags to safely stop it and re-run "
        "ceph-bluestore-tool against the same data, which it doesn't track"
    )


_BLUESTORE_COMMAND_BUILDERS = {
    "bluestore_omap_quick_fix": _bluestore_omap_quick_fix_command,
}


# --- Cluster Upgrade action (2026-07-23) -----------------------------------
#
# dashboard/routes/upgrade.py-only — see action_policy.yaml's
# `cluster_upgrade_action_ids:` comment for why this is a third action_id
# family. Same "validate before it ever reaches a shell string" posture as
# the management-action builders above.
_TARGET_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _require_target_version(params: dict) -> str:
    target_version = params.get("target_version")
    if not isinstance(target_version, str) or not _TARGET_VERSION_RE.match(target_version):
        raise ExecutorError(f"invalid or missing target_version: {target_version!r} (expected x.y.z)")
    return target_version


# 2026-08-04: same flags/reasoning as worker/llm/router_client.py's own
# _UPGRADE_OSD_FLAGS (that module can't import this one — worker/executor/
# is deliberately self-contained, see this function's own docstring below —
# and this one can't import that one either, since router_client.py already
# imports FROM worker.executor.commands; small, deliberate duplication
# rather than a new shared module for one 4-tuple). Verified against
# cephadm's own orchestrator source (src/pybind/mgr/cephadm/upgrade.py)
# that `ceph orch upgrade start` does NOT set/unset these itself (only
# `noautoscale`) — a still-open gap in cephadm, not something to rely on
# the orchestrator for.
_UPGRADE_OSD_FLAGS = ("noout", "noscrub", "nodeep-scrub", "nosnaptrim")


def _upgrade_ceph_cluster_command(host: str | None, params: dict) -> str:
    """`ceph orch upgrade start` requires cephadm's orchestrator — a
    non-cephadm deployment (docker/podman/none exec mode) has no
    orchestrator to drive a rolling upgrade at all, so this fails loudly
    here rather than sending a command that would just error on the
    remote end anyway.

    Sets noout/noscrub/nodeep-scrub/nosnaptrim BEFORE starting the
    upgrade, chained into the SAME `cephadm shell -- bash -c '...'`
    invocation (one shell/container spin-up instead of 5) — but does NOT
    unset them here: `ceph orch upgrade start` is fire-and-forget (cephadm's
    own mgr module drives the rest asynchronously, possibly for a long
    time), so unsetting has to happen once the upgrade actually finishes,
    which this synchronous command has no way to observe. See
    dashboard/routes/upgrade.py's "Bỏ noout/noscrub..." button
    (unset_upgrade_osd_flags_route) — a deliberate manual step for this
    path, unlike the package-based path (worker/llm/router_client.py's
    _unset_upgrade_osd_flags), which unsets automatically because ITS
    per-host loop is fully synchronous and this app's own code observes
    every step of it.

    Only the cephadm wrapping style is applied (not the full docker/podman/
    none matrix watcher/ceph_client.py::build_exec_command handles) —
    worker/executor/ is deliberately self-contained (no import from
    watcher/, see ssh_executor.py's docstring), and the cephadm-only gate
    right above means no other wrapping style is ever needed here. `host`
    is unused (the command is identical regardless of host) — accepted
    only so every _CLUSTER_UPGRADE_COMMAND_BUILDERS entry shares one
    signature (see the package-based builders below, which DO need it).
    """
    target_version = _require_target_version(params)
    exec_mode = params.get("_cluster_exec_mode", settings.ceph_exec_mode)
    if exec_mode != "cephadm":
        raise ExecutorError(
            f"upgrade_ceph_cluster requires ceph_exec_mode=cephadm (currently "
            f"{exec_mode!r}) — non-cephadm deployments have no orchestrator "
            "to drive `ceph orch upgrade`"
        )
    set_flag_commands = [f"ceph osd set {flag}" for flag in _UPGRADE_OSD_FLAGS]
    upgrade_start_command = f"ceph orch upgrade start --ceph-version {shlex.quote(target_version)}"
    chained = " && ".join([*set_flag_commands, upgrade_start_command])
    return f"cephadm shell -- bash -c {shlex.quote(chained)}"


# --- Package-based (ceph-deploy / traditional) Cluster Upgrade (2026-07-23) -
#
# For a cluster with ceph_exec_mode="none" (no cephadm orchestrator — a
# traditional/"ceph-deploy"-style package install), `ceph orch upgrade` isn't
# available at all. The Upgrade page instead offers upgrading the OS Ceph
# packages directly on each configured node (mon/mgr/osd/rgw — see
# shared/cluster_nodes.py::configured_nodes for the ordering) and restarting
# whichever daemons that node runs. Two ways to get the new packages onto
# each node: download them from download.ceph.com (this codebase adds the
# right apt/yum repo for the target release), or install from a directory of
# packages already staged on the node.
#
# IMPORTANT — unlike `ceph orch upgrade start` above (fire-and-forget; a
# cephadm mgr module supervises the rest, gating each daemon's upgrade on
# cluster health), THESE commands are run one host at a time by the exact
# same generic per-host loop every other Action uses
# (worker/llm/router_client.py::_execute_approved_action) — there is no
# orchestrator here gating progress on cluster health between hosts. If one
# host's command fails, the loop still tries the REMAINING hosts (existing,
# unchanged behavior — see that function's docstring); the only way to halt
# flipping it stops any host after the current one from being attempted. The
# Dashboard's generated plan text (dashboard/routes/upgrade.py) says this
# explicitly before the operator approves.
#
# Verified against a real cephadm/reef cluster like the rest of this file —
# but NOT against a real ceph-deploy/traditional-package cluster (this
# codebase's own lab cluster runs cephadm), so the exact repo-setup commands
# below are written defensively from Ceph's own published install
# instructions rather than from a live test, same posture COMMANDS["resync_ntp"]
# already documents for itself.
_PACKAGE_DIR_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")


def _require_package_dir(params: dict) -> str:
    package_dir = params.get("package_dir")
    if not isinstance(package_dir, str) or not _PACKAGE_DIR_RE.match(package_dir):
        raise ExecutorError(
            f"invalid or missing package_dir: {package_dir!r} (expected an absolute path)"
        )
    return package_dir


def _require_non_cephadm_exec_mode(action_id: str, params: dict | None = None) -> None:
    exec_mode = (params or {}).get("_cluster_exec_mode", settings.ceph_exec_mode)
    if exec_mode != "none":
        raise ExecutorError(
            f"{action_id} is for ceph_exec_mode=none (ceph-deploy/traditional package install) "
            f"clusters only — currently configured as {exec_mode!r}"
        )


def _restart_discovered_units_snippet(
    host: str, user: str | None = None, key_path: str | None = None
) -> str | None:
    """Restarts every Ceph systemd unit `_discover_ceph_units` finds on
    `host`, across ALL daemon types (not just OSD, unlike
    _restart_osd_daemon_command) — a package upgrade updates every Ceph
    binary on the host regardless of which daemon(s) happen to run there.
    Returns None (not an empty string) if the host runs no discoverable
    Ceph unit at all, so callers can tell "nothing to restart" apart from
    "restart nothing" — a package-only host (e.g. a client-only node,
    though none of this app's configured roles describe one) is not an
    error, just has no restart step.
    """
    discovered = _discover_ceph_units(host, user, key_path)
    all_units = [name for units in discovered.values() for name in units]
    if not all_units:
        return None
    # 2026-07-24 fix: verified live — repeated restart attempts across
    # several retried upgrade runs hit systemd's StartLimitBurst for
    # ceph-mgr@... within the same second ("Start request repeated too
    # quickly" -> "start-limit-hit"), which leaves the unit refusing to
    # start on every subsequent `systemctl restart` until something clears
    # it. `reset-failed` before each restart makes this step idempotent
    # against that state instead of silently failing again.
    return " && ".join(
        f"(systemctl reset-failed {shlex.quote(name)} 2>/dev/null; systemctl restart {shlex.quote(name)})"
        for name in all_units
    )


# --- Story 7.2 (2026-08-04): phased MON->MGR->OSD->MDS/RGW restart --------
#
# worker/llm/router_client.py's package-upgrade branch now runs install
# (all hosts) and restart (role-scoped, one daemon type at a time) as
# SEPARATE phases instead of one "install && restart everything" command
# per host — a colocated MON+OSD host must install once but restart its
# MON unit in the MON phase and its OSD unit in the OSD phase separately,
# never both together and never twice. `_restart_discovered_units_snippet`
# above (still used by the un-phased/legacy full command, see the two
# `_upgrade_ceph_cluster_package_*_command` builders below) restarts EVERY
# discovered unit in one shot — this is its daemon-type-filtered sibling.
_PHASE_INSTALL_ONLY = "install_only"
_PHASE_RESTART_ONLY = "restart_only"


def _restart_units_by_type_snippet(
    host: str, daemon_types: list[str], user: str | None = None, key_path: str | None = None
) -> str | None:
    """Like _restart_discovered_units_snippet, but restarts ONLY the
    systemd units classified under one of `daemon_types` (e.g. ["mon"]) —
    every other discovered unit on this host (e.g. its OSD unit, if
    colocated) is left untouched, restarted separately by a LATER phase's
    own call to this same function with a different daemon_types list.
    Returns None (not an empty string) if none of the requested types have
    any discovered unit on this host, same "let the caller decide it's a
    no-op, not an error" contract _restart_discovered_units_snippet already
    has — a host role-tagged for a phase (e.g. configured as MON) but with
    no ACTUAL mon systemd unit found is not an error, just nothing to do
    for that phase on that host.
    """
    discovered = _discover_ceph_units(host, user, key_path)
    matching_units = [
        name for daemon_type in daemon_types for name in discovered.get(daemon_type, [])
    ]
    if not matching_units:
        return None
    # Same StartLimitBurst reset-failed fix as _restart_discovered_units_snippet
    # above (2026-07-24) — applies identically here, same systemd behavior
    # regardless of whether all discovered units restart together or one
    # daemon-type slice restarts per phase.
    return " && ".join(
        f"(systemctl reset-failed {shlex.quote(name)} 2>/dev/null; systemctl restart {shlex.quote(name)})"
        for name in matching_units
    )


def _package_manager_branch(install_snippet_by_manager: dict[str, str]) -> str:
    """Builds one `if command -v apt-get ...; elif command -v dnf/yum ...;
    else ...; fi` shell block, same inline-detection style as
    COMMANDS["resync_ntp"] above — no separate SSH round trip just to ask
    "which package manager" before the real command.
    """
    apt_snippet = install_snippet_by_manager["apt"]
    rpm_snippet = install_snippet_by_manager["rpm"]
    return (
        "if command -v apt-get >/dev/null 2>&1; then "
        f"{apt_snippet}; "
        "elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then "
        f"{rpm_snippet}; "
        "else echo 'no supported package manager (apt-get/dnf/yum) found' >&2; exit 1; fi"
    )


def _upgrade_ceph_cluster_package_download_command(host: str | None, params: dict) -> str:
    """Adds the official Ceph package repo for the target release
    (download.ceph.com) and installs/upgrades the `ceph` package, then
    restarts whatever this host actually runs. See this section's module
    is the only mid-sequence stop).

    Story 7.2 (2026-08-04): `params["_phase"]` optionally selects a PHASED
    variant instead of the full "install && restart everything" command —
    `worker/llm/router_client.py`'s package-upgrade branch calls this with
    `_phase="install_only"` for its install phase and `_phase="restart_only"`
    + `params["_phase_daemon_types"]` (e.g. `["mon"]`) for each of its
    role-scoped restart phases. Omitting `_phase` entirely (every existing
    caller — `dashboard/routes/upgrade.py`'s propose-time preview, every
    pre-7.2 test) keeps the ORIGINAL un-phased behavior unchanged.
    """
    _require_non_cephadm_exec_mode("upgrade_ceph_cluster_package_download", params)
    if host is None:
        raise ExecutorError(
            "upgrade_ceph_cluster_package_download needs a specific host to discover its "
            "Ceph systemd unit(s) and detect its package manager — no host given"
        )
    phase = params.get("_phase")
    if phase == _PHASE_RESTART_ONLY:
        daemon_types = params.get("_phase_daemon_types") or []
        restart_snippet = _restart_units_by_type_snippet(
            host, daemon_types, params.get("_ssh_user"), params.get("_ssh_key_path")
        )
        if restart_snippet is None:
            raise ExecutorError(
                f"{host}: no matching Ceph systemd unit(s) for daemon type(s) "
                f"{daemon_types!r} to restart"
            )
        return restart_snippet

    target_version = _require_target_version(params)
    from shared.ceph_releases import codename_for_version, repo_path_version

    # codename is no longer used to build the repo PATH below (see
    # 2026-07-24 note) — this lookup only remains as a sanity check that
    # target_version is a release we actually recognize at all, guarding
    # against a typo'd/nonexistent version rather than a real repo path.
    if codename_for_version(target_version) is None:
        raise ExecutorError(
            f"no known Ceph release codename for target_version={target_version!r} — "
            "shared/ceph_releases.py may need a new entry"
        )

    # 2026-07-24 fix: verified live against download.ceph.com — the
    # CODENAME-based repo (e.g. rpm-quincy/, debian-quincy/) is a ROLLING
    # alias that only ever carries the OS versions the LATEST point release
    # of that codename still supports (e.g. rpm-quincy/el8/ is now an EMPTY
    # placeholder — Quincy dropped el8 support after an early point
    # release). The exact-VERSION repo (rpm-17.2.7/, debian-17.2.7/) is a
    # frozen per-release archive that still has el8 (and whatever else that
    # specific point release originally shipped) — using target_version
    # instead of codename here is what actually makes an upgrade to an
    # older still-supported-on-your-OS point release possible at all.
    # 2026-07-27 fix: EXCEPT Nautilus, where download.ceph.com never
    # published a per-exact-version directory at all (verified live) — only
    # the codename alias, which is safe to use forever for Nautilus
    # specifically since it's long EOL and will never get another point
    # release. repo_path_version() returns the codename only for that one
    # case; every other release still gets target_version unchanged.
    repo_path = repo_path_version(target_version)
    apt_snippet = (
        "wget -q -O- https://download.ceph.com/keys/release.asc "
        "| gpg --dearmor -o /usr/share/keyrings/ceph-archive-keyring.gpg "
        f"&& echo \"deb [signed-by=/usr/share/keyrings/ceph-archive-keyring.gpg] "
        f"https://download.ceph.com/debian-{repo_path}/ $(lsb_release -sc) main\" "
        "> /etc/apt/sources.list.d/ceph.list "
        "&& apt-get update -y && apt-get install -y ceph"
    )
    # Also needs the architecture segment (rpm-<version>/el<N>/ itself has
    # no repodata, only rpm-<version>/el<N>/<arch>/ does — verified live).
    #
    # 2026-07-24 fix: `dnf/yum config-manager --add-repo` NEVER removes a
    # previously-added repo file — it only adds a new one, auto-named from
    # the URL (e.g. download.ceph.com_rpm-quincy_el8_.repo). A retried or
    # earlier-version attempt (this feature, or a prior manual one) leaves
    # that file behind; dnf/yum refuses to install ANYTHING while ANY
    # enabled repo fails to refresh, so one stale broken repo (e.g. an
    # earlier attempt's codename-based URL, now 404 — see the target_version
    # note above) permanently blocks every future attempt on that host,
    # even ones targeting a URL that itself works fine. Verified live: this
    # exact scenario stacked up 2+ stale download.ceph.com_rpm-*.repo files
    # across 3 real nodes. Only removes files matching the pattern THIS
    # app's own add-repo calls create — never touches an operator's own
    # ceph.repo/ceph-local.repo or anything else already on the host.
    # 2026-07-24 fix: some Ceph RPMs (ceph-mgr-modules-core, several
    # python3-* deps) are noarch-only, published under a SEPARATE
    # rpm-<version>/el<N>/noarch/ directory — NOT under the arch-specific
    # one. Enabling only $(uname -m) leaves `dnf install ceph` unable to
    # resolve ceph-mgr's own dependency on ceph-mgr-modules-core at all
    # (verified live: "nothing provides ceph-mgr-modules-core..." — the
    # package genuinely exists on download.ceph.com, just in the noarch
    # repo this command wasn't enabling). Both repos must be added.
    # 2026-07-27 fix: `dnf install ceph` (bare, no version) trivially
    # resolves to the correct version for every release except Nautilus —
    # everywhere else the repo is scoped to exactly one version (see
    # `repo_path`'s own note above). rpm-nautilus/ physically hosts EVERY
    # Nautilus point release side by side and its repodata advertises all
    # of them as separate installable NEVRAs (verified live), so a bare
    # `dnf install ceph` there silently resolves to whatever is numerically
    # newest (currently 14.2.22) regardless of target_version. Pin the
    # exact version in the package name whenever repo_path had to fall back
    # to the codename, so an operator targeting an older Nautilus point
    # release actually gets that version. apt is unaffected — verified
    # live that debian-nautilus/dists/*/Packages only ever lists the
    # latest version per suite, no equivalent ambiguity there.
    rpm_package = f"ceph-{target_version}" if repo_path != target_version else "ceph"
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
        f"https://download.ceph.com/rpm-{repo_path}/el$(rpm -E %rhel)/noarch/) "
        f"&& (dnf install -y {rpm_package} || yum install -y {rpm_package})"
    )
    install_command = _package_manager_branch({"apt": apt_snippet, "rpm": rpm_snippet})

    if phase == _PHASE_INSTALL_ONLY:
        return install_command

    restart_snippet = _restart_discovered_units_snippet(
        host, params.get("_ssh_user"), params.get("_ssh_key_path")
    )
    if restart_snippet is None:
        return install_command
    return f"{install_command} && {restart_snippet}"


def _upgrade_ceph_cluster_package_local_command(host: str | None, params: dict) -> str:
    """Installs Ceph packages already staged in `package_dir` on this SAME
    host (no scp/copy step — the operator is responsible for having put the
    right packages there beforehand), then restarts whatever this host
    actually runs.

    Story 7.2: same `params["_phase"]` install_only/restart_only switch as
    `_upgrade_ceph_cluster_package_download_command` above — see its
    docstring.
    """
    _require_non_cephadm_exec_mode("upgrade_ceph_cluster_package_local", params)
    if host is None:
        raise ExecutorError(
            "upgrade_ceph_cluster_package_local needs a specific host to discover its Ceph "
            "systemd unit(s) and detect its package manager — no host given"
        )
    phase = params.get("_phase")
    if phase == _PHASE_RESTART_ONLY:
        daemon_types = params.get("_phase_daemon_types") or []
        restart_snippet = _restart_units_by_type_snippet(
            host, daemon_types, params.get("_ssh_user"), params.get("_ssh_key_path")
        )
        if restart_snippet is None:
            raise ExecutorError(
                f"{host}: no matching Ceph systemd unit(s) for daemon type(s) "
                f"{daemon_types!r} to restart"
            )
        return restart_snippet

    package_dir = _require_package_dir(params)
    quoted_dir = shlex.quote(package_dir)

    exists_check = f"[ -d {quoted_dir} ] || {{ echo '{quoted_dir}: directory not found' >&2; exit 1; }}"
    apt_snippet = f"apt-get install -y {quoted_dir}/*.deb"
    rpm_snippet = f"(dnf install -y {quoted_dir}/*.rpm || yum localinstall -y {quoted_dir}/*.rpm)"
    install_command = (
        f"{exists_check} && " + _package_manager_branch({"apt": apt_snippet, "rpm": rpm_snippet})
    )

    if phase == _PHASE_INSTALL_ONLY:
        return install_command

    restart_snippet = _restart_discovered_units_snippet(
        host, params.get("_ssh_user"), params.get("_ssh_key_path")
    )
    if restart_snippet is None:
        return install_command
    return f"{install_command} && {restart_snippet}"


_CLUSTER_UPGRADE_COMMAND_BUILDERS = {
    "upgrade_ceph_cluster": _upgrade_ceph_cluster_command,
    "upgrade_ceph_cluster_package_download": _upgrade_ceph_cluster_package_download_command,
    "upgrade_ceph_cluster_package_local": _upgrade_ceph_cluster_package_local_command,
}


# --- Ceph patch build & deploy pipeline (2026-07-24) ------------------------
#
# dashboard/routes/patch.py-only — see action_policy.yaml's `patch_action_ids:`
# comment for why this is a fourth action_id family. patch_build_and_stage's
# `host` is the separate build server (shared/cluster_nodes.py::patch_build_node(),
# NEVER a Ceph node); patch_install's `host` is a Ceph node from
# configured_nodes(), the same SSH SSRF whitelist every other Ceph-targeted
# action already uses.
_PATCH_FILENAME = "ceph-aiops-current.patch"


def _patch_build_and_stage_command(host: str | None, params: dict) -> str:
    """Writes the operator-uploaded patch to disk, applies it, runs the
    operator-configured build command, then has the BUILD SERVER ITSELF scp
    the resulting .rpm files onto every configured Ceph node's staging
    directory.

    There is no in-app file-transfer code doing that copy instead — this
    codebase has no SFTP/scp anywhere (every SSH call is exec_command-only,
    see ssh_executor.py), and built Ceph RPMs (ceph-osd/ceph-common etc. can
    be tens-hundreds of MB) are far too large for a base64-over-exec_command
    trick like the one used for the patch text itself just below (fine
    there only because patch files are small, capped at upload time).
    Requires the operator to have already placed a copy of the SAME SSH
    private key (settings.ssh_key_path) on the build server, at the same
    path — the build server's own scp calls need it to reach the Ceph
    nodes, which already trust that key's public half (Watcher/Worker
    already SSH to them with it).

    `ceph_patch_build_command` is operator-configured trusted shell text
    (same trust level as e.g. ceph_container_name elsewhere in this
    codebase — a Settings-page value, not per-request/attacker-reachable
    input) — deliberately NOT shlex.quote()'d, since it must remain
    executable shell syntax (the operator's own multi-step build
    invocation), not a single literal argument.
    """
    if host is None:
        raise ExecutorError("patch_build_and_stage needs the build server host — no host given")
    patch_content = params.get("patch_content")
    if not isinstance(patch_content, str) or not patch_content.strip():
        raise ExecutorError("patch_build_and_stage requires a non-empty patch_content param")

    source_dir = settings.ceph_patch_source_dir.strip()
    build_command = settings.ceph_patch_build_command.strip()
    output_dir = settings.ceph_patch_output_dir.strip()
    staging_dir = settings.ceph_patch_node_staging_dir.strip()
    if not source_dir or not build_command or not output_dir or not staging_dir:
        raise ExecutorError(
            "ceph_patch_source_dir/ceph_patch_build_command/ceph_patch_output_dir/"
            "ceph_patch_node_staging_dir must all be configured (Cài đặt) before proposing a "
            "patch build"
        )

    target_hosts = [n["host"] for n in configured_nodes()]
    if not target_hosts:
        raise ExecutorError("no Ceph nodes configured (shared.cluster_nodes.configured_nodes is empty)")

    patch_b64 = base64.b64encode(patch_content.encode()).decode()
    quoted_source_dir = shlex.quote(source_dir)
    patch_path = shlex.quote(f"{source_dir.rstrip('/')}/{_PATCH_FILENAME}")
    write_patch = f"base64 -d > {patch_path} <<< {shlex.quote(patch_b64)}"
    apply_patch = (
        f"cd {quoted_source_dir} && git apply --check {_PATCH_FILENAME} && git apply {_PATCH_FILENAME}"
    )

    quoted_output_dir = shlex.quote(output_dir)
    key_path = shlex.quote(settings.ssh_key_path)
    copy_steps = " && ".join(
        f"scp -o StrictHostKeyChecking=accept-new -i {key_path} {quoted_output_dir}/*.rpm "
        f"{shlex.quote(f'{settings.ssh_user}@{ceph_host}:{staging_dir}/')}"
        for ceph_host in target_hosts
    )
    return f"{write_patch} && {apply_patch} && {build_command} && {copy_steps}"


def _patch_install_command(host: str | None, params: dict) -> str:
    """Installs whatever .rpm files patch_build_and_stage already copied
    into settings.ceph_patch_node_staging_dir on this Ceph node, then
    restarts whatever this host actually runs — near-identical to
    _upgrade_ceph_cluster_package_local_command above, except the staging
    directory is a fixed app-owned constant (config/settings.py's
    ceph_patch_node_staging_dir), not an operator-typed path, since THIS
    pipeline is the one that put the files there in the first place. Same
    execution-model caveat as that function: no orchestrator gating
    mid-sequence stop (see worker/llm/router_client.py::_execute_approved_action)."""
    _require_non_cephadm_exec_mode("patch_install")
    if host is None:
        raise ExecutorError(
            "patch_install needs a specific host to discover its Ceph systemd unit(s) and "
            "detect its package manager — no host given"
        )
    staging_dir = settings.ceph_patch_node_staging_dir.strip()
    if not staging_dir:
        raise ExecutorError("ceph_patch_node_staging_dir must be configured before proposing an install")
    quoted_dir = shlex.quote(staging_dir)

    exists_check = f"[ -d {quoted_dir} ] || {{ echo '{quoted_dir}: directory not found' >&2; exit 1; }}"
    apt_snippet = f"apt-get install -y {quoted_dir}/*.deb"
    rpm_snippet = f"(dnf install -y {quoted_dir}/*.rpm || yum localinstall -y {quoted_dir}/*.rpm)"
    install_command = (
        f"{exists_check} && " + _package_manager_branch({"apt": apt_snippet, "rpm": rpm_snippet})
    )

    restart_snippet = _restart_discovered_units_snippet(host)
    if restart_snippet is None:
        return install_command
    return f"{install_command} && {restart_snippet}"


_PATCH_COMMAND_BUILDERS = {
    "patch_build_and_stage": _patch_build_and_stage_command,
    "patch_install": _patch_install_command,
}


# --- Dựng cụm Ceph tự động (2026-07-25, Story 8.1) --------------------------
#
# dashboard/routes/deploy_cluster.py-only — see action_policy.yaml's
# `cluster_deploy_action_ids:` comment for why this is a fifth action_id
# family. Unlike every other family above, the REAL execution for these 3
# action_ids is NOT a single resolved command string — it's a multi-phase,
# multi-role, cross-host-ordered sequence (SSH check -> deps -> repo ->
# packages -> MON init -> wait-for-quorum -> MGR -> OSD -> verify) that
# worker/executor/cluster_deploy.py::run() owns entirely, because
# worker/llm/router_client.py::_execute_approved_action's generic per-host
# loop (one command family, fired identically at every host, no cross-host
# ordering, no wait step) cannot express that. The three builders below are
# ONLY an illustrative preview string for the pending-approval screen
# (dashboard/routes/deploy_cluster.py's propose route, same posture as
# _safe_command_preview's existing docstring already documents for the
# Cluster Upgrade feature) — never executed, never authoritative.
def _first_node_ip_with_role(params: dict, role: str) -> str | None:
    for node in params.get("nodes") or []:
        if role in (node.get("roles") or []):
            return node.get("ip")
    return None


def _deploy_cluster_cephadm_preview_command(host: str | None, params: dict) -> str:
    version = params.get("version", "?")
    first_mon = _first_node_ip_with_role(params, "mon") or host or "<mon-node>"
    return (
        f"cephadm bootstrap --mon-ip {first_mon} (bản {version}), rồi 'ceph orch host add' + "
        f"'ceph orch apply mgr|osd|rgw' (RGW chỉ nếu có node RGW) cho các node còn lại — xem đầy "
        f"đủ các pha khi bắt đầu cài đặt"
    )


def _deploy_cluster_ceph_deploy_preview_command(host: str | None, params: dict) -> str:
    version = params.get("version", "?")
    first_mon = _first_node_ip_with_role(params, "mon") or host or "<mon-node>"
    return (
        f"Cài gói ceph-mon/ceph-mgr/ceph-osd/ceph-radosgw bản {version} qua repo download.ceph.com, "
        f"khởi tạo MON thủ công (monmap/keyring/mkfs) bắt đầu từ {first_mon}, chờ quorum, rồi "
        f"MGR/OSD/RGW (RGW chỉ nếu có node RGW) — xem đầy đủ các pha khi bắt đầu cài đặt"
    )


def _deploy_cluster_rpm_local_preview_command(host: str | None, params: dict) -> str:
    version = params.get("version", "?")
    rpm_path = params.get("rpm_path", "?")
    first_mon = _first_node_ip_with_role(params, "mon") or host or "<mon-node>"
    return (
        f"Cài gói ceph-mon/ceph-mgr/ceph-osd/ceph-radosgw bản {version} từ thư mục RPM cục bộ "
        f"{rpm_path} (không tải Internet), khởi tạo MON thủ công bắt đầu từ {first_mon}, chờ "
        f"quorum, rồi MGR/OSD/RGW (RGW chỉ nếu có node RGW) — xem đầy đủ các pha khi bắt đầu cài đặt"
    )


def _delete_cluster_preview_command(host: str | None, params: dict) -> str:
    """Shared by both delete_cluster_cephadm and delete_cluster_manual —
    verified live, 2026-07-27: the systemctl-discovery + rm -rf +
    ceph-volume-zap teardown works the same way regardless of how the
    cluster was originally deployed (cephadm's own `rm-cluster` turned out
    to be unreliable as a cluster-wide teardown — see
    cluster_deploy.py::_PHASES_BY_ACTION_ID's own comment)."""
    wipe_note = (
        ", rồi ceph-volume lvm zap --destroy trên từng đĩa OSD"
        if params.get("wipe_osd_disks")
        else " (không xoá dữ liệu đĩa OSD)"
    )
    return (
        f"Dừng mọi daemon Ceph (systemctl stop/disable) trên từng node, xoá /etc/ceph + "
        f"/var/lib/ceph, gỡ cài đặt gói Ceph khỏi hệ điều hành (dnf/apt remove ceph*/librados*/"
        f"librbd*/libcephfs*){wipe_note} — xem đầy đủ khi bắt đầu xoá"
    )


def _convert_cluster_to_cephadm_preview_command(host: str | None, params: dict) -> str:
    version = params.get("version", "?")
    first_mon = _first_node_ip_with_role(params, "mon") or host or "<mon-node>"
    return (
        f"Cài cephadm (bản {version}) trên từng node, 'cephadm adopt --style legacy' lần lượt cho "
        f"từng MON rồi từng MGR (bắt đầu từ {first_mon}), bật cephadm orchestrator, 'ceph orch host "
        f"add' cho mọi node, rồi 'cephadm adopt' cho từng OSD — KHÔNG xoá dữ liệu OSD, chỉ đổi cách "
        f"daemon được quản lý — xem đầy đủ các pha khi bắt đầu chuyển đổi"
    )


def _restore_cluster_from_backup_preview_command(host: str | None, params: dict) -> str:
    version = params.get("version", "?")
    first_mon = _first_node_ip_with_role(params, "mon") or host or "<mon-node>"
    return (
        f"Dựng cụm trống bằng phương thức ceph-deploy (bản {version}, bắt đầu từ {first_mon}) — "
        f"giống hệt deploy_cluster_ceph_deploy — rồi khôi phục auth/CRUSH map/monmap từ bản backup "
        f"metadata gần nhất, sau đó khôi phục từng RBD image trong tracked_images (full export + "
        f"chain export-diff qua rbd import), và đối chiếu kích thước sau khôi phục — xem đầy đủ các "
        f"pha khi bắt đầu khôi phục"
    )


def _node_os_gate_prepare_preview_command(host: str | None, params: dict) -> str:
    target_host = params.get("host", host or "?")
    roles = ", ".join(params.get("roles") or []) or "?"
    return (
        f"Chuẩn bị node {target_host} (vai: {roles}) để cài lại OS — backup OSD id/fsid (nếu có vai "
        f"OSD), kích hoạt backup metadata cụm on-demand nếu bản gần nhất quá 24h, đặt cờ bảo trì "
        f"noout/noscrub/nodeep-scrub/nosnaptrim (nếu có vai OSD), rồi gỡ mon khỏi quorum từ một mon "
        f"khác và xác nhận số lượng quorum còn lại đúng (nếu có vai MON) — xem đầy đủ các pha khi "
        f"bắt đầu Chuẩn bị"
    )


def _node_os_gate_abort_preview_command(host: str | None, params: dict) -> str:
    target_host = params.get("host", host or "?")
    roles = ", ".join(params.get("roles") or []) or "?"
    return (
        f"Huỷ Chuẩn bị cho node {target_host} (vai: {roles}) — rejoin mon vào quorum theo đúng quy "
        f"trình đầy đủ (fetch mon keyring/monmap hiện tại, mkfs + start lại, chờ tự join qua Paxos), "
        f"gỡ cờ bảo trì NẾU đây là node duy nhất đang mid-flight, và giải phóng khoá tuần tự — xem "
        f"đầy đủ các pha khi bắt đầu Huỷ Chuẩn bị"
    )


def _node_os_gate_recover_preview_command(host: str | None, params: dict) -> str:
    target_host = params.get("host", host or "?")
    roles = ", ".join(params.get("roles") or []) or "?"
    version = params.get("target_version", "?")
    return (
        f"Xác nhận & Phục hồi node {target_host} (vai: {roles}) lên Ceph {version} — kiểm tra "
        f"đĩa/LVM, cấu hình node cơ bản (hosts/SELinux/firewalld/chrony), cài lại gói Ceph đúng "
        f"phiên bản, phục hồi ceph.conf + admin/bootstrap-osd keyring từ 1 mon còn sống, kích "
        f"hoạt lại OSD (nếu có vai OSD), rejoin mon vào quorum (nếu có vai MON), rồi gỡ cờ bảo "
        f"trì NẾU đây là node OSD cuối cùng đang mid-flight — xem đầy đủ các pha khi bắt đầu"
    )


_CLUSTER_DEPLOY_COMMAND_BUILDERS = {
    "deploy_cluster_cephadm": _deploy_cluster_cephadm_preview_command,
    "deploy_cluster_ceph_deploy": _deploy_cluster_ceph_deploy_preview_command,
    "deploy_cluster_rpm_local": _deploy_cluster_rpm_local_preview_command,
    "delete_cluster_cephadm": _delete_cluster_preview_command,
    "delete_cluster_manual": _delete_cluster_preview_command,
    "convert_cluster_to_cephadm": _convert_cluster_to_cephadm_preview_command,
    "restore_cluster_from_backup": _restore_cluster_from_backup_preview_command,
    "node_os_gate_prepare": _node_os_gate_prepare_preview_command,
    "node_os_gate_abort": _node_os_gate_abort_preview_command,
    "node_os_gate_recover": _node_os_gate_recover_preview_command,
}


def _volume_perf_sweep_preview_command(host: str | None, params: dict) -> str:
    pool = params.get("pool", "?")
    mon_ip = params.get("mon_ip") or host or "<mon-node>"
    return (
        f"Quét tải fio tăng dần (iodepth 1→256) trên scratch image riêng của pool {pool}, chạy từ "
        f"{mon_ip} — tìm điểm bão hoà hiệu năng, KHÔNG đụng volume thật — xem đầy đủ các bước khi "
        f"bắt đầu đo"
    )


def _vm_perf_benchmark_preview_command(host: str | None, params: dict) -> str:
    vm_ip = params.get("vm_ip") or host or "<vm-ip>"
    controller_ip = params.get("controller_ip") or "<openstack-controller>"
    device = params.get("device", "<device>")
    ssh_user = params.get("ssh_user", "<ssh-user>")
    return (
        f"SSH vào OpenStack Controller {controller_ip}, sau đó SSH {ssh_user}@{vm_ip} và chạy "
        f"fio READ-ONLY 4K random-read trên {device}, mỗi mức tải đo 3 lần và lấy median "
        "từ bên trong VM; không chạy write benchmark và không hiển thị nội dung SSH key"
    )


# 2026-07-29 fix (verified live): dashboard/routes/actions.py::approve_action
# checks has_command(action_id) BEFORE ever setting Action.status=APPROVED —
# without an entry here, has_command("volume_perf_sweep") returned False
# (same "own multi-step orchestrator, no per-host Command" shape as the
# cluster-deploy family above, but that family's own preview builders are
# what actually makes has_command() see them). approve_action's "no Command
# -> nothing to execute" branch then marked the Action EXECUTED immediately
# on approval, WITHOUT ever setting APPROVED — worker/llm/router_client.py's
# poll_approved_actions only ever queries status=APPROVED, so it never even
# saw this action. Symptom, exactly as reported: clicking "Duyệt" just
# silently redirected to "/" with no sweep ever starting.
_VOLUME_PERF_COMMAND_BUILDERS = {
    "volume_perf_sweep": _volume_perf_sweep_preview_command,
    "vm_perf_benchmark": _vm_perf_benchmark_preview_command,
}


def _restore_rbd_image_to_production_preview_command(host: str | None, params: dict) -> str:
    pool = params.get("pool", "?")
    image = params.get("image", "?")
    return (
        f"Khôi phục ĐÈ lên {pool}/{image} từ bản backup full gần nhất + toàn bộ chain "
        f"export-diff (rbd import/import-diff, theo đúng thứ tự tạo) — GHI ĐÈ dữ liệu hiện tại "
        f"của image này."
    )


def _restore_rbd_image_as_new_preview_command(host: str | None, params: dict) -> str:
    pool = params.get("pool", "?")
    image = params.get("image", "?")
    dest_pool = params.get("dest_pool", "?")
    dest_image = params.get("dest_image", "?")
    return (
        f"Khôi phục {pool}/{image} từ full backup + chain export-diff sang volume MỚI "
        f"{dest_pool}/{dest_image}, kiểm tra volume đích sau import; không thay đổi volume nguồn."
    )


# 2026-07-31 (Story 9.7): same reasoning as _VOLUME_PERF_COMMAND_BUILDERS above —
# worker/backup/engine.py is its own bespoke orchestrator (see that module's
# docstring), so approve_action's has_command() gate needs an entry here too,
# otherwise approving restore_rbd_image_to_production would hit the EXACT
# same silent-no-op bug the 2026-07-29 fix above describes for
# volume_perf_sweep (Action marked EXECUTED without ever reaching Worker
# execution). Only restore_rbd_image_to_production is listed — the other
# backup_action_ids members are either auto-executed (Safe, never go
# through this approve gate) or not yet implemented (backup_delete_manual).
_BACKUP_COMMAND_BUILDERS = {
    "restore_rbd_image_to_production": _restore_rbd_image_to_production_preview_command,
    "restore_rbd_image_as_new": _restore_rbd_image_as_new_preview_command,
}


def get_command(action_id: str, host: str | None = None, params: dict | None = None) -> str:
    """No command defined -> ExecutorError, never a silent no-op or a guess
    at what to run — an unrecognized action_id must never execute anything.

    2026-08-10 (multi-tenant remediation Phase 1) — deliberately still reads
    `settings.ceph_exec_mode` directly, not cluster-parameterized: the every-
    day SAFE-remediation loop (`_restart_osd_daemon_command`, every plain
    `COMMANDS[...]` entry) never branches on exec_mode at all — Ceph CLI
    commands assume `ceph-common` is installed on the target host (same
    assumption `_phase_cephadm_bootstrap`'s `ensure_ceph_common` step makes),
    regardless of how the daemons themselves are deployed. Only
    `bluestore_omap_quick_fix` (RISKY-only, proposed from a dedicated
    Dashboard OSD picker) and the Cluster Upgrade action_ids (their own
    Upgrade-page-only flow, explicitly deferred to a later multi-tenant
    phase) branch on it — neither has a cluster-scoped caller yet, so
    threading a `cluster` param through here now would be untestable dead
    code; revisit alongside whichever phase makes those two flows
    cluster-aware.

    `host` is used by restart_osd_daemon and both package-based Cluster
    Upgrade action_ids (systemd unit names must be discovered per-host —
    see `_restart_osd_daemon_command`/`_restart_discovered_units_snippet`) —
    every other action_id's command is the same regardless of host, so
    callers that don't have a specific host yet (e.g. building a preview
    string before any node is chosen) may omit it.

    `host` is used by restart_osd_daemon and both package-based Cluster
    Upgrade action_ids (systemd unit names must be discovered per-host —
    see `_restart_osd_daemon_command`/`_restart_discovered_units_snippet`) —
    every other action_id's command is the same regardless of host, so
    callers that don't have a specific host yet (e.g. building a preview
    string before any node is chosen) may omit it.

    `params` is only used by the management actions (see
    `_MANAGEMENT_COMMAND_BUILDERS` above) — missing/invalid required keys
    raise ExecutorError the same way a missing host does for
    restart_osd_daemon, never a guess.
    """
    if action_id == "restart_osd_daemon":
        return _restart_osd_daemon_command(host, params)
    if action_id in _MANAGEMENT_COMMAND_BUILDERS:
        return _MANAGEMENT_COMMAND_BUILDERS[action_id](params or {})
    if action_id in _INCIDENT_PARAMETER_COMMAND_BUILDERS:
        return _INCIDENT_PARAMETER_COMMAND_BUILDERS[action_id](params or {})
    if action_id in _DEVICE_HEALTH_COMMAND_BUILDERS:
        return _DEVICE_HEALTH_COMMAND_BUILDERS[action_id](params or {})
    if action_id in _CLUSTER_UPGRADE_COMMAND_BUILDERS:
        return _CLUSTER_UPGRADE_COMMAND_BUILDERS[action_id](host, params or {})
    if action_id in _PATCH_COMMAND_BUILDERS:
        return _PATCH_COMMAND_BUILDERS[action_id](host, params or {})
    if action_id in _CLUSTER_DEPLOY_COMMAND_BUILDERS:
        return _CLUSTER_DEPLOY_COMMAND_BUILDERS[action_id](host, params or {})
    if action_id in _VOLUME_PERF_COMMAND_BUILDERS:
        return _VOLUME_PERF_COMMAND_BUILDERS[action_id](host, params or {})
    if action_id in _BACKUP_COMMAND_BUILDERS:
        return _BACKUP_COMMAND_BUILDERS[action_id](host, params or {})
    if action_id in _BLUESTORE_COMMAND_BUILDERS:
        return _BLUESTORE_COMMAND_BUILDERS[action_id](host, params or {})
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
        or action_id in _INCIDENT_PARAMETER_COMMAND_BUILDERS
        or action_id in _DEVICE_HEALTH_COMMAND_BUILDERS
        or action_id in _CLUSTER_UPGRADE_COMMAND_BUILDERS
        or action_id in _PATCH_COMMAND_BUILDERS
        or action_id in _CLUSTER_DEPLOY_COMMAND_BUILDERS
        or action_id in _VOLUME_PERF_COMMAND_BUILDERS
        or action_id in _BACKUP_COMMAND_BUILDERS
        or action_id in _BLUESTORE_COMMAND_BUILDERS
        or action_id in COMMANDS
    )
