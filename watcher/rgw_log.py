import json
import logging
import shlex

from config.settings import settings
from watcher.ceph_client import run_command_on_node, run_command_on_node_with

logger = logging.getLogger(__name__)

# Plain-tail window when no filter is given — matches watcher/collector.py's
# LOG_TAIL_LINES posture (a display excerpt, not a full log dump).
RGW_LOG_TAIL_LINES = 100
# When a filter term IS given, the raw window scanned before grepping — a
# term that only appears further back than the plain tail's window would
# otherwise never be found. Still bounded (not the whole file) so one
# request's SSH round trip and output size stay predictable.
RGW_LOG_FILTER_SCAN_LINES = 3000
# Cap on matched lines actually returned — a broad filter term (or none)
# could otherwise match/return an unbounded amount from the scan window.
RGW_LOG_MAX_DISPLAY_LINES = 300
# Operator-supplied filter text is capped before ever reaching a remote
# command — not a security boundary by itself (shlex.quote() below is what
# actually prevents shell injection), just a sane bound on request size.
RGW_LOG_FILTER_MAX_CHARS = 200
RGW_LOG_COMMAND_TIMEOUT_SECONDS = 15


class RgwLogError(Exception):
    """Raised when the RGW log can't be fetched from a host at all (SSH
    failure, no RGW daemon found in cephadm mode, missing container name
    config, ...). An empty result (no lines matched a filter) is NOT this —
    that's a normal, valid outcome the caller returns as-is."""


def _quoted_grep(filter_text: str) -> str:
    # -i (case-insensitive) matches how an operator scanning a log for a
    # request id / bucket name / error string usually doesn't know or care
    # about its exact casing. `--` stops grep from treating a filter that
    # happens to start with "-" as a flag.
    return f"grep -i -- {shlex.quote(filter_text)}"


def _docker_podman_command(filter_text: str | None) -> str:
    exec_mode = settings.ceph_exec_mode
    container = settings.ceph_rgw_container_name
    # radosgw runs in the foreground and logs to stderr, same as ceph-mon/
    # ceph-osd (see watcher/collector.py's _log_command_for_host) — `docker
    # logs`/`podman logs` only surfaces it if stderr is redirected to stdout.
    if filter_text:
        return (
            f"{exec_mode} logs {shlex.quote(container)} --tail {RGW_LOG_FILTER_SCAN_LINES} 2>&1"
            f" | {_quoted_grep(filter_text)} | tail -n {RGW_LOG_MAX_DISPLAY_LINES}"
        )
    return f"{exec_mode} logs {shlex.quote(container)} --tail {RGW_LOG_TAIL_LINES} 2>&1"


def _none_mode_command(filter_text: str | None) -> str:
    # Traditional ceph-deploy/package-install RGW unit naming
    # (ceph-radosgw@<instance-id>.service) — globbed the same way
    # watcher/collector.py globs ceph-osd@*/ceph-mon@* for "none" mode,
    # since there's no reliable instance-id -> host mapping available here
    # either (a host commonly runs exactly one RGW instance, but nothing
    # here assumes that).
    unit_glob = "ceph-radosgw@*"
    if filter_text:
        return (
            f"journalctl -u {shlex.quote(unit_glob)} -n {RGW_LOG_FILTER_SCAN_LINES} --no-pager 2>&1"
            f" | {_quoted_grep(filter_text)} | tail -n {RGW_LOG_MAX_DISPLAY_LINES}"
        )
    return f"journalctl -u {shlex.quote(unit_glob)} -n {RGW_LOG_TAIL_LINES} --no-pager 2>&1"


def _cephadm_rgw_daemon_names(host: str) -> list[str]:
    """Discovers this host's exact RGW daemon name(s) via `cephadm ls
    --no-detail`, same approach as watcher/collector.py's
    _cephadm_relevant_daemon_names (mon/osd/mgr) — cephadm daemon names
    carry a per-deployment random suffix, not predictable from the host
    alone.

    NOTE: unlike the mon./osd./mgr. prefixes in collector.py (verified
    against a real cephadm/reef cluster), the "rgw." prefix used here is
    inferred from cephadm's naming convention for those other daemon types,
    not verified live against a real RGW deployment. If a real cluster's
    `cephadm ls` names RGW daemons differently, this simply returns no
    matches and the caller surfaces that as an empty-result RgwLogError
    rather than guessing further.
    """
    try:
        output = run_command_on_node(host, "cephadm ls --no-detail")
        daemons = json.loads(output)
    except Exception as exc:
        raise RgwLogError(f"Không liệt kê được daemon trên {host}: {exc}") from exc
    if not isinstance(daemons, list):
        return []
    return [
        d["name"]
        for d in daemons
        if isinstance(d, dict) and isinstance(d.get("name"), str) and d["name"].startswith("rgw.")
    ]


def _cephadm_command(name: str, filter_text: str | None) -> str:
    # `cephadm logs` itself doesn't take a --tail/-n line-count flag — pipe
    # through `tail` the same way collector.py's _collect_cephadm_log_excerpt
    # does for mon/osd/mgr.
    if filter_text:
        return (
            f"cephadm logs --name {shlex.quote(name)} 2>&1 | tail -n {RGW_LOG_FILTER_SCAN_LINES}"
            f" | {_quoted_grep(filter_text)} | tail -n {RGW_LOG_MAX_DISPLAY_LINES}"
        )
    return f"cephadm logs --name {shlex.quote(name)} 2>&1 | tail -n {RGW_LOG_TAIL_LINES}"


def fetch_rgw_log(host: str, filter_text: str | None = None) -> str:
    """Tails `host`'s RGW (radosgw) daemon log over SSH, wrapped for however
    the cluster is deployed (settings.ceph_exec_mode) — same docker/podman/
    cephadm/none split watcher/collector.py uses for mon/osd/mgr daemon
    logs, just for RGW and triggered on-demand from the Dashboard's Nodes
    page (dashboard/routes/nodes.py) rather than during incident diagnosis.

    `filter_text`, when given (stripped, capped to RGW_LOG_FILTER_MAX_CHARS,
    blank treated as "no filter"), greps the log server-side over a larger
    scan window (RGW_LOG_FILTER_SCAN_LINES) before capping the result to
    RGW_LOG_MAX_DISPLAY_LINES lines — a plain tail's window is usually too
    short to contain an older match. Always shlex.quote()'d into the remote
    command exactly like every other operator-supplied token that reaches
    an SSH command elsewhere in this codebase (container/unit names) — there
    is no path from this parameter to shell injection.

    Raises RgwLogError if the log can't be fetched at all. An empty string
    (no matching lines, or a genuinely empty log) is a normal outcome, not
    an error.
    """
    filter_text = (filter_text or "").strip()[:RGW_LOG_FILTER_MAX_CHARS] or None
    exec_mode = settings.ceph_exec_mode

    if exec_mode == "cephadm":
        names = _cephadm_rgw_daemon_names(host)
        if not names:
            raise RgwLogError(f"Không tìm thấy RGW daemon nào trên {host}")
        parts = []
        for name in names:
            try:
                output = run_command_on_node(
                    host, _cephadm_command(name, filter_text), RGW_LOG_COMMAND_TIMEOUT_SECONDS
                )
            except Exception as exc:
                raise RgwLogError(f"Không đọc được log RGW ({name}) trên {host}: {exc}") from exc
            parts.append(output if len(names) == 1 else f"--- {name} ---\n{output}")
        return "\n".join(parts)

    if exec_mode == "none":
        command = _none_mode_command(filter_text)
    else:
        if not settings.ceph_rgw_container_name:
            raise RgwLogError(
                "Chưa cấu hình tên container RGW (RGW container name) — thêm ở trang Cài đặt."
            )
        command = _docker_podman_command(filter_text)

    try:
        return run_command_on_node(host, command, RGW_LOG_COMMAND_TIMEOUT_SECONDS)
    except Exception as exc:
        raise RgwLogError(f"Không đọc được log RGW trên {host}: {exc}") from exc


def fetch_rgw_log_with(host: str, filter_text: str | None, ssh_user: str, ssh_key_path: str,
                       exec_mode: str, rgw_container_name: str) -> str:
    """Cluster-scoped RGW log fetch using explicit connection settings."""
    filter_text = (filter_text or "").strip()[:RGW_LOG_FILTER_MAX_CHARS] or None
    run = lambda command: run_command_on_node_with(
        host, command, ssh_user, ssh_key_path, RGW_LOG_COMMAND_TIMEOUT_SECONDS
    )
    if exec_mode == "cephadm":
        try:
            payload = json.loads(run("cephadm ls --no-detail"))
        except Exception as exc:
            raise RgwLogError(f"Không liệt kê được daemon trên {host}: {exc}") from exc
        names = [row["name"] for row in payload if isinstance(row, dict)
                 and isinstance(row.get("name"), str) and row["name"].startswith("rgw.")] \
            if isinstance(payload, list) else []
        if not names:
            raise RgwLogError(f"Không tìm thấy RGW daemon nào trên {host}")
        parts = []
        for name in names:
            output = run(_cephadm_command(name, filter_text))
            parts.append(output if len(names) == 1 else f"--- {name} ---\n{output}")
        return "\n".join(parts)
    if exec_mode == "none":
        command = _none_mode_command(filter_text)
    else:
        if not rgw_container_name:
            raise RgwLogError("Chưa cấu hình tên container RGW cho cụm đang chọn.")
        if filter_text:
            command = (f"{exec_mode} logs {shlex.quote(rgw_container_name)} --tail "
                       f"{RGW_LOG_FILTER_SCAN_LINES} 2>&1 | {_quoted_grep(filter_text)} "
                       f"| tail -n {RGW_LOG_MAX_DISPLAY_LINES}")
        else:
            command = f"{exec_mode} logs {shlex.quote(rgw_container_name)} --tail {RGW_LOG_TAIL_LINES} 2>&1"
    try:
        return run(command)
    except Exception as exc:
        raise RgwLogError(f"Không đọc được log RGW trên {host}: {exc}") from exc
