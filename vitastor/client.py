"""Read-only Vitastor CLI connection client."""

from __future__ import annotations

import base64
import hashlib
import fcntl
import json
import os
import shlex
import re
import tempfile
from contextlib import contextmanager

import paramiko
from paramiko.hostkeys import HostKeyEntry, InvalidHostKey

CONNECT_TIMEOUT_SECONDS = 3
COMMAND_TIMEOUT_SECONDS = 15
_DEFAULT_KNOWN_HOSTS_PATH = (
    "/var/lib/ceph-ai/ssh/vitastor_known_hosts"
    if os.environ.get("CEPH_AI_CONTAINERIZED", "").lower() == "true"
    else os.path.expanduser("~/.ssh/vitastor_aiops_known_hosts")
)
KNOWN_HOSTS_PATH = os.environ.get("CEPH_AI_VITASTOR_KNOWN_HOSTS_PATH", _DEFAULT_KNOWN_HOSTS_PATH)
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


class VitastorHostKeyProvisionError(ValueError):
    """Raised when an operator-supplied Vitastor host key cannot be pinned."""


_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,253}$")


@contextmanager
def _host_keys_lock():
    directory = os.path.dirname(KNOWN_HOSTS_PATH) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    lock_path = f"{KNOWN_HOSTS_PATH}.lock"
    with open(lock_path, "a+") as lock_file:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _write_host_keys_atomically(host_keys: paramiko.HostKeys) -> None:
    directory = os.path.dirname(KNOWN_HOSTS_PATH) or "."
    fd, temporary_path = tempfile.mkstemp(prefix=".vitastor_known_hosts.", dir=directory)
    try:
        os.close(fd)
        host_keys.save(temporary_path)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, KNOWN_HOSTS_PATH)
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


def provision_host_key(host: str, public_key: str) -> str:
    """Pin an operator-verified node host key; never use trust-on-first-use.

    ``public_key`` must be obtained and checked outside the Dashboard, for
    example from the node console or a separately trusted administrator host.
    Replacing an existing entry is deliberate so an operator can recover a
    node after reinstalling it without disabling host-key verification.
    """
    if not _HOST_RE.fullmatch(str(host or "").strip()):
        raise VitastorHostKeyProvisionError("IP/hostname không hợp lệ")
    parts = str(public_key or "").strip().split()
    if len(parts) < 2 or not re.fullmatch(r"(?:ssh|ecdsa)-[A-Za-z0-9@._+-]+", parts[0]):
        raise VitastorHostKeyProvisionError("SSH host public key không hợp lệ")
    try:
        # Validate the base64 key before modifying the trust store. Paramiko
        # performs the key-type-specific validation below as well.
        base64.b64decode(parts[1], validate=True)
        entry = HostKeyEntry.from_line(f"{host} {parts[0]} {parts[1]}")
    except (TypeError, ValueError, InvalidHostKey) as exc:
        raise VitastorHostKeyProvisionError("SSH host public key không hợp lệ") from exc
    if entry is None or entry.key is None:
        raise VitastorHostKeyProvisionError("Loại SSH host key không được hỗ trợ")

    with _host_keys_lock():
        host_keys = paramiko.HostKeys()
        if os.path.exists(KNOWN_HOSTS_PATH):
            try:
                host_keys.load(KNOWN_HOSTS_PATH)
            except (OSError, paramiko.SSHException) as exc:
                raise VitastorHostKeyProvisionError("Không đọc được kho SSH host key hiện tại") from exc
        host_keys.pop(host, None)
        host_keys.add(host, entry.key.get_name(), entry.key)
        try:
            _write_host_keys_atomically(host_keys)
        except OSError as exc:
            raise VitastorHostKeyProvisionError("Không thể lưu SSH host key") from exc
    return entry.key.get_name()


def list_host_keys() -> list[dict[str, str]]:
    """Return the pinned Vitastor node keys without exposing key material."""
    if not os.path.exists(KNOWN_HOSTS_PATH):
        return []
    try:
        host_keys = paramiko.HostKeys()
        host_keys.load(KNOWN_HOSTS_PATH)
    except (OSError, paramiko.SSHException) as exc:
        raise VitastorHostKeyProvisionError("Không đọc được kho SSH host key Vitastor") from exc
    result = []
    for host, keys in sorted(host_keys.items()):
        for key_type, key in sorted(keys.items()):
            result.append({
                "host": host,
                "key_type": key_type,
                "fingerprint": "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("="),
            })
    return result


def remove_host_key(host: str) -> bool:
    """Remove one pinned Vitastor node key; never alter other hosts."""
    host = str(host or "").strip()
    if not _HOST_RE.fullmatch(host):
        raise VitastorHostKeyProvisionError("IP/hostname không hợp lệ")
    with _host_keys_lock():
        if not os.path.exists(KNOWN_HOSTS_PATH):
            return False
        host_keys = paramiko.HostKeys()
        try:
            host_keys.load(KNOWN_HOSTS_PATH)
        except (OSError, paramiko.SSHException) as exc:
            raise VitastorHostKeyProvisionError("Không đọc được kho SSH host key Vitastor") from exc
        removed = host_keys.pop(host, None) is not None
        if removed:
            try:
                _write_host_keys_atomically(host_keys)
            except OSError as exc:
                raise VitastorHostKeyProvisionError("Không thể lưu kho SSH host key Vitastor") from exc
        return removed


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
