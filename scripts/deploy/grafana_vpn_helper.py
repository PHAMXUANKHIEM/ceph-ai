#!/usr/bin/env python3
"""Narrow host control socket for the Grafana OpenVPN client.

The web container never receives NET_ADMIN, the OpenVPN process, or the
profile on disk.  Only a certificate based, inline profile can be installed.
"""

import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
# All subprocess calls use fixed executable paths, fixed units and validated IPs.
import subprocess  # nosec B404
import tempfile


CONFIG_DIR = Path("/etc/ceph-ai/vpn")
PROFILE = CONFIG_DIR / "grafana.ovpn"
TARGET = CONFIG_DIR / "grafana-target.env"
UNIT = "ceph-ai-grafana-vpn.service"
MAX_PROFILE_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 400 * 1024
BLOCKS = frozenset(("ca", "cert", "key", "tls-auth", "tls-crypt"))
OPTIONS = frozenset((
    "client", "dev", "proto", "remote", "resolv-retry", "nobind",
    "persist-key", "persist-tun", "remote-cert-tls", "verify-x509-name",
    "cipher", "ncp-ciphers", "auth", "verb",
    "mute", "reneg-sec", "explicit-exit-notify", "auth-nocache",
    "key-direction", "tls-version-min", "tls-cipher",
    "connect-retry", "connect-timeout", "server-poll-timeout",
    "auth-retry", "float", "sndbuf", "rcvbuf", "keepalive",
    "ping", "ping-restart", "remote-random", "remote-random-hostname",
    "tls-client", "auth-user-pass",
))
FORBIDDEN_DESTINATIONS = tuple(ipaddress.ip_network(value) for value in (
    "10.20.1.0/24", "10.3.52.0/22", "10.5.0.0/16", "10.88.0.0/16",
    "10.89.0.0/24", "127.0.0.0/8", "169.254.0.0/16",
))


def _validate_directive(value: str, seen: set[str]) -> None:
    parts = value.split()
    directive = parts[0].lstrip("-").lower()
    if directive not in OPTIONS or parts[0].startswith("-"):
        raise ValueError("Tùy chọn .ovpn không được phép: %s" % directive)
    seen.add(directive)
    if directive in BLOCKS or directive == "auth-user-pass":
        raise ValueError("Chỉ hỗ trợ chứng chỉ/khóa nhúng trong .ovpn; không hỗ trợ file hoặc mật khẩu ngoài.")
    if directive == "dev" and parts[1:] != ["tun"]:
        raise ValueError("Profile chỉ được sử dụng dev tun.")
    if directive == "remote":
        _validate_remote(parts)
    if directive == "proto" and (len(parts) != 2 or parts[1] not in ("udp", "udp4", "tcp", "tcp4", "tcp-client")):
        raise ValueError("Chỉ hỗ trợ UDP hoặc TCP IPv4.")


def _validate_remote(parts: list[str]) -> None:
    if len(parts) not in (2, 3, 4) or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", parts[1]):
        raise ValueError("Địa chỉ VPN remote không hợp lệ.")
    if len(parts) >= 3 and (not parts[2].isdigit() or not 1 <= int(parts[2]) <= 65535):
        raise ValueError("Cổng VPN remote không hợp lệ.")


def _decode_profile(raw: bytes) -> str:
    if not raw or len(raw) > MAX_PROFILE_BYTES or b"\x00" in raw:
        raise ValueError("File .ovpn trống, quá lớn hoặc không hợp lệ (tối đa 256 KiB).")
    try:
        return raw.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise ValueError("File .ovpn phải là UTF-8.") from exc


def validate_profile(raw: bytes) -> str:
    profile = _decode_profile(raw)
    active_block = None
    seen = set()
    for line in profile.splitlines():
        value = line.strip()
        if active_block:
            if value == "</%s>" % active_block:
                active_block = None
            elif value.startswith("</") or value.startswith("<"):
                raise ValueError("Khối chứng chỉ trong .ovpn không hợp lệ.")
            continue
        if not value or value[0] in "#;":
            continue
        match = re.fullmatch(r"<([a-z-]+)>", value)
        if match:
            active_block = match.group(1)
            if active_block not in BLOCKS:
                raise ValueError("Khối .ovpn không được hỗ trợ: %s" % active_block)
            seen.add(active_block)
            continue
        _validate_directive(value, seen)
    if active_block:
        raise ValueError("Khối chứng chỉ .ovpn chưa đóng.")
    if not {"client", "dev", "remote", "ca"}.issubset(seen):
        raise ValueError("Profile cần client, dev tun, remote và chứng chỉ CA nhúng.")
    return profile.rstrip() + "\n"


def validate_target(value: str) -> str:
    try:
        target = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise ValueError("Nhập IPv4 đích riêng của Grafana do network owner cung cấp.") from exc
    if target.is_multicast or target.is_unspecified or any(target in net for net in FORBIDDEN_DESTINATIONS):
        raise ValueError("IP đích trùng mạng quản trị, Ceph, DB hoặc mạng container.")
    return str(target)


def _systemctl(*args: str) -> bool:
    try:
        # The unit and every caller-provided argument are fixed literals.
        return subprocess.run(["/usr/bin/systemctl", *args, UNIT],  # nosec B603
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _target():
    try:
        value = TARGET.read_text(encoding="ascii").strip()
        if not value.startswith("GRAFANA_IP="):
            return None
        return validate_target(value[len("GRAFANA_IP="):])
    except (OSError, ValueError):
        return None


def _route_device(target: str):
    try:
        # target only comes from validate_target(), so it is a canonical IPv4 address.
        result = subprocess.run(["/usr/sbin/ip", "-j", "route", "get", target],  # nosec B603
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=3, check=False)
        if result.returncode == 0:
            route = json.loads(result.stdout)
            return route[0].get("dev") if route else None
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    return None


def status() -> dict:
    installed = Path("/usr/sbin/openvpn").is_file()
    configured = PROFILE.is_file() and _target() is not None
    active = _systemctl("is-active", "--quiet")
    target = _target()
    device = _route_device(target) if active and target else None
    state = "connected" if active and device and device.startswith("tun") else "connecting" if active else "disconnected"
    return {"installed": installed, "configured": configured, "state": state,
            "target": target, "route_device": device if state == "connected" else None}


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".vpn-", dir=CONFIG_DIR)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def handle(request: dict) -> dict:
    action = request.get("action")
    if action == "status":
        return status()
    if action == "upload":
        if _systemctl("is-active", "--quiet"):
            raise ValueError("Hãy ngắt VPN trước khi thay profile.")
        try:
            raw = base64.b64decode(request.get("profile", ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("File .ovpn không hợp lệ.") from exc
        profile = validate_profile(raw)
        target = validate_target(request.get("target", ""))
        CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(CONFIG_DIR, 0o700)
        _atomic_write(PROFILE, profile.encode("utf-8"), 0o600)
        _atomic_write(TARGET, ("GRAFANA_IP=%s\n" % target).encode("ascii"), 0o600)
        return status()
    if action == "connect":
        if not status()["installed"] or not status()["configured"]:
            raise ValueError("Chưa cài OpenVPN hoặc chưa tải profile hợp lệ.")
        if not _systemctl("enable", "--now"):
            raise ValueError("Không khởi động được OpenVPN. Kiểm tra journalctl của service.")
        return status()
    if action == "disconnect":
        if not _systemctl("disable", "--now"):
            raise ValueError("Không thể ngắt OpenVPN. Kiểm tra systemd service.")
        return status()
    raise ValueError("Thao tác VPN không hợp lệ.")


def _serve(connection: socket.socket) -> None:
    connection.settimeout(15)
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        chunk = connection.recv(min(65536, MAX_REQUEST_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in chunk:
            break
    try:
        if len(data) > MAX_REQUEST_BYTES or not data.endswith(b"\n"):
            raise ValueError("Yêu cầu quá lớn hoặc chưa hoàn chỉnh.")
        request = json.loads(data.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("Yêu cầu VPN không hợp lệ.")
        response = handle(request)
    except (ValueError, OSError, TypeError, json.JSONDecodeError) as exc:
        response = {"error": str(exc)}
    connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8"))


def main() -> None:
    if os.environ.get("LISTEN_FDS") != "1":
        raise SystemExit("systemd socket activation is required")
    listener = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    while True:
        connection, _ = listener.accept()
        with connection:
            _serve(connection)


if __name__ == "__main__":
    main()
