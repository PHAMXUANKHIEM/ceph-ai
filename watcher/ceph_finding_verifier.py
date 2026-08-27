"""Read-only live verification before a LogFinding may teach new code."""

from __future__ import annotations

import base64
import binascii
import json
import re
import shlex
from dataclasses import dataclass
from urllib.parse import urlparse

from shared import db
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import Cluster, LogFinding, LogPattern, RgwAccessAuditEvent
from watcher import ceph_client

_VAULT_RE = re.compile(r"vault|failed to retrieve actual key", re.IGNORECASE)
_DEFAULT_KEY_RE = re.compile(r"rgw[ _]crypt[ _]default[ _]encryption[ _]key|default encryption key", re.IGNORECASE)
_TOKEN_PATH_RE = re.compile(r"Vault token file ['\"](?P<path>/[^'\"]+)['\"]", re.IGNORECASE)
_SAFE_TOKEN_PATH_RE = re.compile(r"^/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){1,30}$")
_SAFE_CONTAINER_ID_RE = re.compile(r"^[a-f0-9]{12,64}$")
_SAFE_RGW_SECTION_RE = re.compile(r"^client\.rgw\.[A-Za-z0-9_.-]{1,180}$")
_SAFE_RGW_DAEMON_RE = re.compile(r"^rgw\.[A-Za-z0-9_.-]{1,180}$")
_VAULT_BACKEND_OPTIONS = {"rgw_crypt_sse_s3_backend", "rgw_crypt_s3_kms_backend"}


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


def _ceph_config_dump(cluster: Cluster) -> tuple[list[dict], str | None]:
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
    return [row for row in payload if isinstance(row, dict)], None


def _ceph_vault_config(cluster: Cluster) -> tuple[list[dict], str | None]:
    """Read effective RGW Vault configuration from Ceph's config database."""
    payload, error = _ceph_config_dump(cluster)
    if error:
        return [], error
    rows = [
        row for row in payload
        if str(row.get("section") or "").startswith("client.rgw")
        and (
            "vault" in str(row.get("name") or "").lower()
            or str(row.get("name") or "").lower() in _VAULT_BACKEND_OPTIONS
        )
    ]
    return rows, None


def _vault_backend_enabled(rows: list[dict]) -> bool:
    """Return whether RGW currently has a Vault encryption backend enabled.

    Token/address options can remain in Ceph's config database after an
    operator retires Vault. They are not evidence that RGW still uses Vault;
    the backend selector is authoritative for deciding whether an old Vault
    finding is still actionable.
    """
    return any(
        str(row.get("name") or "").strip().lower() in _VAULT_BACKEND_OPTIONS
        and str(row.get("value") or "").strip().lower() == "vault"
        for row in rows
    )


def _is_default_key_finding(finding: LogFinding, patterns: list[LogPattern]) -> bool:
    texts = [finding.title, finding.summary, finding.root_cause_hypothesis]
    texts.extend(value for row in patterns for value in (row.template, row.sample_line))
    return any(_DEFAULT_KEY_RE.search(value or "") for value in texts)


def _default_key_config_status(cluster: Cluster) -> tuple[str, tuple[str, ...]]:
    rows, error = _ceph_config_dump(cluster)
    if error:
        return "UNREACHABLE", (f"ceph_config_dump_error={error}",)
    matches = [
        row for row in rows
        if str(row.get("section") or "").startswith("client.rgw")
        and str(row.get("name") or "") == "rgw_crypt_default_encryption_key"
    ]
    if not matches:
        return "UNSET", ("rgw_default_key_status=UNSET",)
    facts = []
    valid = True
    for row in matches:
        value = str(row.get("value") or "").strip()
        try:
            decoded = base64.b64decode(value, validate=True)
            row_valid = len(decoded) == 32
        except (binascii.Error, ValueError):
            row_valid = False
        valid = valid and row_valid
        facts.append(
            f"rgw_default_key_status[section={row.get('section')}]="
            f"{'VALID_BASE64_256' if row_valid else 'INVALID_BASE64_OR_LENGTH'}"
        )
    return ("VALID" if valid else "INVALID"), tuple(facts)


def default_key_vault_remediation_candidate(cluster: Cluster) -> dict | None:
    """Return closed, non-secret params only when removing the key is safe.

    The proposal is deliberately deterministic: exact RGW section, invalid
    default key, and Vault SSE-S3 backend must all be present in live config.
    """
    rows, error = _ceph_config_dump(cluster)
    if error:
        return None
    by_section: dict[str, dict[str, str]] = {}
    for row in rows:
        section = str(row.get("section") or "")
        name = str(row.get("name") or "")
        if _SAFE_RGW_SECTION_RE.fullmatch(section):
            by_section.setdefault(section, {})[name] = str(row.get("value") or "").strip()
    candidates = []
    for section, values in by_section.items():
        raw_key = values.get("rgw_crypt_default_encryption_key")
        if raw_key is None or values.get("rgw_crypt_sse_s3_backend", "").lower() != "vault":
            continue
        try:
            key_valid = len(base64.b64decode(raw_key, validate=True)) == 32
        except (binascii.Error, ValueError):
            key_valid = False
        if not key_valid:
            candidates.append(section)
    if len(candidates) != 1:
        return None
    daemons, daemon_error = _rgw_orch_daemons(cluster)
    if daemon_error:
        return None
    daemon_names = sorted({
        str(row.get("daemon_name") or "") for row in daemons
        if _SAFE_RGW_DAEMON_RE.fullmatch(str(row.get("daemon_name") or ""))
    })
    if not daemon_names:
        return None
    return {"section": candidates[0], "daemon_names": daemon_names}


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


def _safe_vault_addr(value: object) -> str | None:
    addr = str(value or "").strip().rstrip("/")
    parsed = urlparse(addr)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
        return None
    return addr


def _probe_vault_token(
    host: str, path: str, addr: str, cluster: Cluster, container_id: str | None = None,
) -> str:
    """Validate Vault reachability and token without printing token contents."""
    safe_path = _safe_token_path(path)
    safe_addr = _safe_vault_addr(addr)
    if safe_path is None or safe_addr is None:
        return "UNSAFE_VAULT_CONFIG"
    probe = (
        f"test -s {shlex.quote(safe_path)} || {{ echo TOKEN_MISSING_OR_EMPTY; exit 0; }}; "
        f"h=$(curl -sS -o /dev/null -w '%{{http_code}}' --connect-timeout 3 --max-time 5 "
        f"{shlex.quote(safe_addr + '/v1/sys/health')} || echo 000); "
        f"t=$(curl -sS -o /dev/null -w '%{{http_code}}' --connect-timeout 3 --max-time 5 "
        f"-H \"X-Vault-Token: $(cat {shlex.quote(safe_path)})\" "
        f"{shlex.quote(safe_addr + '/v1/auth/token/lookup-self')} || echo 000); "
        "echo VAULT_HEALTH_HTTP=$h TOKEN_LOOKUP_HTTP=$t"
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
    ssh_user, ssh_key, _exec_mode, _container = resolve_ssh_creds(cluster)
    try:
        return ceph_client.run_command_on_node_with(
            host, command, ssh_user, ssh_key, timeout=12,
        ).strip()[:300]
    except Exception as exc:
        return "SSH_ERROR " + " ".join(str(exc).split())[:240]


def _functional_rgw_recovery(
    finding: LogFinding, patterns: list[LogPattern], *, evidence_re: re.Pattern[str] = _VAULT_RE,
) -> tuple[str | None, tuple[str, ...]]:
    """Correlate a successful encrypted request after the latest error evidence."""
    cluster_id = getattr(finding, "cluster_id", None)
    evidence_times = [
        row.last_seen_at for row in patterns
        if getattr(row, "last_seen_at", None)
        and any(evidence_re.search(value or "") for value in (row.template, row.sample_line))
    ]
    if not cluster_id or not evidence_times:
        return None, ()
    latest_error_at = max(evidence_times)
    affected = _json_hosts(finding.affected_hosts_json)
    with db.SessionLocal() as session:
        query = (
            session.query(RgwAccessAuditEvent)
            .filter(RgwAccessAuditEvent.cluster_id == cluster_id)
            .filter(RgwAccessAuditEvent.event_at > latest_error_at)
            .filter(RgwAccessAuditEvent.http_status >= 200)
            .filter(RgwAccessAuditEvent.http_status < 300)
            .filter(RgwAccessAuditEvent.encryption.isnot(None))
            .filter(RgwAccessAuditEvent.encryption != "Plaintext")
        )
        if affected:
            query = query.filter(RgwAccessAuditEvent.rgw_host.in_(affected))
        event = query.order_by(RgwAccessAuditEvent.event_at.desc()).first()
    if event is None:
        return None, ()
    encryption = str(event.encryption or "").lower()
    family = "sse_s3" if "sse-s3" in encryption else ("kms" if "kms" in encryption else None)
    facts = (
        f"functional_request={event.method} {event.http_status} encryption={event.encryption}",
        f"functional_request_at={event.event_at.isoformat()} host={event.rgw_host}",
    )
    return family, facts


def verify_vault_recovery(
    finding: LogFinding, patterns: list[LogPattern], cluster: Cluster,
) -> VerificationResult:
    """Require live Ceph, runtime token metadata and Vault token lookup."""
    if _is_default_key_finding(finding, patterns):
        health, health_error = _health(cluster)
        status, config_facts = _default_key_config_status(cluster)
        facts = (f"ceph_health={health or 'ERROR'}",) + config_facts
        if health_error or status in {"UNREACHABLE", "INVALID"}:
            return VerificationResult(
                "RGW_DEFAULT_ENCRYPTION_KEY_INVALID",
                "rgw_crypt_default_encryption_key vẫn không phải khóa base64 256-bit hợp lệ.",
                facts, False,
            )
        if status == "UNSET":
            _family, functional_facts = _functional_rgw_recovery(
                finding, patterns, evidence_re=_DEFAULT_KEY_RE,
            )
            if functional_facts:
                return VerificationResult(
                    "RGW_DEFAULT_ENCRYPTION_KEY_RECOVERY_VERIFIED",
                    "Cấu hình khóa sai đã được gỡ và có request mã hóa thành công sau lỗi.",
                    facts + functional_facts, True,
                )
            return VerificationResult(
                "RGW_DEFAULT_ENCRYPTION_KEY_REMOVED_AWAITING_IO",
                "Đã gỡ cấu hình khóa sai; đang chờ PUT/GET mã hóa thành công để xác nhận.",
                facts, False,
            )
        _family, functional_facts = _functional_rgw_recovery(
            finding, patterns, evidence_re=_DEFAULT_KEY_RE,
        )
        if functional_facts:
            return VerificationResult(
                "RGW_DEFAULT_ENCRYPTION_KEY_RECOVERY_VERIFIED",
                "Khóa mặc định hợp lệ và có request mã hóa thành công sau lỗi.",
                facts + functional_facts, True,
            )
        return VerificationResult(
            "RGW_DEFAULT_ENCRYPTION_KEY_RECOVERY_UNVERIFIED",
            "Khóa đã hợp lệ nhưng chưa có request mã hóa thành công sau lỗi.", facts, False,
        )
    if not _is_vault_finding(finding, patterns):
        return VerificationResult("NOT_VAULT", "Không cần Vault recovery gate.", (), True)
    health, health_error = _health(cluster)
    if health_error or health not in {"HEALTH_OK", "HEALTH_WARN"}:
        return VerificationResult(
            "CEPH_RECOVERY_UNVERIFIED", "Ceph chưa phản hồi ổn định để xác nhận phục hồi.",
            (f"ceph_health={health or 'ERROR'}",), False,
        )
    facts = [f"ceph_health={health}"]
    backend_family, functional_facts = _functional_rgw_recovery(finding, patterns)
    facts.extend(functional_facts)
    config_rows, config_error = _ceph_vault_config(cluster)
    if config_error:
        return VerificationResult(
            "VAULT_CONFIG_UNREACHABLE", "Không đọc được ceph config dump.",
            tuple(facts + [f"ceph_config_dump_error={config_error}"]), False,
        )
    if not _vault_backend_enabled(config_rows):
        return VerificationResult(
            "VAULT_NOT_CONFIGURED",
            "Vault không còn được bật trong cấu hình RGW; finding Vault lịch sử không còn áp dụng.",
            tuple(facts + ["rgw_vault_backend=DISABLED"]), True,
        )
    pairs = []
    by_section_name = {
        (str(row.get("section")), str(row.get("name"))): row.get("value")
        for row in config_rows
    }
    for row in config_rows:
        name = str(row.get("name") or "")
        if not name.endswith("vault_token_file"):
            continue
        if backend_family == "sse_s3" and "_sse_s3_vault_" not in name:
            continue
        if backend_family == "kms" and "_sse_s3_vault_" in name:
            continue
        section = str(row.get("section") or "")
        path = _safe_token_path(row.get("value"))
        addr = _safe_vault_addr(by_section_name.get((section, name[:-10] + "addr")))
        if path and addr:
            pairs.append((section, name, path, addr))
    if not pairs:
        return VerificationResult(
            "VAULT_RECOVERY_CONFIG_INCOMPLETE",
            "Không ghép được Vault token_file với vault_addr trong ceph config dump.",
            tuple(facts), False,
        )
    daemons, orch_error = _rgw_orch_daemons(cluster)
    affected = set(_json_hosts(finding.affected_hosts_json))
    rgw_hosts = {row["host"] for row in configured_nodes(cluster) if "RGW" in row["roles"]}
    targets = sorted((affected & rgw_hosts) or rgw_hosts)
    outcomes: dict[str, str] = {}
    if daemons:
        facts.append(f"rgw_deployment=cephadm containers={len(daemons)}")
        for daemon in daemons:
            container_id = str(daemon["container_id"])
            daemon_name = str(daemon.get("daemon_name") or container_id)
            for host in targets:
                for section, name, path, addr in pairs:
                    value = _probe_vault_token(host, path, addr, cluster, container_id)
                    if not value.startswith("SSH_ERROR"):
                        outcomes[f"{host}/{daemon_name}:{name}"] = value
    else:
        facts.append(f"ceph_orch_ps_error={orch_error or 'no RGW daemons'}")
        for host in targets:
            for section, name, path, addr in pairs:
                outcomes[f"{host}:{name}"] = _probe_vault_token(host, path, addr, cluster)
    facts.extend(f"vault_probe[{target}]={value}" for target, value in outcomes.items())
    accepted_health = {"200", "429", "472", "473"}
    healthy = []
    for value in outcomes.values():
        health_match = re.search(r"VAULT_HEALTH_HTTP=(\d{3})", value)
        token_match = re.search(r"TOKEN_LOOKUP_HTTP=(\d{3})", value)
        healthy.append(
            bool(health_match and health_match.group(1) in accepted_health)
            and bool(token_match and token_match.group(1) == "200")
        )
    if outcomes and all(healthy):
        return VerificationResult(
            "VAULT_RECOVERY_VERIFIED",
            "Ceph ổn định; mọi Vault endpoint và token RGW đã xác thực live thành công.",
            tuple(facts), True,
        )
    return VerificationResult(
        "VAULT_RECOVERY_UNVERIFIED",
        "Log đã ngừng nhưng Vault endpoint/token chưa vượt qua kiểm tra live; giữ finding OPEN.",
        tuple(facts), False,
    )


def verify(finding: LogFinding, patterns: list[LogPattern], cluster: Cluster) -> VerificationResult:
    health, health_error = _health(cluster)
    if health_error:
        return VerificationResult(
            "CEPH_UNREACHABLE",
            "Không truy vấn được ceph health; chưa thể kết luận Vault là nguyên nhân gốc.",
            (f"ceph_health_error={health_error}",), False,
        )

    facts = [f"ceph_health={health}"]
    if _is_default_key_finding(finding, patterns):
        status, config_facts = _default_key_config_status(cluster)
        facts.extend(config_facts)
        if status == "INVALID":
            return VerificationResult(
                "RGW_DEFAULT_ENCRYPTION_KEY_INVALID",
                "Cấu hình đang dùng tên thuật toán/giá trị sai thay vì khóa base64 256-bit.",
                tuple(facts), False,
            )
        if status == "VALID":
            return VerificationResult(
                "RGW_DEFAULT_ENCRYPTION_KEY_VALID",
                "Default encryption key trong Ceph config là base64 256-bit hợp lệ.",
                tuple(facts), False,
            )
        return VerificationResult(
            "RGW_DEFAULT_ENCRYPTION_KEY_UNSET",
            "Không còn cấu hình default encryption key trong Ceph config.", tuple(facts), False,
        )
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
    if not config_error and not _vault_backend_enabled(config_rows):
        return VerificationResult(
            "VAULT_NOT_CONFIGURED",
            "Vault không còn được bật trong cấu hình RGW; finding lịch sử được coi là ngoài phạm vi.",
            tuple(facts + ["rgw_vault_backend=DISABLED"]), True,
        )
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
