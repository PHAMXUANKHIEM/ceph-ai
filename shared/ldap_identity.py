"""Safe LDAP/Active Directory connection and group lookup adapter."""

from __future__ import annotations

import json
import os
import re
import ssl
import stat
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from ldap3 import ALL, SIMPLE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars


class DirectoryValidationError(RuntimeError):
    """A directory check failed without exposing bind credentials."""


_ENV_REF = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]{0,127}$")
_VAULT_REF = re.compile(r"^vault:(?P<path>[A-Za-z0-9][A-Za-z0-9_./:@-]{0,511})#(?P<field>[A-Za-z0-9_.-]{1,128})$")
_ALLOWED_SECRET_ROOTS = (
    Path("/etc/ceph-ai-secrets"),
    Path("/run/secrets"),
    Path("/var/lib/ceph-ai/secrets"),
)


def resolve_secret(secret_ref: str) -> str:
    """Resolve only explicit env/file references; never fetch arbitrary paths."""
    reference = str(secret_ref or "").strip()
    if _ENV_REF.fullmatch(reference):
        value = os.environ.get(reference[4:], "")
        if not value:
            raise DirectoryValidationError("secret_ref env variable chưa được cấu hình")
        return value
    if reference.startswith("file:"):
        candidate = Path(reference[5:]).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise DirectoryValidationError("secret_ref file không tồn tại") from exc
        if not any(resolved == root or root in resolved.parents for root in _ALLOWED_SECRET_ROOTS):
            raise DirectoryValidationError("secret_ref file nằm ngoài thư mục secret được cho phép")
        try:
            mode = resolved.stat().st_mode
            if not stat.S_ISREG(mode) or mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise DirectoryValidationError("secret_ref file phải là regular file và không writable bởi group/other")
            value = resolved.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise DirectoryValidationError("không đọc được secret_ref file") from exc
        if not value:
            raise DirectoryValidationError("secret_ref file đang rỗng")
        return value
    if reference.startswith("vault:"):
        match = _VAULT_REF.fullmatch(reference)
        if not match:
            raise DirectoryValidationError("vault: secret_ref phải có dạng vault:path#field")
        vault_addr = (os.environ.get("VAULT_ADDR") or os.environ.get("CEPH_AI_VAULT_ADDR") or "").strip()
        if not vault_addr:
            raise DirectoryValidationError("Vault chưa được cấu hình qua VAULT_ADDR")
        parsed = urlsplit(vault_addr)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise DirectoryValidationError("VAULT_ADDR phải là HTTPS URL hợp lệ")
        token = os.environ.get("CEPH_AI_VAULT_TOKEN", "").strip()
        if not token:
            raise DirectoryValidationError("Vault token chưa được cấu hình qua CEPH_AI_VAULT_TOKEN")
        endpoint = vault_addr.rstrip("/") + "/v1/" + quote(match.group("path"), safe="/-_.:@")
        request = Request(endpoint, headers={"Accept": "application/json", "X-Vault-Token": token})
        try:
            tls_context = ssl.create_default_context()
            with urlopen(request, timeout=5, context=tls_context) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise DirectoryValidationError(f"Vault secret lookup thất bại (HTTP {exc.code})") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise DirectoryValidationError(f"Vault secret lookup thất bại: {type(exc).__name__}") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        if not isinstance(data, dict):
            raise DirectoryValidationError("Vault response không có data object")
        value = data.get(match.group("field"))
        if not isinstance(value, str) or not value.strip():
            raise DirectoryValidationError("Vault field không tồn tại hoặc đang rỗng")
        return value.strip()
    raise DirectoryValidationError("secret_ref phải có dạng env:VAR hoặc file:/đường/dẫn")


def _config(config_json: str | None) -> dict:
    try:
        value = json.loads(config_json or "{}")
    except (TypeError, ValueError) as exc:
        raise DirectoryValidationError("LDAP config JSON không hợp lệ") from exc
    if not isinstance(value, dict):
        raise DirectoryValidationError("LDAP config phải là JSON object")
    return value


def _bool_config(config: dict, key: str, default: bool) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise DirectoryValidationError(f"LDAP config {key} phải là boolean")
    return value


def _connection(provider_type: str, endpoint: str, secret_ref: str | None, config: dict):
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"ldap", "ldaps"} or not parsed.hostname:
        raise DirectoryValidationError("LDAP/AD endpoint không hợp lệ")
    bind_dn = str(config.get("bind_dn") or "").strip()
    if not bind_dn:
        raise DirectoryValidationError("LDAP/AD config thiếu bind_dn")
    password = resolve_secret(secret_ref or "")
    use_ssl = parsed.scheme == "ldaps"
    verify_tls = _bool_config(config, "tls_verify", True)
    tls = Tls(validate=2 if verify_tls else 0) if use_ssl else None
    server = Server(
        parsed.hostname,
        port=parsed.port or (636 if use_ssl else 389),
        use_ssl=use_ssl,
        tls=tls,
        get_info=ALL,
        connect_timeout=min(max(int(config.get("timeout_seconds", 5)), 1), 30),
    )
    connection = Connection(server, user=bind_dn, password=password, authentication=SIMPLE, auto_bind=False)
    try:
        if config.get("start_tls"):
            if use_ssl:
                raise DirectoryValidationError("start_tls không được dùng cùng ldaps")
            if not connection.open() or not connection.start_tls():
                raise DirectoryValidationError("LDAP StartTLS thất bại")
        if not connection.bind():
            message = str(connection.result.get("message") or "LDAP bind thất bại")
            raise DirectoryValidationError(f"LDAP/AD bind thất bại: {message[:240]}")
        return connection, server
    except (LDAPException, OSError) as exc:
        try:
            connection.unbind()
        except Exception:
            pass
        if isinstance(exc, DirectoryValidationError):
            raise
        raise DirectoryValidationError(f"LDAP/AD connection thất bại: {type(exc).__name__}") from exc


def _entry_value(entry, attribute: str):
    value = entry.entry_attributes_as_dict.get(attribute)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def validate_directory_provider(
    provider_type: str,
    endpoint: str,
    secret_ref: str | None,
    config_json: str | None,
) -> dict:
    """Bind and optionally resolve one user's groups; never return secrets."""
    if provider_type not in {"ldap", "ad"}:
        raise DirectoryValidationError("directory adapter chỉ hỗ trợ ldap hoặc ad")
    config = _config(config_json)
    connection, server = _connection(provider_type, endpoint, secret_ref, config)
    try:
        evidence = {
            "kind": "directory_bind",
            "provider_type": provider_type,
            "host": server.host,
            "port": server.port,
            "tls": bool(server.ssl),
            "bind_verified": True,
        }
        username = str(config.get("probe_username") or "").strip()
        if not username:
            return evidence
        base_dn = str(config.get("base_dn") or "").strip()
        if not base_dn:
            raise DirectoryValidationError("LDAP config thiếu base_dn cho group lookup")
        escaped_username = escape_filter_chars(username)
        user_filter = str(config.get("user_filter") or "(&(objectClass=person)(uid={username}))")
        user_filter = user_filter.format(username=escaped_username, user_dn="")
        user_attribute = str(config.get("user_attribute") or "uid")
        if not connection.search(base_dn, user_filter, search_scope=SUBTREE, attributes=[user_attribute]):
            raise DirectoryValidationError("LDAP/AD không tìm thấy probe_username")
        if not connection.entries:
            raise DirectoryValidationError("LDAP/AD trả về user rỗng")
        user_entry = connection.entries[0]
        user_dn = str(user_entry.entry_dn)
        group_filter = str(config.get("group_filter") or "(&(objectClass=group)(member={user_dn}))")
        group_filter = group_filter.format(username=escaped_username, user_dn=escape_filter_chars(user_dn))
        group_attribute = str(config.get("group_attribute") or "cn")
        connection.search(base_dn, group_filter, search_scope=SUBTREE, attributes=[group_attribute])
        groups = []
        for entry in connection.entries:
            value = _entry_value(entry, group_attribute)
            if value is not None:
                groups.extend(str(item) for item in value) if isinstance(value, list) else groups.append(str(value))
        evidence.update({"user_lookup_verified": True, "user_dn": user_dn, "group_count": len(groups), "groups": sorted(set(groups))[:100]})
        return evidence
    finally:
        try:
            connection.unbind()
        except Exception:
            pass
