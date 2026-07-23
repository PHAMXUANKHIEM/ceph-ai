import json
import logging
import re
import shlex

from config.settings import settings
from watcher import ceph_client
from watcher.ceph_client import run_command_on_node

logger = logging.getLogger(__name__)

LOG_TAIL_LINES = 50
# Only word characters and hyphens after "mon." — a bare \S+ would also
# swallow trailing punctuation (e.g. "mon.khiempx-mon2," or "mon.name)"),
# silently failing the hostname_to_ip lookup afterwards.
_MON_NAME_PATTERN = re.compile(r"mon\.([\w-]+)")


def _mon_hostname_to_ip() -> dict[str, str]:
    names = [n.strip() for n in settings.ceph_mon_hostnames.split(",") if n.strip()]
    ips = [ip.strip() for ip in settings.ceph_mon_nodes.split(",") if ip.strip()]
    if len(names) != len(ips):
        logger.warning(
            "ceph_mon_hostnames (%d entries) and ceph_mon_nodes (%d entries) "
            "have mismatched lengths — name-to-IP mapping may be wrong or incomplete",
            len(names),
            len(ips),
        )
    return dict(zip(names, ips))


def _get_osd_nodes() -> list[str]:
    return [h.strip() for h in settings.ceph_osd_nodes.split(",") if h.strip()]


def _get_mon_nodes() -> list[str]:
    return [h.strip() for h in settings.ceph_mon_nodes.split(",") if h.strip()]


def _get_mgr_nodes() -> list[str]:
    return [h.strip() for h in settings.ceph_mgr_nodes.split(",") if h.strip()]


def identify_relevant_nodes(ceph_code: str, check_detail: dict) -> list[str]:
    """Return the SSH-able IP(s) of the node(s) relevant to this check.

    `ceph_code` prefix decides daemon type FIRST and deterministically
    (OSD_/PG_ -> OSD nodes, MGR_ -> MGR nodes, never MON) — this is what AC #1
    depends on. Only for MON-related/ambiguous codes do we try to parse a
    specific mon name out of the check's detail text.
    """
    if ceph_code.startswith("OSD_") or ceph_code.startswith("PG_"):
        # No cheap osd-id -> host mapping available in v1 (would need an
        # extra `ceph osd tree`/`ceph osd find` query) — collect from all
        # OSD nodes instead of the whole cluster. See Dev Notes.
        return _get_osd_nodes()

    if ceph_code.startswith("MGR_"):
        mgr_nodes = _get_mgr_nodes()
        if mgr_nodes:
            return mgr_nodes
        # No MGR nodes configured — fall through to the generic MON-fallback
        # below rather than returning nothing; a degraded-but-present log
        # beats none at all.

    detail_messages = " ".join(d.get("message", "") for d in check_detail.get("detail", []))
    mon_names_mentioned = _MON_NAME_PATTERN.findall(detail_messages)
    if mon_names_mentioned:
        hostname_to_ip = _mon_hostname_to_ip()
        ips = [hostname_to_ip[name] for name in mon_names_mentioned if name in hostname_to_ip]
        if ips:
            return ips

    # MON-related or unrecognized code with no parseable node name: fall back
    # to whichever MON node last actually answered a health query, rather
    # than blindly assuming the first configured node is reachable.
    if ceph_client.last_successful_mon_node:
        return [ceph_client.last_successful_mon_node]
    mon_nodes = _get_mon_nodes()
    return mon_nodes[:1]


def _log_command_for_host(host: str) -> tuple[str, str]:
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
    """
    is_osd = host in set(_get_osd_nodes())
    exec_mode = settings.ceph_exec_mode
    if exec_mode == "none":
        unit_glob = "ceph-osd@*" if is_osd else "ceph-mon@*"
        command = f"journalctl -u {shlex.quote(unit_glob)} -n {LOG_TAIL_LINES} --no-pager 2>&1"
        return command, unit_glob

    container = settings.ceph_osd_container_name if is_osd else settings.ceph_container_name
    # ceph-mon/ceph-osd run with `-d` (foreground) and log to stderr, so
    # `docker logs`/`podman logs` surfaces the actual daemon log on ITS
    # stderr — must redirect stderr to stdout or the excerpt comes back
    # empty even though the command "succeeds" (exit 0, no error raised).
    command = f"{exec_mode} logs {shlex.quote(container)} --tail {LOG_TAIL_LINES} 2>&1"
    return command, container


def _cephadm_daemon_prefix_for_code(ceph_code: str) -> str:
    if ceph_code.startswith("OSD_") or ceph_code.startswith("PG_"):
        return "osd."
    if ceph_code.startswith("MGR_"):
        return "mgr."
    return "mon."


def _cephadm_relevant_daemon_names(host: str, daemon_prefix: str) -> list[str]:
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
        output = run_command_on_node(host, "cephadm ls --no-detail")
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


def _collect_cephadm_log_excerpt(host: str, ceph_code: str) -> str:
    daemon_prefix = _cephadm_daemon_prefix_for_code(ceph_code)
    daemon_names = _cephadm_relevant_daemon_names(host, daemon_prefix)
    if not daemon_names:
        return f"--- {host} (cephadm: no {daemon_prefix}* daemon found) ---"

    parts = []
    for name in daemon_names:
        # `cephadm logs` itself doesn't take a --tail/-n line-count flag —
        # pipe through `tail` the same way the non-cephadm docker/podman
        # path pipes `docker logs` output, for a consistently-sized excerpt.
        command = f"cephadm logs --name {shlex.quote(name)} 2>&1 | tail -n {LOG_TAIL_LINES}"
        try:
            output = run_command_on_node(host, command)
            parts.append(f"--- {host} ({name}) ---\n{output}")
        except Exception as exc:
            logger.warning("_collect_cephadm_log_excerpt: %s (%s) unavailable: %s", host, name, exc)
            parts.append(f"--- {host} ({name}) --- (unavailable: {exc})")
    return "\n".join(parts)


def collect_relevant_logs(ceph_code: str, check_detail: dict) -> tuple[list[str], str]:
    """Collect the daemon log from the node(s) relevant to this check, using
    whatever command shape matches settings.ceph_exec_mode.

    Returns (nodes, log_excerpt) — `nodes` is the list of node IPs actually
    targeted (used for the Incident envelope's `nodes[]` field).
    """
    nodes = identify_relevant_nodes(ceph_code, check_detail)
    excerpt_parts = []
    for host in nodes:
        if settings.ceph_exec_mode == "cephadm":
            excerpt_parts.append(_collect_cephadm_log_excerpt(host, ceph_code))
            continue
        command, descriptor = _log_command_for_host(host)
        try:
            output = run_command_on_node(host, command)
            excerpt_parts.append(f"--- {host} ({descriptor}) ---\n{output}")
        except Exception as exc:
            logger.warning("collect_relevant_logs: %s unavailable: %s", host, exc)
            excerpt_parts.append(f"--- {host} ({descriptor}) --- (unavailable: {exc})")
    return nodes, "\n".join(excerpt_parts)
