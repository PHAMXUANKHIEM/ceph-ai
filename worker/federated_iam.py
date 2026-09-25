"""Worker-side reconciliation for RGW federated role mappings.

The Dashboard only creates a confirmed registry row.  This module is called
from the existing Worker approval-poll thread and is the only place that may
execute ``radosgw-admin`` for this feature.  It fails closed when no RGW node
or unsupported provider is configured.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from urllib.parse import urlsplit

from config.settings import settings
from shared import db
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import (
    Cluster,
    RgwFederatedIdentityAudit,
    RgwFederatedIdentityProvider,
    RgwFederatedRoleMapping,
)
from shared.time import utc_now
from worker.executor.commands import wrap_ceph_runtime_command
from worker.executor.ssh_executor import ExecutorError, execute_command

logger = logging.getLogger(__name__)

_SAFE_ROLE_NAME = re.compile(r"^ceph-ai-[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}$")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _role_name(mapping: RgwFederatedRoleMapping) -> str:
    role_name = f"ceph-ai-{mapping.name}"
    if not _SAFE_ROLE_NAME.fullmatch(role_name):
        raise RuntimeError("role mapping name không tạo được RGW role name an toàn")
    return role_name


def _trust_policy(
    provider: RgwFederatedIdentityProvider, mapping: RgwFederatedRoleMapping
) -> dict:
    if provider.provider_type != "oidc" or not provider.issuer_url:
        raise RuntimeError("RGW role reconciliation hiện chỉ hỗ trợ provider OIDC")
    parsed = urlsplit(provider.issuer_url)
    issuer_id = f"{parsed.netloc}{parsed.path}".strip("/")
    if not issuer_id:
        raise RuntimeError("OIDC issuer không có hostname hợp lệ")
    condition_key = f"{issuer_id}:{mapping.source_key}"
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Federated": f"arn:aws:iam:::oidc-provider/{issuer_id}"},
            "Action": ["sts:AssumeRoleWithWebIdentity"],
            "Condition": {"StringLike": {condition_key: mapping.match_value}},
        }],
    }


def build_reconcile_command(
    provider: RgwFederatedIdentityProvider,
    mapping: RgwFederatedRoleMapping,
    *,
    exec_mode: str,
    container_name: str = "",
) -> tuple[str, str, str]:
    """Build idempotent OIDC-provider, role and permission-policy commands.

    Returns ``(wrapped_command, role_name, policy_name)``.  JSON documents are
    shell-quoted as one argument and are never interpolated as shell syntax.
    """
    role_name = _role_name(mapping)
    policy_name = f"ceph-ai-{mapping.name}"
    policy = json.loads(mapping.policy_json or "{}")
    trust = _trust_policy(provider, mapping)
    provider_url = shlex.quote(provider.issuer_url or "")
    role_arg = shlex.quote(role_name)
    policy_arg = shlex.quote(policy_name)
    trust_arg = shlex.quote(_canonical_json(trust))
    permission_arg = shlex.quote(_canonical_json(policy))

    ensure_oidc = (
        f"radosgw-admin oidc-provider get --provider-url={provider_url} >/dev/null 2>&1"
        f" || radosgw-admin oidc-provider create --provider-url={provider_url}"
    )
    if provider.audience:
        ensure_oidc += f" --client-ids={shlex.quote(provider.audience)}"
    ensure_role = (
        f"radosgw-admin role get --role-name={role_arg} >/dev/null 2>&1"
        f" || radosgw-admin role create --role-name={role_arg} --path=/ceph-ai/"
        f" --assume-role-policy-doc={trust_arg}"
    )
    update_trust = (
        f"radosgw-admin role modify --role-name={role_arg}"
        f" --assume-role-policy-doc={trust_arg}"
    )
    put_policy = (
        f"radosgw-admin role-policy put --role-name={role_arg}"
        f" --policy-name={policy_arg} --policy-doc={permission_arg}"
    )
    verify_policy = (
        f"radosgw-admin role-policy get --role-name={role_arg}"
        f" --policy-name={policy_arg} --format=json"
    )
    # The role modify call makes retries converge when the role already exists.
    inner = " && ".join((f"({ensure_oidc})", f"({ensure_role})", update_trust, put_policy, verify_policy))
    return wrap_ceph_runtime_command(inner, exec_mode=exec_mode, container_name=container_name), role_name, policy_name


def _parse_verified_policy(raw: str) -> object:
    value: object = json.loads(raw)
    if isinstance(value, dict) and isinstance(value.get("policy"), str):
        value = json.loads(value["policy"])
    return value


def _audit(session, *, provider_id: str, action: str, result: str, evidence: dict, error: str | None = None) -> None:
    session.add(RgwFederatedIdentityAudit(
        provider_id=provider_id,
        actor="worker:federated-iam",
        action=action,
        request_id=None,
        result=result,
        evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        error_message=error,
    ))


def _mark_failure(mapping_id: str, provider_id: str, error: str, *, blocked: bool = False) -> None:
    with db.SessionLocal() as session:
        mapping = session.get(RgwFederatedRoleMapping, mapping_id)
        if mapping is None:
            return
        mapping.reconcile_attempts += 1
        mapping.last_reconcile_error = error[:1000]
        mapping.status = "BLOCKED" if blocked else "RECONCILE_FAILED"
        mapping.updated_at = utc_now()
        _audit(
            session, provider_id=provider_id, action="role_mapping_reconcile",
            result="blocked" if blocked else "failed",
            evidence={"mapping_id": mapping_id, "attempt": mapping.reconcile_attempts},
            error=error[:1000],
        )
        session.commit()


def _reconcile_one(mapping_id: str) -> str:
    with db.SessionLocal() as session:
        mapping = session.get(RgwFederatedRoleMapping, mapping_id)
        if mapping is None or mapping.status != "REGISTERED":
            return "skipped"
        provider = session.get(RgwFederatedIdentityProvider, mapping.provider_id)
        cluster = session.query(Cluster).filter(Cluster.is_default.is_(True)).one_or_none()
        if provider is None or provider.status != "APPLIED" or not provider.enabled:
            error = "Provider chưa ở trạng thái APPLIED"
            provider_id = mapping.provider_id
            # Release the session before opening a separate audit transaction.
            session.rollback()
        elif provider.provider_type != "oidc":
            error = "RGW role reconciliation hiện chỉ hỗ trợ OIDC; LDAP/AD cần adapter riêng"
            provider_id = provider.id
            session.rollback()
        elif cluster is None:
            error = "Chưa có default Ceph cluster để reconcile RGW role"
            provider_id = provider.id
            session.rollback()
        else:
            rgw_nodes = [item["host"] for item in configured_nodes(cluster) if "RGW" in item["roles"]]
            if not rgw_nodes:
                error = "Cluster chưa cấu hình RGW node"
                provider_id = provider.id
                session.rollback()
            else:
                try:
                    ssh_user, ssh_key, exec_mode, container_name = resolve_ssh_creds(cluster)
                    if exec_mode in {"docker", "podman"}:
                        container_name = cluster.ceph_rgw_container_name or container_name
                    command, role_name, policy_name = build_reconcile_command(
                        provider, mapping, exec_mode=exec_mode, container_name=container_name,
                    )
                    output = execute_command(rgw_nodes[0], command, user=ssh_user, key_path=ssh_key)
                    verified = _parse_verified_policy(output)
                    expected = json.loads(mapping.policy_json or "{}")
                    if _canonical_json(verified) != _canonical_json(expected):
                        raise RuntimeError("Post-check policy RGW không khớp policy registry")
                    mapping.status = "RECONCILED"
                    mapping.enabled = True
                    mapping.rgw_role_name = role_name
                    mapping.reconcile_attempts += 1
                    mapping.last_reconciled_at = utc_now()
                    mapping.last_reconcile_error = None
                    mapping.updated_at = utc_now()
                    _audit(
                        session, provider_id=provider.id, action="role_mapping_reconcile",
                        result="succeeded",
                        evidence={"mapping_id": mapping.id, "role_name": role_name,
                                  "policy_name": policy_name, "rgw_host": rgw_nodes[0]},
                    )
                    session.commit()
                    return "reconciled"
                except Exception as exc:
                    error = f"{type(exc).__name__}: {str(exc).replace(chr(10), ' ')[:900]}"
                    provider_id = provider.id
                    session.rollback()
                    blocked = isinstance(exc, ExecutorError) and "no supported" in str(exc).lower()
                    _mark_failure(mapping_id, provider_id, error, blocked=blocked)
                    return "failed"
    _mark_failure(mapping_id, provider_id, error, blocked=True)
    return "blocked"


def reconcile_registered_mappings_once(limit: int = 10) -> int:
    """Reconcile a bounded batch; called by the existing Worker poll loop."""
    with db.SessionLocal() as session:
        ids = [row.id for row in session.query(RgwFederatedRoleMapping).filter_by(
            status="REGISTERED"
        ).order_by(RgwFederatedRoleMapping.created_at.asc()).limit(max(1, limit)).all()]
    count = 0
    for mapping_id in ids:
        try:
            if _reconcile_one(mapping_id) == "reconciled":
                count += 1
        except Exception:
            logger.exception("federated IAM mapping reconcile crashed id=%s", mapping_id)
    return count
