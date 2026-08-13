"""Read-only hardware and process telemetry for Vitastor OSD hosts."""
from __future__ import annotations

import json
import os
import shlex
import re

import paramiko

from vitastor.client import CONNECT_TIMEOUT_SECONDS, KNOWN_HOSTS_PATH


def _exec(client, command: str) -> tuple[int, str, str]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=30)
    return stdout.channel.recv_exit_status(), stdout.read().decode(), stderr.read().decode()


def _smart_summary(payload: dict) -> dict:
    nvme = payload.get("nvme_smart_health_information_log") or {}
    temperature = nvme.get("temperature")
    wear = nvme.get("percentage_used")
    media_errors = int(nvme.get("media_errors") or 0) + int(nvme.get("num_err_log_entries") or 0)
    attributes = ((payload.get("ata_smart_attributes") or {}).get("table") or [])
    for attr in attributes:
        name = str(attr.get("name") or "").lower()
        raw = (attr.get("raw") or {}).get("value")
        if temperature is None and "temperature" in name and isinstance(raw, (int, float)): temperature = raw
        if wear is None and any(token in name for token in ("wear", "percent_lifetime", "percentage_used")):
            wear = raw if isinstance(raw, (int, float)) else wear
        if any(token in name for token in ("uncorrect", "error_count", "pending_sector")) and isinstance(raw, (int, float)):
            media_errors += int(raw)
    passed = (payload.get("smart_status") or {}).get("passed")
    return {"temperature_c": temperature, "wear_percent": wear, "media_errors": media_errors, "smart_passed": passed}


def query_node_hardware(host: str, ssh_user: str, ssh_key_path: str) -> dict:
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH): client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, username=ssh_user, key_filename=ssh_key_path, timeout=CONNECT_TIMEOUT_SECONDS)
        client.save_host_keys(KNOWN_HOSTS_PATH)
        _code, process_out, _error = _exec(client, "ps -C vitastor-osd -o %cpu=,rss= --no-headers || true")
        cpu = ram = processes = 0
        for line in process_out.splitlines():
            fields = line.split()
            if len(fields) >= 2:
                cpu += float(fields[0]); ram += int(fields[1]) * 1024; processes += 1
        _code, devices_out, _error = _exec(client, "for d in /dev/vitastor/osd*-data; do [ -e \"$d\" ] || continue; r=$(readlink -f \"$d\"); b=$(lsblk -ndo PKNAME \"$r\" | head -n1); [ -n \"$b\" ] || b=$(basename \"$r\"); printf '%s\\t/dev/%s\\n' \"$d\" \"$b\"; done")
        devices = []
        seen = set()
        for line in devices_out.splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2 or parts[1] in seen: continue
            seen.add(parts[1]); code, output, error = _exec(client, f"smartctl -j -a {shlex.quote(parts[1])}")
            try: smart = _smart_summary(json.loads(output))
            except (json.JSONDecodeError, TypeError): smart = {"error": error.strip() or f"smartctl exit {code}"}
            devices.append({"osd_path": parts[0], "device": parts[1], **smart})
        return {"host": host, "osd_processes": processes, "cpu_percent": cpu, "ram_bytes": ram, "devices": devices}
    finally:
        client.close()


def query_node_network(host: str, targets: list[str], ssh_user: str, ssh_key_path: str) -> dict:
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH): client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(hostname=host, username=ssh_user, key_filename=ssh_key_path, timeout=CONNECT_TIMEOUT_SECONDS)
        client.save_host_keys(KNOWN_HOSTS_PATH)
        _code, output, _error = _exec(client, "for n in /sys/class/net/*; do i=$(basename \"$n\"); [ \"$i\" = lo ] && continue; printf '%s ' \"$i\"; cat \"$n/operstate\" \"$n/mtu\" \"$n/speed\" \"$n/statistics/rx_errors\" \"$n/statistics/rx_dropped\" \"$n/statistics/tx_errors\" \"$n/statistics/tx_dropped\" 2>/dev/null | tr '\\n' ' '; echo; done")
        interfaces = []
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 8:
                def integer(index):
                    try: return int(fields[index])
                    except ValueError: return 0
                interfaces.append({"name": fields[0], "state": fields[1], "mtu": integer(2), "speed_mbps": integer(3), "rx_errors": integer(4), "rx_dropped": integer(5), "tx_errors": integer(6), "tx_dropped": integer(7)})
        probes = []
        for target in targets:
            quoted = shlex.quote(target)
            code, ping_out, _error = _exec(client, f"ping -n -c 3 -W 1 {quoted} 2>/dev/null || true")
            match = re.search(r"= [0-9.]+/([0-9.]+)/", ping_out)
            rtt = float(match.group(1)) if match else None
            jumbo_code, _out, _error = _exec(client, f"ping -n -c 1 -W 1 -M do -s 8972 {quoted} >/dev/null 2>&1")
            probes.append({"target": target, "reachable": rtt is not None, "rtt_ms": rtt, "jumbo_9000": jumbo_code == 0})
        return {"source": host, "interfaces": interfaces, "probes": probes}
    finally:
        client.close()
