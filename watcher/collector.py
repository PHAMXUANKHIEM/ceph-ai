from __future__ import annotations

import json
import logging
import re
import shlex
from typing import TYPE_CHECKING

from config.settings import settings
from watcher import ceph_client
from watcher.ceph_client import run_command_on_node, run_command_on_node_with
from watcher.osd_hosts import osd_ids_in_detail, resolve_osd_hosts

if TYPE_CHECKING:
    from shared.models import Cluster

logger = logging.getLogger(__name__)

LOG_TAIL_LINES = 50
# Only word characters and hyphens after "mon." — a bare \S+ would also
# swallow trailing punctuation (e.g. "mon.khiempx-mon2," or "mon.name)"),
# silently failing the hostname_to_ip lookup afterwards.
_MON_NAME_PATTERN = re.compile(r"mon\.([\w-]+)")


def _mon_hostname_to_ip(cluster: "Cluster | None" = None) -> dict[str, str]:
    hostnames_raw = cluster.ceph_mon_hostnames if cluster is not None else settings.ceph_mon_hostnames
    nodes_raw = cluster.ceph_mon_nodes if cluster is not None else settings.ceph_mon_nodes
    names = [n.strip() for n in hostnames_raw.split(",") if n.strip()]
    ips = [ip.strip() for ip in nodes_raw.split(",") if ip.strip()]
    if len(names) != len(ips):
        logger.warning(
            "ceph_mon_hostnames (%d entries) and ceph_mon_nodes (%d entries) "
            "have mismatched lengths — name-to-IP mapping may be wrong or incomplete",
            len(names),
            len(ips),
        )
    return dict(zip(names, ips))


def _get_osd_nodes(cluster: "Cluster | None" = None) -> list[str]:
    raw = cluster.ceph_osd_nodes if cluster is not None else settings.ceph_osd_nodes
    return [h.strip() for h in raw.split(",") if h.strip()]


def _get_mon_nodes(cluster: "Cluster | None" = None) -> list[str]:
    raw = cluster.ceph_mon_nodes if cluster is not None else settings.ceph_mon_nodes
    return [h.strip() for h in raw.split(",") if h.strip()]


def _get_mgr_nodes(cluster: "Cluster | None" = None) -> list[str]:
    raw = cluster.ceph_mgr_nodes if cluster is not None else settings.ceph_mgr_nodes
    return [h.strip() for h in raw.split(",") if h.strip()]


def identify_relevant_nodes(
    ceph_code: str,
    check_detail: dict,
    cluster: "Cluster | None" = None,
    osd_host_map: dict[int, str] | None = None,
) -> list[str]:
    """Return the SSH-able IP(s) of the node(s) relevant to this check.

    `ceph_code` prefix decides daemon type FIRST and deterministically
    (OSD_/PG_ -> OSD nodes, MGR_ -> MGR nodes, never MON) — this is what AC #1
    depends on. Only for MON-related/ambiguous codes do we try to parse a
    specific mon name out of the check's detail text.

    `cluster` (2026-08-10, multi-tenant remediation Phase 1): when given,
    resolves every node list from THAT cluster's own fields instead of the
    global `settings` singleton — same opt-in posture as
    `shared/cluster_nodes.py::configured_nodes()`.

    `osd_host_map` (2026-08-20, out-param): khi truyền vào một dict, hàm
    điền {osd_id: host} cho đúng những OSD mà check này nêu đích danh —
    `build_and_publish_incident` chuyển tiếp nó vào envelope để LLM không
    còn phải đoán osd nào nằm ở máy nào (xem `watcher/osd_hosts.py`).
    """
    if ceph_code.startswith("OSD_") or ceph_code.startswith("PG_"):
        # 2026-08-20 — SỬA LỖI CÓ THẬT: trước đây hàm này trả về TOÀN BỘ
        # danh sách node OSD kèm comment "No cheap osd-id -> host mapping
        # available in v1". Cái gap đó không còn: watcher/osd_hosts.py tra
        # đúng host bằng cách hỏi systemd của từng node OSD đã cấu hình.
        # Hậu quả của bản cũ không chỉ là thu log thừa — danh sách phẳng ấy
        # vào thẳng prompt LLM ("Affected nodes: ip1, ip2, ip3") nên model
        # buộc phải đoán và sinh ra chẩn đoán gán osd vào SAI node, rồi
        # cùng danh sách ấy được ghi vào Action.target_nodes nên lệnh khắc
        # phục cũng nhắm sai máy.
        osd_ids = osd_ids_in_detail(check_detail)
        if osd_ids:
            resolved = resolve_osd_hosts(osd_ids, cluster)
            if osd_host_map is not None:
                osd_host_map.update(resolved)
            if resolved:
                # Giữ thứ tự cấu hình thay vì thứ tự osd_id, để excerpt đọc
                # ổn định giữa các lần chạy.
                targeted = [h for h in _get_osd_nodes(cluster) if h in set(resolved.values())]
                if targeted:
                    return targeted
        # Không nêu osd nào, hoặc không osd nào khớp host đã cấu hình: quay
        # về gom cả cụm OSD. Đây là "chưa xác định được", KHÔNG phải "đã xác
        # định là tất cả" — `osd_host_map` để trống chính là tín hiệu ấy cho
        # phía dựng prompt.
        return _get_osd_nodes(cluster)

    if ceph_code.startswith("MGR_"):
        mgr_nodes = _get_mgr_nodes(cluster)
        if mgr_nodes:
            return mgr_nodes
        # No MGR nodes configured — fall through to the generic MON-fallback
        # below rather than returning nothing; a degraded-but-present log
        # beats none at all.

    detail_messages = " ".join(d.get("message", "") for d in check_detail.get("detail", []))
    mon_names_mentioned = _MON_NAME_PATTERN.findall(detail_messages)
    if mon_names_mentioned:
        hostname_to_ip = _mon_hostname_to_ip(cluster)
        ips = [hostname_to_ip[name] for name in mon_names_mentioned if name in hostname_to_ip]
        if ips:
            return ips

    # MON-related or unrecognized code with no parseable node name: fall back
    # to whichever MON node last actually answered a health query, rather
    # than blindly assuming the first configured node is reachable.
    # `ceph_client.last_successful_mon_node` is a DEFAULT-cluster-only sticky
    # value (see query_cluster_health_with's own docstring — observed
    # clusters poll with update_sticky_fallback=False) — never consulted for
    # a non-default cluster, which falls straight through to its own
    # configured MON list instead.
    if cluster is None and ceph_client.last_successful_mon_node:
        return [ceph_client.last_successful_mon_node]
    mon_nodes = _get_mon_nodes(cluster)
    return mon_nodes[:1]


def _log_command_for_host(host: str, cluster: "Cluster | None" = None) -> tuple[str, str]:
    """Returns (command, descriptor) for fetching this host's daemon log via
    docker/podman/none mode — `descriptor` is shown in the excerpt header (a
    container name for docker/podman, a systemd unit glob for bare-metal) so
    the header stays meaningful regardless of ceph_exec_mode.

    Not used for "cephadm" mode — see _collect_cephadm_log_excerpt, which
    discovers exact daemon names via `cephadm ls` instead (a fixed container/
    unit guess doesn't work there: cephadm names are per-host/auto-generated,
    and a cephadm host commonly runs mon+mgr+osd all on the same box).

    "none" mode (ceph-deploy / package install, no container) has no
    osd-id -> host mapping available here (see identify_relevant_nodes'
    OSD_/PG_ comment — that's the same gap), so rather than guess a specific
    `ceph-osd@<id>` unit name, this targets ALL osd units on the host with a
    systemd unit glob (`journalctl` supports `-u` globbing natively) — one
    query gets every OSD daemon's recent log on that node instead of none.

    `cluster` (2026-08-10, multi-tenant remediation Phase 1): resolves
    exec-mode/container names from that cluster's own fields when given.
    """
    is_osd = host in set(_get_osd_nodes(cluster))
    exec_mode = cluster.ceph_exec_mode if cluster is not None else settings.ceph_exec_mode
    if exec_mode == "none":
        unit_glob = "ceph-osd@*" if is_osd else "ceph-mon@*"
        command = f"journalctl -u {shlex.quote(unit_glob)} -n {LOG_TAIL_LINES} --no-pager 2>&1"
        return command, unit_glob

    if cluster is not None:
        container = cluster.ceph_osd_container_name if is_osd else cluster.ceph_container_name
    else:
        container = settings.ceph_osd_container_name if is_osd else settings.ceph_container_name
    # ceph-mon/ceph-osd run with `-d` (foreground) and log to stderr, so
    # `docker logs`/`podman logs` surfaces the actual daemon log on ITS
    # stderr — must redirect stderr to stdout or the excerpt comes back
    # empty even though the command "succeeds" (exit 0, no error raised).
    command = f"{exec_mode} logs {shlex.quote(container)} --tail {LOG_TAIL_LINES} 2>&1"
    return command, container


def _cephadm_daemon_prefix_for_code(ceph_code: str) -> str:
    # BlueStore health checks are emitted by OSDs even though their Ceph
    # health-code prefix is BLUESTORE_, not OSD_.  Treating them as the
    # generic MON fallback collected an unrelated monitor log and left the
    # diagnosis without the affected OSD evidence.
    if (
        ceph_code.startswith("OSD_")
        or ceph_code.startswith("PG_")
        or ceph_code.startswith("BLUESTORE_")
    ):
        return "osd."
    if ceph_code.startswith("MGR_"):
        return "mgr."
    return "mon."


def _run_on_host(host: str, command: str, cluster: "Cluster | None" = None) -> str:
    """SSH-runs `command` on `host` using `cluster`'s own creds when given,
    else `settings.ssh_user`/`settings.ssh_key_path` (2026-08-10,
    multi-tenant remediation Phase 1) — every collector.py call site that
    used to call `run_command_on_node` directly goes through this instead,
    so a non-default cluster's log collection never silently uses the
    DEFAULT cluster's SSH key."""
    if cluster is not None:
        return run_command_on_node_with(host, command, cluster.ssh_user, cluster.ssh_key_path)
    return run_command_on_node(host, command)


def _cephadm_relevant_daemon_names(host: str, daemon_prefix: str, cluster: "Cluster | None" = None) -> list[str]:
    """Discovers this host's EXACT daemon names matching `daemon_prefix`
    (e.g. "mon.", "osd.", "mgr.") via `cephadm ls --no-detail`.

    Unlike docker/podman (one fixed, configured container name), cephadm
    daemon names aren't predictable from the host alone: OSD names (`osd.0`)
    have no relation to hostname at all, and MGR names carry a random suffix
    (`mgr.khiempx-ceph1.loylll`, verified against a real cluster) — so this
    discovers the real names instead of guessing. Also handles a cephadm
    host commonly running mon+mgr+osd all on the same box (colocated),
    where a single assumed daemon type per host would miss the others.
    """
    try:
        output = _run_on_host(host, "cephadm ls --no-detail", cluster)
        daemons = json.loads(output)
    except Exception as exc:
        logger.warning("_cephadm_relevant_daemon_names: %s: failed to list daemons: %s", host, exc)
        return []
    if not isinstance(daemons, list):
        return []
    return [
        d["name"]
        for d in daemons
        if isinstance(d, dict) and isinstance(d.get("name"), str) and d["name"].startswith(daemon_prefix)
    ]


def _collect_cephadm_log_excerpt(host: str, ceph_code: str, cluster: "Cluster | None" = None) -> str:
    daemon_prefix = _cephadm_daemon_prefix_for_code(ceph_code)
    daemon_names = _cephadm_relevant_daemon_names(host, daemon_prefix, cluster)
    if not daemon_names:
        return f"--- {host} (cephadm: no {daemon_prefix}* daemon found) ---"

    parts = []
    for name in daemon_names:
        # `cephadm logs` itself doesn't take a --tail/-n line-count flag —
        # pipe through `tail` the same way the non-cephadm docker/podman
        # path pipes `docker logs` output, for a consistently-sized excerpt.
        command = f"cephadm logs --name {shlex.quote(name)} 2>&1 | tail -n {LOG_TAIL_LINES}"
        try:
            output = _run_on_host(host, command, cluster)
            parts.append(f"--- {host} ({name}) ---\n{output}")
        except Exception as exc:
            logger.warning("_collect_cephadm_log_excerpt: %s (%s) unavailable: %s", host, name, exc)
            parts.append(f"--- {host} ({name}) --- (unavailable: {exc})")
    return "\n".join(parts)


# Story A (Crash-module visibility, 2026-08-01): RECENT_CRASH's own check
# detail is just a count ("N daemons have recently crashed") — the actual
# diagnostic substance lives in `ceph crash ls-new`, not any daemon's own
# log. Unlike OSD_/PG_/MGR_ codes, a crash isn't tied to one specific,
# still-relevant host (the crashed daemon has typically already restarted
# by the time this is reported), so this bypasses identify_relevant_nodes
# entirely and reuses `ceph_client.run_ceph_json_command` — the same
# read-only, multi-MON-fallback primitive dashboard/ceph_tools.py's
# Chat-with-AI tools already use — instead of the SSH-to-a-guessed-host
# path every other ceph_code takes below.
RECENT_CRASH_CEPH_CODE = "RECENT_CRASH"
_CRASH_EXCERPT_MAX_ENTRIES = 10
_CRASH_BACKTRACE_MAX_CHARS = 2000


def _collect_recent_crash_excerpt(cluster: "Cluster | None" = None) -> tuple[list[str], str]:
    """Returns (nodes, log_excerpt) same shape as collect_relevant_logs —
    `nodes` here is the single MON node `run_ceph_json_command` actually
    reached (becomes the Incident envelope's `nodes[]`, and in turn the
    Action's `target_nodes` — see worker/llm/router_client.py — so the
    eventual `ceph crash archive-all` SAFE-action command runs on a MON
    node already verified reachable, not a guess)."""
    try:
        if cluster is not None:
            host, parsed = ceph_client.run_ceph_json_command_with(
                _get_mon_nodes(cluster), cluster.ceph_container_name, cluster.ssh_user,
                cluster.ssh_key_path, cluster.ceph_exec_mode, "ceph crash ls-new",
            )
        else:
            host, parsed = ceph_client.run_ceph_json_command("ceph crash ls-new")
    except ceph_client.CephQueryError as exc:
        logger.warning("_collect_recent_crash_excerpt: crash ls-new failed: %s", exc)
        return [], f"(unavailable: {exc})"

    # A dict here means `run_ceph_json_command`'s own JSON-decode fallback
    # kicked in (some future Ceph version's `--format json` for this
    # subcommand stopped round-tripping) — not a crash list, nothing to
    # format per-entry.
    crashes = parsed if isinstance(parsed, list) else []
    if not crashes:
        return [host], "ceph crash ls-new returned no entries (crash may already be archived)"

    parts = []
    for crash in crashes[:_CRASH_EXCERPT_MAX_ENTRIES]:
        crash_id = crash.get("crash_id", "?")
        entity = crash.get("entity_name") or crash.get("process_name") or "?"
        hostname = crash.get("utsname_hostname", "?")
        timestamp = crash.get("timestamp") or crash.get("crash_timestamp") or "?"
        backtrace = crash.get("backtrace")
        backtrace_text = (
            "\n".join(backtrace) if isinstance(backtrace, list) else str(backtrace or crash.get("assert_msg", ""))
        )
        parts.append(
            f"--- crash {crash_id} ({entity} on {hostname}, {timestamp}) ---\n"
            f"{backtrace_text[:_CRASH_BACKTRACE_MAX_CHARS]}"
        )
    return [host], "\n".join(parts)


# Story B (DeviceHealth visibility, 2026-08-01): covers DEVICE_HEALTH,
# DEVICE_HEALTH_IN_USE, DEVICE_HEALTH_TOOMANY (all share this prefix — same
# mgr/devicehealth module, same underlying prediction data, just different
# severity/scope). Deliberately does NOT propose or wire up any
# auto-evacuate-this-OSD action — same posture as pg_repair_force
# (commands.py's own comment): nothing in this codebase extracts a specific
# osd_id from a detected Incident in a way the AI diagnosis tool schema
# could safely carry (no params field — see router_client.py::_tool_schema),
# so guessing one would be worse than not automating it. This story is
# CONTEXT ONLY — better diagnosis_text so the operator knows exactly which
# device/OSD to evacuate manually via Chat-with-AI's existing mark_osd_out.
DEVICE_HEALTH_CEPH_CODE_PREFIX = "DEVICE_HEALTH"
_DEVICE_HEALTH_EXCERPT_MAX_ENTRIES = 20
# life_expectancy_max of "" or the zero-timestamp sentinel both mean "no
# prediction yet" — same two "not actually set" checks Ceph's own
# devicehealth module applies before using this field (verified against
# src/pybind/mgr/devicehealth/module.py::check_health).
_NO_LIFE_EXPECTANCY_VALUES = (None, "", "0.000000")


def _format_device_location(device: dict) -> str:
    locations = device.get("location") or []
    parts = [
        f"{loc.get('host', '?')}:{loc.get('dev', '?')}" for loc in locations if isinstance(loc, dict)
    ]
    return ",".join(parts) if parts else "?"


def _collect_device_health_excerpt(cluster: "Cluster | None" = None) -> tuple[list[str], str]:
    """DEVICE_HEALTH*'s own check detail already names the failing device(s)
    in free text, but re-parsing that (like _MON_NAME_PATTERN does for
    MON-related codes) is fragile for a devid string's format. Queries
    `ceph device ls` directly instead and reports every device with a
    life-expectancy prediction already set, using the SAME field names
    Ceph's own devicehealth mgr module reads internally (devid/daemons/
    location/life_expectancy_min/life_expectancy_max — verified against
    src/pybind/mgr/devicehealth/module.py), not guessed ones."""
    try:
        if cluster is not None:
            host, parsed = ceph_client.run_ceph_json_command_with(
                _get_mon_nodes(cluster), cluster.ceph_container_name, cluster.ssh_user,
                cluster.ssh_key_path, cluster.ceph_exec_mode, "ceph device ls",
            )
        else:
            host, parsed = ceph_client.run_ceph_json_command("ceph device ls")
    except ceph_client.CephQueryError as exc:
        logger.warning("_collect_device_health_excerpt: ceph device ls failed: %s", exc)
        return [], f"(unavailable: {exc})"

    devices = parsed if isinstance(parsed, list) else []
    predicted = [
        d
        for d in devices
        if isinstance(d, dict) and d.get("life_expectancy_max") not in _NO_LIFE_EXPECTANCY_VALUES
    ]
    if not predicted:
        return [host], "ceph device ls returned no devices with a life-expectancy prediction set"

    parts = []
    for device in predicted[:_DEVICE_HEALTH_EXCERPT_MAX_ENTRIES]:
        devid = device.get("devid", "?")
        daemons = ",".join(device.get("daemons") or ["none"])
        location = _format_device_location(device)
        life_min = device.get("life_expectancy_min") or "unknown"
        life_max = device.get("life_expectancy_max") or "unknown"
        parts.append(
            f"--- device {devid} ({location}); daemons {daemons} ---\n"
            f"life expectancy between {life_min} and {life_max}"
        )
    return [host], "\n".join(parts)


def collect_relevant_logs(
    ceph_code: str,
    check_detail: dict,
    cluster: "Cluster | None" = None,
    osd_host_map: dict[int, str] | None = None,
) -> tuple[list[str], str]:
    """Collect the daemon log from the node(s) relevant to this check, using
    whatever command shape matches this cluster's exec mode.

    Returns (nodes, log_excerpt) — `nodes` is the list of node IPs actually
    targeted (used for the Incident envelope's `nodes[]` field).

    `cluster` (2026-08-10, multi-tenant remediation Phase 1): when given,
    every node list/SSH creds/exec-mode/container name below comes from
    THAT cluster's own fields instead of the global `settings` singleton —
    same opt-in-via-explicit-cluster posture as every other function in this
    module. `watcher/main.py::_build_and_publish_incident_for_observed_
    cluster` passes its own `Cluster` row here; the DEFAULT cluster's own
    `build_and_publish_incident` still omits it, unchanged behavior.

    `osd_host_map` (2026-08-20, out-param): chuyển thẳng xuống
    `identify_relevant_nodes` — xem docstring hàm ấy.
    """
    if ceph_code == RECENT_CRASH_CEPH_CODE:
        return _collect_recent_crash_excerpt(cluster)
    if ceph_code.startswith(DEVICE_HEALTH_CEPH_CODE_PREFIX):
        return _collect_device_health_excerpt(cluster)

    nodes = identify_relevant_nodes(ceph_code, check_detail, cluster, osd_host_map)
    exec_mode = cluster.ceph_exec_mode if cluster is not None else settings.ceph_exec_mode
    excerpt_parts = []
    for host in nodes:
        if exec_mode == "cephadm":
            excerpt_parts.append(_collect_cephadm_log_excerpt(host, ceph_code, cluster))
            continue
        command, descriptor = _log_command_for_host(host, cluster)
        try:
            output = _run_on_host(host, command, cluster)
            excerpt_parts.append(f"--- {host} ({descriptor}) ---\n{output}")
        except Exception as exc:
            logger.warning("collect_relevant_logs: %s unavailable: %s", host, exc)
            excerpt_parts.append(f"--- {host} ({descriptor}) --- (unavailable: {exc})")
    return nodes, "\n".join(excerpt_parts)
