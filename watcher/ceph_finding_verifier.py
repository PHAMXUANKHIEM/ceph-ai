"""Read-only live verification before a LogFinding may teach new code."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import Cluster, LogFinding, LogPattern
from watcher import ceph_client

_VAULT_RE = re.compile(r"vault|failed to retrieve actual key", re.IGNORECASE)
_TOKEN_PATH_RE = re.compile(r"Vault token file ['\"](?P<path>/[^'\"]+)['\"]", re.IGNORECASE)
_SAFE_TOKEN_PATH_RE = re.compile(r"^/(?:etc|run|var/lib)/ceph/[A-Za-z0-9_./-]{1,240}$")


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


def _stat_token(host: str, path: str, cluster: Cluster) -> str:
    ssh_user, ssh_key, _exec_mode, _container = resolve_ssh_creds(cluster)
    quoted = shlex.quote(path)
    # Metadata only. Never cat, hash, print, or transmit token contents.
    command = (
        f"if [ ! -e {quoted} ]; then echo MISSING; "
        f"else stat -c 'PRESENT mode=%a owner=%U:%G size=%s' -- {quoted}; fi"
    )
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

    path = _token_path(patterns)
    if path is None:
        return VerificationResult(
            "VAULT_TOKEN_PATH_UNKNOWN",
            "Log Vault không cung cấp token path hợp lệ để kiểm tra metadata an toàn.",
            tuple(facts), False,
        )
    facts.append(f"vault_token_path={path}")
    affected = set(_json_hosts(finding.affected_hosts_json))
    rgw_hosts = {row["host"] for row in configured_nodes(cluster) if "RGW" in row["roles"]}
    targets = sorted((affected & rgw_hosts) or rgw_hosts)
    if not targets:
        return VerificationResult(
            "RGW_HOST_UNKNOWN", "Không có RGW host hợp lệ trong inventory để kiểm tra.",
            tuple(facts), False,
        )
    stats = {host: _stat_token(host, path, cluster) for host in targets}
    facts.extend(f"token_metadata[{host}]={value}" for host, value in stats.items())
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
