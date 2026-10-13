#!/usr/bin/env python3
"""Narrow host control socket for the Grafana OpenVPN client.

The web container never receives NET_ADMIN, the OpenVPN process, or the
profile on disk. Only inline CA/key blocks and a narrowly scoped, temporary
username/password file for OTP profiles are supported.
"""

import base64
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
UNIT = "ceph-ai-grafana-vpn.service"
TUNNEL_DEVICE = "cephvpn0"
RUNTIME_DIR = Path("/run/ceph-ai-grafana-vpn")
CREDENTIALS = RUNTIME_DIR / "credentials"
AUTH_LINE = f"auth-user-pass {CREDENTIALS}"
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
def _validate_directive(value: str, seen: set[str]) -> str | None:
    parts = value.split()
    directive = parts[0].lstrip("-").lower()
    if directive not in OPTIONS or parts[0].startswith("-"):
        raise ValueError("Tùy chọn .ovpn không được phép: %s" % directive)
    seen.add(directive)
    if directive in BLOCKS:
        raise ValueError("Chỉ hỗ trợ chứng chỉ/khóa nhúng trong .ovpn, không hỗ trợ file tham chiếu ngoài.")
    if directive == "auth-user-pass":
        if len(parts) != 1:
            raise ValueError("Không cho phép .ovpn tham chiếu file mật khẩu bên ngoài.")
        return AUTH_LINE
    if directive == "dev" and parts[1:] != ["tun"]:
        raise ValueError("Profile chỉ được sử dụng dev tun.")
    if directive == "remote":
        _validate_remote(parts)
    if directive == "proto" and (len(parts) != 2 or parts[1] not in ("udp", "udp4", "tcp", "tcp4", "tcp-client")):
        raise ValueError("Chỉ hỗ trợ UDP hoặc TCP IPv4.")
    if directive == "remote-cert-tls" and parts[1:] != ["server"]:
        raise ValueError("Profile phải kiểm tra chứng chỉ của VPN server.")
    if directive == "auth-retry" and parts[1:] != ["none"]:
        raise ValueError("Profile OTP chỉ hỗ trợ auth-retry none để tránh lặp lại OTP sai.")
    return None


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


def _check_required_options(seen: set[str]) -> None:
    if not {"client", "dev", "remote", "ca", "remote-cert-tls"}.issubset(seen):
        raise ValueError("Profile cần client, dev tun, remote, CA nhúng và remote-cert-tls server.")
    if "auth-user-pass" not in seen and not {"cert", "key"}.issubset(seen):
        raise ValueError("Profile cần chứng chỉ/khóa client nhúng hoặc auth-user-pass.")


def validate_profile(raw: bytes) -> str:
    profile = _decode_profile(raw)
    lines = profile.splitlines()
    active_block = None
    seen = set()
    for index, line in enumerate(lines):
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
        lines[index] = _validate_directive(value, seen) or line
    if active_block:
        raise ValueError("Khối chứng chỉ .ovpn chưa đóng.")
    _check_required_options(seen)
    return "\n".join(lines).rstrip() + "\n"


def _systemctl(*args: str) -> bool:
    try:
        # The unit and every caller-provided argument are fixed literals.
        return subprocess.run(["/usr/bin/systemctl", *args, UNIT],  # nosec B603
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _tunnel_present() -> bool:
    try:
        result = subprocess.run(["/usr/sbin/ip", "-j", "link", "show", "dev", TUNNEL_DEVICE],  # nosec B603
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=3, check=False)
        return result.returncode == 0 and bool(json.loads(result.stdout))
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return False


def status() -> dict:
    installed = Path("/usr/sbin/openvpn").is_file()
    configured = PROFILE.is_file()
    auth_required = _auth_required()
    active = _systemctl("is-active", "--quiet")
    device = TUNNEL_DEVICE if active and _tunnel_present() else None
    state = "connected" if device else "connecting" if active else "disconnected"
    return {"installed": installed, "configured": configured, "auth_required": auth_required, "state": state,
            "route_device": device}


def _auth_required() -> bool:
    try:
        return AUTH_LINE in PROFILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False


def _credentials(request: dict) -> bytes:
    username, password = request.get("username"), request.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise ValueError("Nhập username và mật khẩu/OTP của VPN.")
    if not username or not password or len(username) > 256 or len(password) > 512:
        raise ValueError("Username hoặc mật khẩu/OTP VPN bị thiếu hoặc quá dài.")
    if any(char in username + password for char in ("\r", "\n", "\x00")):
        raise ValueError("Username hoặc mật khẩu/OTP VPN chứa ký tự không hợp lệ.")
    return (username + "\n" + password + "\n").encode("utf-8")


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".vpn-", dir=path.parent)
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


def _upload_profile(request: dict) -> dict:
    if _systemctl("is-active", "--quiet"):
        raise ValueError("Hãy ngắt VPN trước khi thay profile.")
    try:
        raw = base64.b64decode(request.get("profile", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("File .ovpn không hợp lệ.") from exc
    profile = validate_profile(raw)
    if not _systemctl("disable"):
        raise ValueError("Không thể tắt tự khởi động VPN trước khi thay profile.")
    CREDENTIALS.unlink(missing_ok=True)
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    _atomic_write(PROFILE, profile.encode("utf-8"), 0o600)
    return status()


def _connect(request: dict) -> dict:
    current = status()
    if not current["installed"] or not current["configured"]:
        raise ValueError("Chưa cài OpenVPN hoặc chưa tải profile hợp lệ.")
    if _systemctl("is-active", "--quiet"):
        raise ValueError("VPN đang chạy; hãy ngắt trước khi kết nối lại.")
    if _auth_required():
        content = _credentials(request)
        RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(RUNTIME_DIR, 0o700)
        _atomic_write(CREDENTIALS, content, 0o600)
        started = _systemctl("start")
        if not started:
            CREDENTIALS.unlink(missing_ok=True)
    else:
        started = _systemctl("enable", "--now")
    if not started:
        raise ValueError("Không khởi động được OpenVPN. Kiểm tra journalctl của service.")
    return status()


def _disconnect() -> dict:
    stopped = _systemctl("disable", "--now")
    CREDENTIALS.unlink(missing_ok=True)
    if not stopped:
        raise ValueError("Không thể ngắt OpenVPN. Kiểm tra systemd service.")
    return status()


def handle(request: dict) -> dict:
    action = request.get("action")
    if action == "status":
        return status()
    if action == "upload":
        return _upload_profile(request)
    if action == "connect":
        return _connect(request)
    if action == "disconnect":
        return _disconnect()
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
