"""Admin-only RGW federated identity provider registry.

This first IAM slice deliberately stores references, not credentials.  It
provides a safe provider inventory, preview, transport/discovery validation,
and an explicit apply/disable transition.  Role mapping and STS issuance are
separate follow-up actions and must not be implied by a provider row alone.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.ldap_identity import validate_directory_provider
from shared.rgw_sts import StsIssueError, assume_role_with_web_identity, role_arn, validate_session_name, validate_tags
from shared.models import (
    RgwFederatedIdentityAudit,
    RgwFederatedIdentityProvider,
    RgwFederatedRoleMapping,
    RgwFederatedStsSession,
)
from shared.time import utc_now

router = APIRouter()
templates = make_templates()
logger = logging.getLogger(__name__)

PROVIDER_TYPES = frozenset({"oidc", "ldap", "ad"})
MAPPING_SOURCE_TYPES = frozenset({"claim", "group"})
STS_STATUSES = frozenset({"REQUESTED", "ACTIVE", "EXPIRED", "REVOKED", "FAILED"})
SENSITIVE_KEYS = frozenset({
    "password", "pass", "token", "access_token", "refresh_token", "client_secret",
    "secret", "private_key", "private_key_pem", "bind_password",
})
SECRET_REF_RE = re.compile(r"^[A-Za-z0-9_.:/#-]{1,255}$")
MAX_ERROR_LENGTH = 500


def _require_admin(user: str) -> None:
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin mới được quản lý Federated IAM")


def _request_id(request: Request) -> str:
    raw = str(request.headers.get("X-Request-ID") or "").strip()
    return raw[:128] if raw else ""


def _safe_error(exc: Exception) -> str:
    text = str(exc).replace("\n", " ").strip()
    return f"{type(exc).__name__}: {text[:MAX_ERROR_LENGTH]}"


def _validate_url(value: object, *, schemes: set[str], field: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if not raw or parsed.scheme.lower() not in schemes or not parsed.hostname:
        expected = "/".join(sorted(schemes))
        raise HTTPException(status_code=400, detail=f"{field} phải dùng {expected} và có hostname")
    if len(raw) > 2048:
        raise HTTPException(status_code=400, detail=f"{field} quá dài")
    return raw.rstrip("/")


def _reject_sensitive_keys(value: object, *, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).strip().casefold()
            if key_text in SENSITIVE_KEYS or any(part in key_text for part in ("password", "token", "private_key")):
                raise HTTPException(
                    status_code=400,
                    detail=f"{path}.{key} không được chứa secret; dùng secret_ref",
                )
            _reject_sensitive_keys(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, path=f"{path}[{index}]")


def _safe_config(value: object) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="config phải là object JSON")
    _reject_sensitive_keys(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
        parsed = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="config chứa kiểu dữ liệu không hỗ trợ") from exc
    if len(encoded) > 12000:
        raise HTTPException(status_code=400, detail="config quá lớn")
    return parsed


def _normalize_payload(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Payload phải là object JSON")
    name = str(body.get("name") or "").strip()
    provider_type = str(body.get("provider_type") or "").strip().casefold()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}", name):
        raise HTTPException(status_code=400, detail="Tên provider không hợp lệ")
    if provider_type not in PROVIDER_TYPES:
        raise HTTPException(status_code=400, detail="provider_type phải là oidc, ldap hoặc ad")

    issuer_url = None
    endpoint_url = None
    if provider_type == "oidc":
        issuer_url = _validate_url(body.get("issuer_url"), schemes={"http", "https"}, field="issuer_url")
        if body.get("endpoint_url"):
            endpoint_url = _validate_url(body.get("endpoint_url"), schemes={"http", "https"}, field="endpoint_url")
    else:
        endpoint_url = _validate_url(
            body.get("endpoint_url"), schemes={"ldap", "ldaps"}, field="endpoint_url",
        )

    secret_ref = str(body.get("secret_ref") or "").strip()
    if secret_ref and not SECRET_REF_RE.fullmatch(secret_ref):
        raise HTTPException(status_code=400, detail="secret_ref không hợp lệ")
    config = _safe_config(body.get("config"))
    audience = str(body.get("audience") or "").strip() or None
    if audience and len(audience) > 255:
        raise HTTPException(status_code=400, detail="audience quá dài")
    return {
        "name": name,
        "provider_type": provider_type,
        "issuer_url": issuer_url,
        "endpoint_url": endpoint_url,
        "audience": audience,
        "secret_ref": secret_ref or None,
        "config": config,
    }


def _provider_view(row: RgwFederatedIdentityProvider) -> dict:
    try:
        config = json.loads(row.config_json or "{}")
    except (TypeError, ValueError):
        config = {}
    return {
        "id": row.id,
        "name": row.name,
        "provider_type": row.provider_type,
        "status": row.status,
        "enabled": bool(row.enabled),
        "issuer_url": row.issuer_url,
        "endpoint_url": row.endpoint_url,
        "audience": row.audience,
        "secret_configured": bool(row.secret_ref),
        "config": config,
        "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None,
        "last_error": row.last_error,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _mapping_policy(value: object) -> tuple[dict, list[str]]:
    if not isinstance(value, dict) or value.get("Version") != "2012-10-17":
        raise HTTPException(status_code=400, detail="Policy mapping cần Version 2012-10-17")
    statements = value.get("Statement")
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list) or not 1 <= len(statements) <= 50:
        raise HTTPException(status_code=400, detail="Policy mapping cần từ 1 đến 50 Statement")
    warnings: list[str] = []
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") not in {"Allow", "Deny"}:
            raise HTTPException(status_code=400, detail="Statement/Effect trong mapping không hợp lệ")
        actions = statement.get("Action")
        actions = [actions] if isinstance(actions, str) else actions
        resources = statement.get("Resource")
        resources = [resources] if isinstance(resources, str) else resources
        if not isinstance(actions, list) or not actions or not all(
            isinstance(action, str) and 1 <= len(action) <= 128 for action in actions
        ):
            raise HTTPException(status_code=400, detail="Statement phải có Action hợp lệ")
        if not isinstance(resources, list) or not resources or not all(
            isinstance(resource, str) and 1 <= len(resource) <= 2048 for resource in resources
        ):
            raise HTTPException(status_code=400, detail="Statement phải có Resource hợp lệ")
        if any("*" in action for action in actions):
            warnings.append("Mapping chứa wildcard Action; cần review quyền dư thừa trước khi reconcile.")
        if any(resource == "*" for resource in resources):
            warnings.append("Mapping có Resource *; không nên register nếu chưa có approval riêng.")
        if "${" in json.dumps(statement, ensure_ascii=False):
            raise HTTPException(status_code=400, detail="Mapping không hỗ trợ string interpolation")
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise HTTPException(status_code=400, detail="Policy mapping vượt quá 64 KiB")
    return json.loads(encoded), sorted(set(warnings))


def _normalize_mapping_payload(body: dict) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Payload mapping phải là object JSON")
    name = str(body.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}", name):
        raise HTTPException(status_code=400, detail="Tên role mapping không hợp lệ")
    provider_id = str(body.get("provider_id") or "").strip()
    if not provider_id or len(provider_id) > 64:
        raise HTTPException(status_code=400, detail="provider_id không hợp lệ")
    source_type = str(body.get("source_type") or "").strip().casefold()
    if source_type not in MAPPING_SOURCE_TYPES:
        raise HTTPException(status_code=400, detail="source_type phải là claim hoặc group")
    source_key = str(body.get("source_key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", source_key):
        raise HTTPException(status_code=400, detail="source_key không hợp lệ")
    match_value = str(body.get("match_value") or "").strip()
    if not match_value or len(match_value) > 255:
        raise HTTPException(status_code=400, detail="match_value không hợp lệ")
    policy, warnings = _mapping_policy(body.get("policy"))
    return {
        "name": name,
        "provider_id": provider_id,
        "source_type": source_type,
        "source_key": source_key,
        "match_value": match_value,
        "policy": policy,
        "warnings": warnings,
    }


def _mapping_view(row: RgwFederatedRoleMapping) -> dict:
    try:
        policy = json.loads(row.policy_json or "{}")
    except (TypeError, ValueError):
        policy = {}
    encoded = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "name": row.name,
        "source_type": row.source_type,
        "source_key": row.source_key,
        "match_value": row.match_value,
        "policy": policy,
        "policy_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "status": row.status,
        "enabled": bool(row.enabled),
        "rgw_role_name": row.rgw_role_name,
        "reconcile_attempts": row.reconcile_attempts,
        "last_reconciled_at": row.last_reconciled_at.isoformat() if row.last_reconciled_at else None,
        "last_reconcile_error": row.last_reconcile_error,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _sts_view(row: RgwFederatedStsSession) -> dict:
    try:
        tags = json.loads(row.session_tags_json or "{}")
    except (TypeError, ValueError):
        tags = {}
    return {
        "id": row.id,
        "provider_id": row.provider_id,
        "mapping_id": row.mapping_id,
        "actor": row.actor,
        "role_name": row.role_name,
        "session_name": row.session_name,
        "session_tags": tags if isinstance(tags, dict) else {},
        "status": row.status,
        "duration_seconds": row.duration_seconds,
        "access_key_id": row.access_key_id,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "issued_at": row.issued_at.isoformat() if row.issued_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "revoked_by": row.revoked_by,
        "failure_reason": row.failure_reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _expire_sts_sessions(session) -> None:
    now = utc_now()
    rows = session.query(RgwFederatedStsSession).filter(
        RgwFederatedStsSession.status == "ACTIVE",
        RgwFederatedStsSession.expires_at.is_not(None),
        RgwFederatedStsSession.expires_at <= now,
    ).all()
    for row in rows:
        row.status = "EXPIRED"
        row.updated_at = now


def _sts_request_payload(body: object) -> dict:
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="STS payload phải là object JSON")
    mapping_id = str(body.get("mapping_id") or "").strip()
    if not mapping_id or len(mapping_id) > 64:
        raise HTTPException(status_code=400, detail="mapping_id không hợp lệ")
    try:
        duration_seconds = int(body.get("duration_seconds", 3600))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="duration_seconds không hợp lệ") from exc
    if not 900 <= duration_seconds <= 43200:
        raise HTTPException(status_code=400, detail="duration_seconds phải trong khoảng 900-43200")
    session_name = str(body.get("session_name") or "").strip()
    try:
        session_name = validate_session_name(session_name)
        tags = validate_tags(body.get("session_tags"))
    except StsIssueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = str(body.get("web_identity_token") or "").strip()
    if not token or len(token) > 16384:
        raise HTTPException(status_code=400, detail="web_identity_token là bắt buộc và tối đa 16384 ký tự")
    return {
        "mapping_id": mapping_id,
        "duration_seconds": duration_seconds,
        "session_name": session_name,
        "session_tags": tags,
        "web_identity_token": token,
    }


def _sts_context(session, payload: dict) -> tuple[RgwFederatedIdentityProvider, RgwFederatedRoleMapping, dict]:
    mapping = session.get(RgwFederatedRoleMapping, payload["mapping_id"])
    if mapping is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy role mapping")
    provider = session.get(RgwFederatedIdentityProvider, mapping.provider_id)
    if provider is None or provider.status != "APPLIED" or not provider.enabled:
        raise HTTPException(status_code=409, detail="Provider phải ở trạng thái APPLIED")
    if provider.provider_type != "oidc":
        raise HTTPException(status_code=409, detail="STS web identity hiện yêu cầu provider OIDC")
    if mapping.status != "RECONCILED" or not mapping.enabled or not mapping.rgw_role_name:
        raise HTTPException(status_code=409, detail="Role mapping phải RECONCILED trước khi cấp STS")
    try:
        config = json.loads(provider.config_json or "{}")
    except (TypeError, ValueError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    return provider, mapping, config


def _audit(
    *, provider_id: str | None, actor: str, action: str, request_id: str,
    result: str, evidence: dict | None = None, error: str | None = None,
) -> None:
    with db.SessionLocal() as session:
        session.add(RgwFederatedIdentityAudit(
            provider_id=provider_id,
            actor=actor,
            action=action,
            request_id=request_id or None,
            result=result,
            evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
            error_message=error,
        ))
        session.commit()


async def _probe_oidc(row: RgwFederatedIdentityProvider) -> dict:
    issuer = _validate_url(row.issuer_url, schemes={"http", "https"}, field="issuer_url")
    discovery = f"{issuer}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        response = await client.get(discovery)
    if response.status_code >= 400:
        raise RuntimeError(f"OIDC discovery trả HTTP {response.status_code}")
    try:
        document = response.json()
    except ValueError as exc:
        raise RuntimeError("OIDC discovery không trả JSON") from exc
    required = ("issuer", "jwks_uri", "authorization_endpoint", "token_endpoint")
    missing = [key for key in required if not document.get(key)]
    if missing:
        raise RuntimeError(f"OIDC discovery thiếu: {', '.join(missing)}")
    discovered_issuer = str(document["issuer"]).rstrip("/")
    if discovered_issuer != issuer:
        raise RuntimeError("issuer trong discovery không khớp issuer_url đã cấu hình")
    return {
        "kind": "oidc_discovery",
        "issuer": discovered_issuer,
        "jwks_uri": str(document["jwks_uri"]),
        "authorization_endpoint": str(document["authorization_endpoint"]),
        "token_endpoint": str(document["token_endpoint"]),
        "http_status": response.status_code,
    }


async def _validate_provider(row: RgwFederatedIdentityProvider) -> dict:
    if row.provider_type == "oidc":
        return await _probe_oidc(row)
    return await asyncio.to_thread(
        validate_directory_provider,
        row.provider_type,
        row.endpoint_url or "",
        row.secret_ref,
        row.config_json,
    )


@router.get("/settings/federated-iam", response_class=HTMLResponse)
async def federated_iam_page(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        providers = [_provider_view(row) for row in session.query(
            RgwFederatedIdentityProvider
        ).order_by(RgwFederatedIdentityProvider.created_at.desc()).all()]
        mappings = [_mapping_view(row) for row in session.query(
            RgwFederatedRoleMapping
        ).order_by(RgwFederatedRoleMapping.created_at.desc()).all()]
    return templates.TemplateResponse(request, "federated_iam.html", {
        "user": user,
        "is_admin": True,
        "providers": providers,
        "mappings": mappings,
        "provider_types": sorted(PROVIDER_TYPES),
    })


@router.get("/api/settings/federated-iam/providers")
async def list_providers(user: str = Depends(require_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        rows = session.query(RgwFederatedIdentityProvider).order_by(
            RgwFederatedIdentityProvider.created_at.desc()
        ).all()
        return {"items": [_provider_view(row) for row in rows], "count": len(rows)}


@router.post("/api/settings/federated-iam/providers/preview")
async def preview_provider(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    payload = _normalize_payload(await request.json())
    return {
        "ok": True,
        "provider": {
            **{key: value for key, value in payload.items() if key != "config"},
            "secret_configured": bool(payload["secret_ref"]),
            "config": payload["config"],
        },
        "warnings": [
            "Provider mới chỉ ở trạng thái DRAFT; chưa cấp STS và chưa map role/policy.",
            "LDAP/AD Validate cần bind_dn, base_dn và secret_ref dạng env:/file:/vault:; không lưu password trong config.",
        ],
    }


@router.post("/api/settings/federated-iam/providers")
async def create_provider(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    payload = _normalize_payload(await request.json())
    with db.SessionLocal() as session:
        if session.query(RgwFederatedIdentityProvider).filter_by(name=payload["name"]).first():
            raise HTTPException(status_code=409, detail="Provider name đã tồn tại")
        row = RgwFederatedIdentityProvider(
            name=payload["name"], provider_type=payload["provider_type"],
            issuer_url=payload["issuer_url"], endpoint_url=payload["endpoint_url"],
            audience=payload["audience"], secret_ref=payload["secret_ref"],
            config_json=json.dumps(payload["config"], ensure_ascii=False, sort_keys=True),
            created_by=user,
        )
        session.add(row)
        session.flush()
        provider_id = row.id
        session.commit()
    _audit(
        provider_id=provider_id, actor=user, action="provider_create", request_id=_request_id(request),
        result="succeeded", evidence={"provider_type": payload["provider_type"], "secret_configured": bool(payload["secret_ref"])},
    )
    return {"ok": True, "provider_id": provider_id}


@router.post("/api/settings/federated-iam/providers/{provider_id}/validate")
async def validate_provider(provider_id: str, request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        row = session.get(RgwFederatedIdentityProvider, provider_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy provider")
        try:
            evidence = await _validate_provider(row)
        except Exception as exc:
            row.status = "FAILED"
            row.last_error = _safe_error(exc)
            row.last_checked_at = utc_now()
            session.commit()
            _audit(
                provider_id=row.id, actor=user, action="provider_validate", request_id=_request_id(request),
                result="failed", error=row.last_error,
            )
            raise HTTPException(status_code=502, detail="Provider validation thất bại") from exc
        row.status = "VALID"
        row.last_error = None
        row.last_checked_at = utc_now()
        session.commit()
        provider = _provider_view(row)
    _audit(
        provider_id=provider_id, actor=user, action="provider_validate", request_id=_request_id(request),
        result="succeeded", evidence=evidence,
    )
    return {"ok": True, "provider": provider, "evidence": evidence}


@router.post("/api/settings/federated-iam/providers/{provider_id}/apply")
async def apply_provider(provider_id: str, request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    body = await request.json()
    with db.SessionLocal() as session:
        row = session.get(RgwFederatedIdentityProvider, provider_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy provider")
        if str(body.get("confirmation") or "").strip() != row.name:
            raise HTTPException(status_code=400, detail="Cần nhập đúng tên provider để apply")
        if row.status != "VALID":
            raise HTTPException(status_code=409, detail="Provider phải Validate thành công trước khi apply")
        row.enabled = True
        row.status = "APPLIED"
        row.updated_at = utc_now()
        session.commit()
        provider = _provider_view(row)
    _audit(
        provider_id=provider_id, actor=user, action="provider_apply", request_id=_request_id(request),
        result="succeeded", evidence={"enabled": True, "note": "Registry only; role mapping/STS chưa bật."},
    )
    return {"ok": True, "provider": provider}


@router.post("/api/settings/federated-iam/providers/{provider_id}/disable")
async def disable_provider(provider_id: str, request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        row = session.get(RgwFederatedIdentityProvider, provider_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy provider")
        row.enabled = False
        row.status = "DISABLED"
        row.updated_at = utc_now()
        session.commit()
        provider = _provider_view(row)
    _audit(
        provider_id=provider_id, actor=user, action="provider_disable", request_id=_request_id(request),
        result="succeeded", evidence={"enabled": False},
    )
    return {"ok": True, "provider": provider}


@router.get("/api/settings/federated-iam/mappings")
async def list_mappings(user: str = Depends(require_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        rows = session.query(RgwFederatedRoleMapping).order_by(
            RgwFederatedRoleMapping.created_at.desc()
        ).all()
        return {"items": [_mapping_view(row) for row in rows], "count": len(rows)}


@router.post("/api/settings/federated-iam/mappings/preview")
async def preview_mapping(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    payload = _normalize_mapping_payload(await request.json())
    with db.SessionLocal() as session:
        provider = session.get(RgwFederatedIdentityProvider, payload["provider_id"])
        if provider is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy provider của mapping")
        provider_name = provider.name
    policy = payload["policy"]
    encoded = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "ok": True,
        "mapping": {
            **{key: value for key, value in payload.items() if key != "policy"},
            "provider_name": provider_name,
            "policy": policy,
            "policy_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        },
        "warnings": payload["warnings"] + [
            "Preview chưa thay đổi RGW; reconcile qua worker sẽ được triển khai ở bước tiếp theo.",
        ],
    }


@router.post("/api/settings/federated-iam/mappings")
async def create_mapping(request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    payload = _normalize_mapping_payload(await request.json())
    with db.SessionLocal() as session:
        if session.get(RgwFederatedIdentityProvider, payload["provider_id"]) is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy provider của mapping")
        if session.query(RgwFederatedRoleMapping).filter_by(name=payload["name"]).first():
            raise HTTPException(status_code=409, detail="Role mapping name đã tồn tại")
        row = RgwFederatedRoleMapping(
            provider_id=payload["provider_id"], name=payload["name"],
            source_type=payload["source_type"], source_key=payload["source_key"],
            match_value=payload["match_value"],
            policy_json=json.dumps(payload["policy"], ensure_ascii=False, sort_keys=True),
            created_by=user,
        )
        session.add(row)
        session.flush()
        mapping_id = row.id
        session.commit()
    _audit(
        provider_id=payload["provider_id"], actor=user, action="role_mapping_create",
        request_id=_request_id(request), result="succeeded",
        evidence={"mapping_id": mapping_id, "mapping_name": payload["name"],
                  "policy_sha256": hashlib.sha256(
                      json.dumps(payload["policy"], ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8")
                  ).hexdigest()},
    )
    return {"ok": True, "mapping_id": mapping_id}


@router.post("/api/settings/federated-iam/mappings/{mapping_id}/register")
async def register_mapping(mapping_id: str, request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    body = await request.json()
    with db.SessionLocal() as session:
        row = session.get(RgwFederatedRoleMapping, mapping_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy role mapping")
        if str(body.get("confirmation") or "").strip() != row.name:
            raise HTTPException(status_code=400, detail="Cần nhập đúng tên mapping để register")
        if row.status not in {"DRAFT", "RECONCILE_FAILED", "BLOCKED"}:
            raise HTTPException(status_code=409, detail="Mapping không ở trạng thái có thể register lại")
        provider = session.get(RgwFederatedIdentityProvider, row.provider_id)
        if provider is None or not provider.enabled or provider.status != "APPLIED":
            raise HTTPException(status_code=409, detail="Provider phải được Apply trước khi register mapping")
        row.enabled = False
        row.status = "REGISTERED"
        row.last_reconcile_error = None
        row.updated_at = utc_now()
        session.commit()
        mapping = _mapping_view(row)
    _audit(
        provider_id=mapping["provider_id"], actor=user, action="role_mapping_register",
        request_id=_request_id(request), result="succeeded",
        evidence={"mapping_id": mapping_id, "status": "REGISTERED",
                  "note": "Worker sẽ reconcile role/policy vào RGW."},
    )
    return {
        "ok": True,
        "mapping": mapping,
        "reconciliation": {"status": "QUEUED", "message": "Worker sẽ reconcile role/policy vào RGW policy store."},
    }


@router.post("/api/settings/federated-iam/sts/sessions/preview")
async def preview_sts_session(request: Request, user: str = Depends(require_login)):
    """Validate the STS request without calling RGW or issuing credentials."""
    _require_admin(user)
    payload = _sts_request_payload(await request.json())
    with db.SessionLocal() as session:
        provider, mapping, config = _sts_context(session, payload)
        return {
            "ok": True,
            "session": {
                "mapping_id": mapping.id,
                "provider_name": provider.name,
                "role_name": mapping.rgw_role_name,
                "role_arn": role_arn(mapping.rgw_role_name),
                "session_name": payload["session_name"],
                "duration_seconds": payload["duration_seconds"],
                "session_tags": payload["session_tags"],
                "sts_endpoint": str(config.get("sts_endpoint") or "configured RGW endpoint"),
            },
            "warnings": [
                "Preview không gọi RGW và chưa tạo credential.",
                "web_identity_token chỉ được dùng trong request này và không lưu vào database/audit.",
            ],
        }


@router.post("/api/settings/federated-iam/sts/sessions")
async def issue_sts_session(request: Request, user: str = Depends(require_login)):
    """Issue credentials once and retain only non-secret session metadata."""
    _require_admin(user)
    payload = _sts_request_payload(await request.json())
    request_id = _request_id(request)
    with db.SessionLocal() as session:
        provider, mapping, config = _sts_context(session, payload)
        provider_id = provider.id
        role_name = mapping.rgw_role_name
        sts_config = dict(config)
        row = RgwFederatedStsSession(
            provider_id=provider_id,
            mapping_id=mapping.id,
            actor=user,
            role_name=role_name,
            session_name=payload["session_name"],
            subject_fingerprint=hashlib.sha256(payload["web_identity_token"].encode("utf-8")).hexdigest(),
            session_tags_json=json.dumps(payload["session_tags"], ensure_ascii=False, sort_keys=True),
            status="REQUESTED",
            duration_seconds=payload["duration_seconds"],
            request_id=request_id or None,
        )
        session.add(row)
        session.flush()
        session_id = row.id
        session.commit()
    try:
        credentials = await asyncio.to_thread(
            assume_role_with_web_identity,
            role_name=role_name,
            session_name=payload["session_name"],
            web_identity_token=payload["web_identity_token"],
            duration_seconds=payload["duration_seconds"],
            session_tags=payload["session_tags"],
            endpoint_url=str(sts_config.get("sts_endpoint") or ""),
            region_name=str(sts_config.get("sts_region") or "us-east-1"),
            verify_tls=bool(sts_config.get("sts_tls_verify", True)),
        )
    except Exception as exc:
        error = _safe_error(exc)
        with db.SessionLocal() as session:
            failed = session.get(RgwFederatedStsSession, session_id)
            if failed is not None:
                failed.status = "FAILED"
                failed.failure_reason = error
                failed.updated_at = utc_now()
                session.commit()
        _audit(
            provider_id=provider_id, actor=user, action="sts_issue",
            request_id=request_id, result="failed",
            evidence={"session_id": session_id, "role_name": role_name}, error=error,
        )
        raise HTTPException(status_code=502, detail="Cấp STS credential thất bại") from exc

    raw_expiration = str(credentials.get("expiration") or "")
    try:
        expires_at = datetime.fromisoformat(raw_expiration.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        expires_at = utc_now() + timedelta(seconds=payload["duration_seconds"])
    with db.SessionLocal() as session:
        issued = session.get(RgwFederatedStsSession, session_id)
        if issued is None:
            raise HTTPException(status_code=500, detail="Không lưu được metadata STS session")
        issued.status = "ACTIVE"
        issued.access_key_id = credentials["access_key_id"]
        issued.expires_at = expires_at
        issued.issued_at = utc_now()
        issued.updated_at = utc_now()
        session.commit()
        view = _sts_view(issued)
    _audit(
        provider_id=provider_id, actor=user, action="sts_issue",
        request_id=request_id, result="succeeded",
        evidence={"session_id": session_id, "role_name": role_name,
                  "access_key_id": credentials["access_key_id"], "expires_at": expires_at.isoformat()},
    )
    return {
        "ok": True,
        "session": view,
        "credentials": {
            "access_key_id": credentials["access_key_id"],
            "secret_access_key": credentials["secret_access_key"],
            "session_token": credentials["session_token"],
            "expiration": raw_expiration,
        },
        "warning": "Credential secret chỉ hiển thị trong response này; Ceph-AI không lưu secret_access_key hoặc session_token.",
    }


@router.get("/api/settings/federated-iam/sts/sessions")
async def list_sts_sessions(user: str = Depends(require_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        _expire_sts_sessions(session)
        session.commit()
        rows = session.query(RgwFederatedStsSession).order_by(
            RgwFederatedStsSession.created_at.desc()
        ).limit(100).all()
        return {"items": [_sts_view(row) for row in rows], "count": len(rows)}


@router.post("/api/settings/federated-iam/sts/sessions/{session_id}/revoke")
async def revoke_sts_session(session_id: str, request: Request, user: str = Depends(require_login)):
    _require_admin(user)
    body = await request.json()
    with db.SessionLocal() as session:
        row = session.get(RgwFederatedStsSession, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy STS session")
        if str(body.get("confirmation") or "").strip() != row.id:
            raise HTTPException(status_code=400, detail="Cần nhập đúng session id để revoke")
        if row.status not in {"REQUESTED", "ACTIVE"}:
            raise HTTPException(status_code=409, detail="STS session không còn active để revoke")
        row.status = "REVOKED"
        row.revoked_at = utc_now()
        row.revoked_by = user
        row.updated_at = utc_now()
        provider_id = row.provider_id
        access_key_id = row.access_key_id
        request_id = _request_id(request)
        session.commit()
        view = _sts_view(row)
    _audit(
        provider_id=provider_id, actor=user, action="sts_session_revoke",
        request_id=request_id, result="succeeded",
        evidence={"session_id": session_id, "access_key_id": access_key_id,
                  "note": "Control-plane revoke; credential phía RGW còn hiệu lực đến expires_at nếu RGW không hỗ trợ revoke trực tiếp."},
    )
    return {
        "ok": True,
        "session": view,
        "warning": "Đã revoke ở control plane; credential đã cấp có thể còn hiệu lực đến khi hết TTL phía RGW.",
    }
