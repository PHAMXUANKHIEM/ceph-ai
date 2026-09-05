"""Read-only Vitastor CLI connection client."""

from __future__ import annotations

import json
import os
import shlex
import re

import paramiko

CONNECT_TIMEOUT_SECONDS = 3
COMMAND_TIMEOUT_SECONDS = 15
KNOWN_HOSTS_PATH = os.path.expanduser("~/.ssh/vitastor_aiops_known_hosts")
VALID_EXEC_MODES = {"none", "docker", "podman"}
LOG_SOURCES = {
    "all": ("vitastor-mon", "vitastor-osd*", "vitastor.target", "vitastor-etcd"),
    "mon": ("vitastor-mon",),
    "osd": ("vitastor-osd*",),
    "etcd": ("vitastor-etcd",),
    "target": ("vitastor.target",),
}
_DURATION_RE = re.compile(r"^([0-9.]+)(ns|µs|us|ms|s)$")


class VitastorConnectionError(RuntimeError):
    pass


def query_logs(
    management_host: str, ssh_user: str, ssh_key_path: str, *, source: str = "all",
    lines: int = 300,
) -> str:
    """Read bounded journald output from allowlisted Vitastor units."""
    if source not in LOG_SOURCES:
        raise VitastorConnectionError("Nguồn log không hợp lệ")
    lines = max(50, min(int(lines), 2000))
    units = " ".join(f"-u {shlex.quote(unit)}" for unit in LOG_SOURCES[source])
    command = f"journalctl --no-pager -o short-iso -n {lines} {units}"
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH): client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(hostname=management_host, username=ssh_user, key_filename=ssh_key_path, timeout=CONNECT_TIMEOUT_SECONDS)
        client.save_host_keys(KNOWN_HOSTS_PATH)
        _stdin, stdout, stderr = client.exec_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
        output, error = stdout.read().decode(errors="replace"), stderr.read().decode(errors="replace")
        status = stdout.channel.recv_exit_status()
        if status != 0: raise VitastorConnectionError(f"{management_host}: journalctl thoát mã {status}: {error.strip()}")
        return output[-500_000:]
    except (OSError, paramiko.SSHException) as exc:
        raise VitastorConnectionError(f"Không SSH được tới {management_host}: {exc}") from exc
    finally:
        client.close()


def _cli_command(
    etcd_address: str, etcd_prefix: str = "/vitastor", config_path: str = "",
    exec_mode: str = "none", container_name: str = "", command: tuple[str, ...] = ("status",),
) -> str:
    if exec_mode not in VALID_EXEC_MODES:
        raise VitastorConnectionError(f"exec mode không hợp lệ: {exec_mode}")
    args = ["vitastor-cli", "--json", "--no-color"]
    if config_path:
        args.extend(["--config_path", config_path])
    else:
        args.extend(["--etcd_address", etcd_address, "--etcd_prefix", etcd_prefix or "/vitastor"])
    args.extend(command)
    command = " ".join(shlex.quote(value) for value in args)
    if exec_mode in {"docker", "podman"}:
        if not container_name:
            raise VitastorConnectionError("Thiếu tên container Vitastor")
        command = f"{exec_mode} exec {shlex.quote(container_name)} {command}"
    return command


def query_status(
    management_host: str, ssh_user: str, ssh_key_path: str, etcd_address: str,
    etcd_prefix: str = "/vitastor", config_path: str = "", exec_mode: str = "none",
    container_name: str = "",
) -> dict:
    """SSH to a management host and return ``vitastor-cli status`` JSON."""
    command = _cli_command(etcd_address, etcd_prefix, config_path, exec_mode, container_name)
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=management_host, username=ssh_user, key_filename=ssh_key_path,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        client.save_host_keys(KNOWN_HOSTS_PATH)
        _stdin, stdout, stderr = client.exec_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
        output = stdout.read().decode()
        error = stderr.read().decode()
        status = stdout.channel.recv_exit_status()
        if status != 0:
            raise VitastorConnectionError(
                f"{management_host}: vitastor-cli thoát mã {status}: {error.strip()}"
            )
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise VitastorConnectionError("vitastor-cli không trả về JSON hợp lệ") from exc
        if not isinstance(payload, dict):
            raise VitastorConnectionError("Dữ liệu status Vitastor không đúng định dạng")
        return payload
    except (OSError, paramiko.SSHException) as exc:
        raise VitastorConnectionError(f"Không SSH được tới {management_host}: {exc}") from exc
    finally:
        client.close()


def query_dashboard(
    management_host: str, ssh_user: str, ssh_key_path: str, etcd_address: str,
    etcd_prefix: str = "/vitastor", config_path: str = "", exec_mode: str = "none",
    container_name: str = "",
) -> dict:
    """Collect the read-only datasets used by the Vitastor overview.

    ``status`` is mandatory. Detail commands are best-effort because older
    Vitastor releases may not support every long/stats flag; their errors are
    returned per section instead of hiding an otherwise healthy cluster.
    """
    commands = {
        "status": ("status",),
        "pools": ("ls-pools", "--stats"),
        "osds": ("osd-tree", "-l"),
        "images": ("ls", "-l"),
    }
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=management_host, username=ssh_user, key_filename=ssh_key_path,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        client.save_host_keys(KNOWN_HOSTS_PATH)
        result: dict[str, object] = {"errors": {}}
        for section, command_args in commands.items():
            command = _cli_command(
                etcd_address, etcd_prefix, config_path, exec_mode, container_name,
                command_args,
            )
            _stdin, stdout, stderr = client.exec_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
            output = stdout.read().decode()
            error = stderr.read().decode().strip()
            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                if section == "status":
                    raise VitastorConnectionError(
                        f"{management_host}: vitastor-cli status thoát mã {exit_status}: {error}"
                    )
                result["errors"][section] = error or f"exit {exit_status}"  # type: ignore[index]
                result[section] = []
                continue
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                if section == "status":
                    raise VitastorConnectionError("vitastor-cli status không trả về JSON hợp lệ")
                result["errors"][section] = "JSON không hợp lệ"  # type: ignore[index]
                result[section] = []
                continue
            result[section] = payload
        if etcd_address:
            endpoint = shlex.quote(etcd_address)
            for section, subcommand in {
                "etcd_status": "endpoint status --cluster -w json",
                "etcd_health": "endpoint health --cluster -w json",
            }.items():
                command = f"ETCDCTL_API=3 etcdctl --endpoints={endpoint} {subcommand}"
                _stdin, stdout, stderr = client.exec_command(command, timeout=COMMAND_TIMEOUT_SECONDS)
                output, error = stdout.read().decode(), stderr.read().decode().strip()
                exit_status = stdout.channel.recv_exit_status()
                if exit_status != 0:
                    result["errors"][section] = error or f"exit {exit_status}"  # type: ignore[index]
                    result[section] = []
                    continue
                try:
                    result[section] = json.loads(output)
                except json.JSONDecodeError:
                    result["errors"][section] = "JSON không hợp lệ"  # type: ignore[index]
                    result[section] = []
        if not isinstance(result.get("status"), dict):
            raise VitastorConnectionError("Dữ liệu status Vitastor không đúng định dạng")
        return result
    except (OSError, paramiko.SSHException) as exc:
        raise VitastorConnectionError(f"Không SSH được tới {management_host}: {exc}") from exc
    finally:
        client.close()


def _duration_ms(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value) * 1000
    match = _DURATION_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    amount, unit = float(match.group(1)), match.group(2)
    return amount * {"ns": 0.000001, "µs": 0.001, "us": 0.001, "ms": 1, "s": 1000}[unit]


def normalize_etcd(status_rows, health_rows) -> dict:
    """Normalize etcdctl JSON across etcd 3.4+ output shapes."""
    statuses = status_rows if isinstance(status_rows, list) else []
    health = health_rows if isinstance(health_rows, list) else []
    members = []
    leaders = set()
    for row in statuses:
        if not isinstance(row, dict):
            continue
        payload = row.get("Status") if isinstance(row.get("Status"), dict) else row.get("status")
        payload = payload if isinstance(payload, dict) else row
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        member_id = str(header.get("member_id") or payload.get("member_id") or "")
        leader = str(payload.get("leader") or "")
        if leader and leader != "0": leaders.add(leader)
        endpoint = str(row.get("Endpoint") or row.get("endpoint") or "")
        members.append({
            "endpoint": endpoint, "member_id": member_id, "leader_id": leader,
            "is_leader": bool(member_id and leader and member_id == leader),
            "db_size": int(payload.get("dbSize") or payload.get("db_size") or 0),
            "raft_index": int(payload.get("raftIndex") or payload.get("raft_index") or 0),
        })
    health_by_endpoint = {}
    for row in health:
        if not isinstance(row, dict): continue
        endpoint = str(row.get("endpoint") or row.get("Endpoint") or "")
        latency = _duration_ms(row.get("took") or row.get("latency"))
        health_by_endpoint[endpoint] = {
            "healthy": bool(row.get("health", row.get("Health", False))), "latency_ms": latency,
            "error": str(row.get("error") or ""),
        }
    for member in members:
        member.update(health_by_endpoint.get(member["endpoint"], {}))
    latencies = [v["latency_ms"] for v in health_by_endpoint.values() if v.get("latency_ms") is not None]
    healthy = sum(1 for value in health_by_endpoint.values() if value.get("healthy"))
    total = max(len(members), len(health_by_endpoint))
    return {
        "members": members, "healthy": healthy, "total": total,
        "quorum": healthy >= total // 2 + 1 if total else False,
        "leader_count": len(leaders), "latency_ms": max(latencies) if latencies else None,
        "db_size": sum(member["db_size"] for member in members),
    }


def normalize_status(payload: dict) -> dict:
    """Map the official status JSON into a stable dashboard contract."""
    def integer(key: str) -> int:
        value = payload.get(key, 0)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    total_raw = integer("total_raw")
    free_raw = integer("free_raw")
    used_raw = max(0, total_raw - free_raw)
    pg_states = payload.get("pg_states") if isinstance(payload.get("pg_states"), dict) else {}
    op_stats = payload.get("op_stats") if isinstance(payload.get("op_stats"), dict) else {}
    recovery_stats = (
        payload.get("recovery_stats") if isinstance(payload.get("recovery_stats"), dict) else {}
    )
    object_counts = (
        payload.get("object_counts") if isinstance(payload.get("object_counts"), dict) else {}
    )
    unhealthy_pg_count = sum(
        int(count) for state, count in pg_states.items()
        if state != "active" and isinstance(count, (int, float))
    )
    slow_primary = payload.get("osds_primary_slow_ops") or []
    slow_secondary = payload.get("osds_secondary_slow_ops") or []
    flags = [name for name in ("readonly", "no_recovery", "no_rebalance", "no_scrub") if payload.get(name)]
    read_stats = op_stats.get("read") if isinstance(op_stats.get("read"), dict) else {}
    write_stats = op_stats.get("write") if isinstance(op_stats.get("write"), dict) else {}
    total_iops = float(read_stats.get("iops") or 0) + float(write_stats.get("iops") or 0)
    total_bps = float(read_stats.get("bps") or 0) + float(write_stats.get("bps") or 0)
    latency_values = []
    for stats in (read_stats, write_stats):
        value = stats.get("latency_ms")
        if isinstance(value, (int, float)): latency_values.append(float(value))
        else:
            value = stats.get("latency_us", stats.get("usec"))
            if isinstance(value, (int, float)): latency_values.append(float(value) / 1000)
    core_fields = ("etcd_alive", "etcd_count", "osd_up", "osd_count")
    core_complete = (
        all(isinstance(payload.get(key), (int, float)) and not isinstance(payload.get(key), bool) for key in core_fields)
        and integer("etcd_count") > 0
        and integer("osd_count") > 0
    )
    detail_complete = (
        core_complete
        and all(isinstance(payload.get(key), (int, float)) and not isinstance(payload.get(key), bool)
                for key in ("pool_count", "active_pool_count"))
        and isinstance(payload.get("pg_states"), dict)
    )
    health = "UNKNOWN"
    if core_complete and (
        integer("etcd_alive") < integer("etcd_count")
        or integer("osd_up") < integer("osd_count")
    ):
        health = "CRITICAL"
    elif detail_complete:
        health = "HEALTHY"
        if integer("etcd_alive") < integer("etcd_count") or integer("osd_up") < integer("osd_count"):
            health = "CRITICAL"
        elif unhealthy_pg_count or integer("osds_full") or slow_primary or slow_secondary:
            health = "CRITICAL"
        elif integer("osds_nearfull") or flags or integer("active_pool_count") < integer("pool_count"):
            health = "WARNING"
    return {
        "health": health,
        "etcd": {"up": integer("etcd_alive"), "total": integer("etcd_count"), "db_size": integer("etcd_db_size")},
        "mon": {"count": integer("mon_count"), "master": str(payload.get("mon_master") or "")},
        "osds": {
            "up": integer("osd_up"), "total": integer("osd_count"),
            "full": integer("osds_full"), "nearfull": integer("osds_nearfull"),
            "primary_slow": slow_primary, "secondary_slow": slow_secondary,
        },
        "capacity": {
            "total": total_raw, "used": used_raw, "free": free_raw,
            "down": integer("down_raw"),
            "used_percent": round(used_raw / total_raw * 100, 2) if total_raw else 0,
        },
        "pools": {"active": integer("active_pool_count"), "total": integer("pool_count"), "backfillfull": payload.get("backfillfull_pools") or []},
        "pg_states": pg_states,
        "objects": object_counts,
        "data_states": {name: integer(f"{name}_data") for name in ("clean", "misplaced", "degraded", "incomplete")},
        "io": op_stats,
        "metrics": {
            "latency_ms": round(sum(latency_values) / len(latency_values), 2) if latency_values else None,
            "bandwidth_bps": total_bps,
            "iops": total_iops,
        },
        "placement_groups": (
            "ACTIVE" if pg_states and set(pg_states) == {"active"}
            else "DEGRADED" if pg_states else "UNKNOWN"
        ),
        "recovery": recovery_stats,
        "flags": flags,
    }
