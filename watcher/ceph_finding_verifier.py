"""Read-only live verification before a LogFinding may teach new code."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass

from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import Cluster, LogFinding, LogPattern
from watcher import ceph_client

_VAULT_RE = re.compile(r"vault|failed to retrieve actual key", re.IGNORECASE)
_TOKEN_PATH_RE = re.compile(r"Vault token file ['\"](?P<path>/[^'\"]+)['\"]", re.IGNORECASE)
_SAFE_TOKEN_PATH_RE = re.compile(r"^/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){1,30}$")
_SAFE_CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{12,64}$")


@dataclass(frozen=True)
class VerificationResult:
    code: str
    summary: str
    live_facts: tuple[str, ...]
    eligible_for_learning: bool


def _token_path(patterns: list[LogPattern]) -> str | None:
    for row in patterns:
        for text in (row.sample_line, row.template):
            match = _TOKEN_PATH_RE.search(text or "")
            if match and _SAFE_TOKEN_PATH_RE.fullmatch(match.group("path")):
                return match.group("path")
    return None


def _safe_token_path(value: object) -> str | None:
    path = str(value or "").strip()
    if ".." in path.split("/") or not _SAFE_TOKEN_PATH_RE.fullmatch(path):
        return None
    return path


def _ceph_vault_config(cluster: Cluster) -> tuple[list[dict], str | None]:
    """Read effective RGW Vault configuration from Ceph's config database."""
    ssh_user, ssh_key, exec_mode, container = resolve_ssh_creds(cluster)
    mons = [value.strip() for value in cluster.ceph_mon_nodes.split(",") if value.strip()]
    try:
        _host, payload = ceph_client.run_ceph_json_command_with(
            mons, container, ssh_user, ssh_key, exec_mode, "ceph config dump",
        )
    except Exception as exc:
        return [], " ".join(str(exc).split())[:500]
    if not isinstance(payload, list):
        return [], "ceph config dump returned a non-list response"
    rows = [
        row for row in payload
        if isinstance(row, dict)
        and str(row.get("section") or "").startswith("client.rgw")
        and "vault" in str(row.get("name") or "").lower()
    ]
    return rows, None


def _rgw_orch_daemons(cluster: Cluster) -> tuple[list[dict], str | None]:
    ssh_user, ssh_key, exec_mode, container = resolve_ssh_creds(cluster)
    mons = [value.strip() for value in cluster.ceph_mon_nodes.split(",") if value.strip()]
    try:
        _host, payload = ceph_client.run_ceph_json_command_with(
            mons, container, ssh_user, ssh_key, exec_mode,
            "ceph orch ps --daemon_type rgw",
        )
    except Exception as exc:
        return [], " ".join(str(exc).split())[:500]
    if not isinstance(payload, list):
        return [], "ceph orch ps returned a non-list response"
    return [
        row for row in payload
        if isinstance(row, dict)
        and row.get("daemon_type") == "rgw"
        and _SAFE_CONTAINER_ID_RE.fullmatch(str(row.get("container_id") or ""))
    ], None


def _is_vault_finding(finding: LogFinding, patterns: list[LogPattern]) -> bool:
    texts = [finding.title, finding.summary, finding.root_cause_hypothesis]
    texts.extend(value for row in patterns for value in (row.template, row.sample_line))
    return any(_VAULT_RE.search(value or "") for value in texts)


def _health(cluster: Cluster) -> tuple[str | None, str | None]:
    ssh_user, ssh_key, exec_mode, container = resolve_ssh_creds(cluster)
    mons = [value.strip() for value in cluster.ceph_mon_nodes.split(",") if value.strip()]
    try:
        payload = ceph_client.query_cluster_health_with(
            mons, container, ssh_user, ssh_key, exec_mode, update_sticky_fallback=False,
        )
        return str(payload.get("status") or "UNKNOWN"), None
    except Exception as exc:
        return None, " ".join(str(exc).split())[:500]


def _stat_token(
    host: str, path: str, cluster: Cluster, container_id: str | None = None,
) -> str:
    ssh_user, ssh_key, _exec_mode, _container = resolve_ssh_creds(cluster)
    quoted = shlex.quote(path)
    # Metadata only. Never cat, hash, print, or transmit token contents.
    probe = (
        f"if [ ! -e {quoted} ]; then echo MISSING; "
        f"else stat -c 'PRESENT mode=%a owner=%U:%G size=%s' -- {quoted}; fi"
    )
    if container_id:
        if not _SAFE_CONTAINER_ID_RE.fullmatch(container_id):
            return "UNSAFE_CONTAINER_ID"
        inner = shlex.quote(probe)
        command = (
            f"if command -v podman >/dev/null 2>&1; then podman exec {container_id} sh -c {inner}; "
            f"elif command -v docker >/dev/null 2>&1; then docker exec {container_id} sh -c {inner}; "
            "else echo CONTAINER_RUNTIME_MISSING; fi"
        )
    else:
        command = probe
    try:
        return ceph_client.run_command_on_node_with(host, command, ssh_user, ssh_key, timeout=5).strip()[:300]
    except Exception as exc:
        return "SSH_ERROR " + " ".join(str(exc).split())[:240]


def verify(finding: LogFinding, patterns: list[LogPattern], cluster: Cluster) -> VerificationResult:
    health, health_error = _health(cluster)
    if health_error:
        return VerificationResult(
            "CEPH_UNREACHABLE",
            "Không truy vấn được ceph health; chưa thể kết luận Vault là nguyên nhân gốc.",
            (f"ceph_health_error={health_error}",), False,
        )

    facts = [f"ceph_health={health}"]
    if not _is_vault_finding(finding, patterns):
        affected = set(_json_hosts(finding.affected_hosts_json))
        allowed = {row["host"] for row in configured_nodes(cluster)}
        if affected and not affected.issubset(allowed):
            return VerificationResult(
                "AFFECTED_HOST_UNVERIFIED", "Host trong finding không thuộc inventory cấu hình.",
                tuple(facts), False,
            )
        return VerificationResult(
            "VERIFIED_LIVE_CEPH", "Cụm phản hồi live và target finding nằm trong inventory.",
            tuple(facts), True,
        )

    log_path = _token_path(patterns)
    config_rows, config_error = _ceph_vault_config(cluster)
    if config_error:
        facts.append(f"ceph_config_dump_error={config_error}")
    token_configs = [
        (str(row.get("section")), str(row.get("name")), path)
        for row in config_rows
        if str(row.get("name") or "").endswith("vault_token_file")
        if (path := _safe_token_path(row.get("value"))) is not None
    ]
    paths = list(dict.fromkeys(([log_path] if log_path else []) + [row[2] for row in token_configs]))
    if not paths:
        return VerificationResult(
            "VAULT_TOKEN_PATH_UNKNOWN",
            "Không tìm thấy token path trong log hoặc ceph config dump.",
            tuple(facts), False,
        )
    for section, name, path in token_configs:
        facts.append(f"ceph_config[{section}].{name}={path}")
    affected = set(_json_hosts(finding.affected_hosts_json))
    rgw_hosts = {row["host"] for row in configured_nodes(cluster) if "RGW" in row["roles"]}
    targets = sorted((affected & rgw_hosts) or rgw_hosts)
    if not targets:
        return VerificationResult(
            "RGW_HOST_UNKNOWN", "Không có RGW host hợp lệ trong inventory để kiểm tra.",
            tuple(facts), False,
        )
    daemons, orch_error = _rgw_orch_daemons(cluster)
    if orch_error:
        facts.append(f"ceph_orch_ps_error={orch_error}")
    stats: dict[str, str] = {}
    if daemons:
        facts.append(f"rgw_deployment=cephadm containers={len(daemons)}")
        # Container IDs are host-local. Probe each ID on allowed RGW hosts;
        # only its owning host succeeds, without trusting hostname/IP aliases.
        for daemon in daemons:
            container_id = str(daemon["container_id"])
            daemon_name = str(daemon.get("daemon_name") or container_id)
            for host in targets:
                for path in paths:
                    value = _stat_token(host, path, cluster, container_id)
                    if not value.startswith("SSH_ERROR") and "no such container" not in value.lower():
                        stats[f"{host}/{daemon_name}:{path}"] = value
    else:
        facts.append(f"rgw_deployment={resolve_ssh_creds(cluster)[2]}")
        for host in targets:
            for path in paths:
                stats[f"{host}:{path}"] = _stat_token(host, path, cluster)
    facts.extend(f"token_metadata[{target}]={value}" for target, value in stats.items())
    if not stats:
        return VerificationResult(
            "VAULT_TOKEN_CHECK_PARTIAL",
            "Đã tìm thấy token path nhưng không xác định được RGW runtime để kiểm tra.",
            tuple(facts), False,
        )
    if all(value == "MISSING" for value in stats.values()):
        return VerificationResult(
            "VAULT_TOKEN_MISSING", "Token Vault thực sự không tồn tại trên các RGW host đã xác minh.",
            tuple(facts), False,
        )
    if any("size=0" in value for value in stats.values()):
        return VerificationResult(
            "VAULT_TOKEN_EMPTY", "Token file tồn tại nhưng rỗng trên ít nhất một RGW host.",
            tuple(facts), False,
        )
    if all(value.startswith("PRESENT") for value in stats.values()):
        return VerificationResult(
            "VAULT_AUTH_OR_KEY_LOOKUP_FAILURE",
            "Token file tồn tại; cần kiểm tra token hết hạn/policy, Vault key path hoặc quyền truy xuất key.",
            tuple(facts), False,
        )
    return VerificationResult(
        "VAULT_TOKEN_CHECK_PARTIAL", "Không xác minh được token metadata trên mọi RGW host.",
        tuple(facts), False,
    )


def _json_hosts(value: str | None) -> list[str]:
    import json
    try:
        parsed = json.loads(value or "[]")
        return [str(item) for item in parsed] if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []
