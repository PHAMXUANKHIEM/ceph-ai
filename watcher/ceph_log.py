"""Bounded, cluster-aware Ceph daemon log reader for the Dashboard."""

import json
import shlex

from config.settings import settings
from watcher.ceph_client import run_command_on_node, run_command_on_node_with

CEPH_LOG_TAIL_LINES = 100
CEPH_LOG_FILTER_SCAN_LINES = 3000
CEPH_LOG_MAX_DISPLAY_LINES = 300
CEPH_LOG_FILTER_MAX_CHARS = 200
CEPH_LOG_COMMAND_TIMEOUT_SECONDS = 15
CEPH_LOG_SERVICES = frozenset({"mon", "mgr", "osd", "rgw"})


class CephLogError(Exception):
    pass


def _grep(filter_text: str) -> str:
    return f"grep -i -- {shlex.quote(filter_text)}"


def _tail(command: str, filter_text: str | None, tail_lines: int | None = None) -> str:
    if filter_text:
        return (
            f"{command} 2>&1 | tail -n {CEPH_LOG_FILTER_SCAN_LINES} | "
            f"{_grep(filter_text)} | tail -n {CEPH_LOG_MAX_DISPLAY_LINES}"
        )
    # `tail_lines` (2026-08-18, Log Intelligence L0): the Dashboard's own
    # display path never passes it and keeps CEPH_LOG_TAIL_LINES exactly as
    # before -- watcher/log_source/ssh_tail.py passes a much larger window
    # because it is building an analysis corpus, not rendering a log panel.
    return f"{command} 2>&1 | tail -n {tail_lines or CEPH_LOG_TAIL_LINES}"


def _container_for(service: str, mon_container: str, osd_container: str, rgw_container: str) -> str:
    # Older docker deployments commonly colocate MON and MGR in the same
    # management container; there is no separate MGR field in Cluster.
    return {
        "mon": mon_container,
        "mgr": mon_container,
        "osd": osd_container,
        "rgw": rgw_container,
    }[service]


def _fetch(run, host: str, service: str, filter_text: str | None, exec_mode: str,
           mon_container: str, osd_container: str, rgw_container: str,
           tail_lines: int | None = None) -> str:
    if service not in CEPH_LOG_SERVICES:
        raise CephLogError(f"Dịch vụ Ceph không hợp lệ: {service}")
    filter_text = (filter_text or "").strip()[:CEPH_LOG_FILTER_MAX_CHARS] or None

    try:
        if exec_mode == "cephadm":
            payload = json.loads(run(host, "cephadm ls --no-detail"))
            prefix = f"{service}."
            names = [
                row["name"] for row in payload
                if isinstance(row, dict)
                and isinstance(row.get("name"), str)
                and row["name"].startswith(prefix)
            ] if isinstance(payload, list) else []
            if not names:
                raise CephLogError(f"Không tìm thấy daemon {service.upper()} trên {host}")
            parts = []
            for name in names:
                output = run(
                    host,
                    _tail(f"cephadm logs --name {shlex.quote(name)}", filter_text, tail_lines),
                )
                parts.append(output if len(names) == 1 else f"--- {name} ---\n{output}")
            return "\n".join(parts)

        default_count = tail_lines or CEPH_LOG_TAIL_LINES
        if exec_mode == "none":
            unit = "ceph-radosgw@*" if service == "rgw" else f"ceph-{service}@*"
            count = CEPH_LOG_FILTER_SCAN_LINES if filter_text else default_count
            command = f"journalctl -u {shlex.quote(unit)} -n {count} --no-pager 2>&1"
        else:
            container = _container_for(service, mon_container, osd_container, rgw_container)
            if not container:
                raise CephLogError(
                    f"Chưa cấu hình tên container cho dịch vụ {service.upper()} của cụm đang chọn."
                )
            count = CEPH_LOG_FILTER_SCAN_LINES if filter_text else default_count
            command = f"{exec_mode} logs {shlex.quote(container)} --tail {count} 2>&1"
        if filter_text:
            command = f"{command} | {_grep(filter_text)} | tail -n {CEPH_LOG_MAX_DISPLAY_LINES}"
        return run(host, command)
    except CephLogError:
        raise
    except Exception as exc:
        raise CephLogError(
            f"Không đọc được log {service.upper()} trên {host}: {exc}"
        ) from exc


def fetch_ceph_log(host: str, service: str, filter_text: str | None = None) -> str:
    def run(target: str, command: str) -> str:
        return run_command_on_node(target, command, CEPH_LOG_COMMAND_TIMEOUT_SECONDS)

    return _fetch(
        run, host, service, filter_text, settings.ceph_exec_mode,
        settings.ceph_container_name, settings.ceph_osd_container_name,
        settings.ceph_rgw_container_name,
    )


def fetch_ceph_log_with(host: str, service: str, filter_text: str | None,
                        ssh_user: str, ssh_key_path: str, exec_mode: str,
                        mon_container: str, osd_container: str, rgw_container: str,
                        tail_lines: int | None = None,
                        timeout: int = CEPH_LOG_COMMAND_TIMEOUT_SECONDS) -> str:
    def run(target: str, command: str) -> str:
        return run_command_on_node_with(target, command, ssh_user, ssh_key_path, timeout)

    return _fetch(
        run, host, service, filter_text, exec_mode,
        mon_container, osd_container, rgw_container, tail_lines,
    )
