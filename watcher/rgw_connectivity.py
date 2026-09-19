"""Bounded, read-only RGW endpoint connectivity probes."""

from __future__ import annotations

import socket
import ssl
from urllib.parse import urlparse


PROBE_TIMEOUT_SECONDS = 3


def probe_endpoint(endpoint: str, *, allowed_hosts: set[str] | None = None) -> dict:
    parsed = urlparse(str(endpoint or "").strip())
    host = parsed.hostname
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == "https" else 80 if scheme == "http" else None)
    result = {
        "endpoint": endpoint,
        "status": "not_available",
        "scheme": scheme or None,
        "host": host,
        "port": port,
        "dns": "not_attempted",
        "tcp": "not_attempted",
        "tls": "not_attempted",
        "error": None,
        "read_only": True,
    }
    if scheme not in {"http", "https"} or not host or port is None:
        result["error"] = "Endpoint phải là http(s) với host hợp lệ."
        return result
    if allowed_hosts is not None and host not in allowed_hosts:
        result["error"] = "Endpoint host không nằm trong danh sách RGW đã cấu hình."
        return result
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        result["dns"] = "ok" if addresses else "failed"
    except (OSError, socket.gaierror) as exc:
        result["dns"] = "failed"
        result["error"] = f"DNS lookup failed: {type(exc).__name__}"
        return result
    try:
        raw_socket = socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SECONDS)
        try:
            result["tcp"] = "ok"
            if scheme == "https":
                context = ssl.create_default_context()
                with context.wrap_socket(raw_socket, server_hostname=host):
                    result["tls"] = "ok"
            else:
                result["tls"] = "not_applicable"
        finally:
            raw_socket.close()
    except (OSError, ssl.SSLError) as exc:
        result["tcp"] = "failed"
        result["error"] = f"Endpoint connection failed: {type(exc).__name__}"
        return result
    result["status"] = "ok" if result["tls"] in {"ok", "not_applicable"} else "failed"
    return result
