"""Typed, fail-closed contract for AI-proposed execution actions.

This module is intentionally independent from the provider/model layer.  A
model may suggest an action, but only this contract and a server-owned gateway
policy may turn it into an execution request.  It does not execute SSH or
Ceph commands yet; that integration is a separate step after this boundary is
proven by tests.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator


_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_TARGET_TYPES = frozenset({"cluster", "host", "node", "osd", "pool", "pg", "volume", "bucket", "object"})
_TARGET_SCOPE_FIELDS = {
    "host": "nodes",
    "node": "nodes",
    "osd": "osds",
    "pool": "pools",
    "pg": "pgs",
    "volume": "volumes",
    "bucket": "buckets",
    "object": "objects",
}
_FORBIDDEN_PARAM_KEYS = frozenset({
    "cmd", "command", "command_line", "freeform_command", "raw_command", "script", "shell",
})
_MAX_PARAMS_JSON_CHARS = 16_000
_MAX_PARAM_DEPTH = 5


class ActionContractError(ValueError):
    """The proposed action cannot cross the typed execution boundary."""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime phải có timezone")
    return value.astimezone(timezone.utc)


def _walk_params(value: Any, *, depth: int = 0) -> None:
    if depth > _MAX_PARAM_DEPTH:
        raise ValueError("action params lồng quá sâu")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("action params chỉ nhận key chuỗi không rỗng")
            if key.strip().lower() in _FORBIDDEN_PARAM_KEYS:
                raise ValueError("action params không được chứa free-form command")
            _walk_params(item, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _walk_params(item, depth=depth + 1)
    elif not isinstance(value, (str, int, float, bool)) and value is not None:
        raise ValueError("action params phải là JSON primitive/object/array")


class TypedActionRequest(BaseModel):
    """The only shape accepted by the typed execution gateway."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action_id: str = Field(min_length=1, max_length=64)
    cluster_id: str = Field(min_length=1, max_length=128)
    capability: str = Field(min_length=1, max_length=64)
    target_type: str = Field(min_length=1, max_length=16)
    target_id: str = Field(min_length=1, max_length=255)
    params: dict[str, Any] = Field(default_factory=dict)
    evidence_fingerprint: str = Field(min_length=64, max_length=64)
    expires_at: datetime
    actor: str = Field(min_length=1, max_length=128)
    approval_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("action_id", "capability")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError("identifier không hợp lệ")
        return value

    @field_validator("target_type")
    @classmethod
    def validate_target_type(cls, value: str) -> str:
        if value not in _TARGET_TYPES:
            raise ValueError("target_type không được hỗ trợ")
        return value

    @field_validator("evidence_fingerprint", "approval_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str | None) -> str | None:
        if value is not None and not _HEX64_RE.fullmatch(value):
            raise ValueError("fingerprint phải là SHA-256 hex 64 ký tự")
        return value.lower() if value else value

    @field_validator("expires_at")
    @classmethod
    def validate_expiry_timezone(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("params")
    @classmethod
    def validate_params(cls, value: dict[str, Any]) -> dict[str, Any]:
        _walk_params(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded) > _MAX_PARAMS_JSON_CHARS:
            raise ValueError("action params vượt giới hạn kích thước")
        return value


class TargetScope(BaseModel):
    """Server-owned targets currently valid for one selected cluster."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cluster_id: str = Field(min_length=1, max_length=128)
    nodes: frozenset[str] = frozenset()
    osds: frozenset[str] = frozenset()
    pools: frozenset[str] = frozenset()
    pgs: frozenset[str] = frozenset()
    volumes: frozenset[str] = frozenset()
    buckets: frozenset[str] = frozenset()
    objects: frozenset[str] = frozenset()


def execution_fingerprint(request: TypedActionRequest) -> str:
    """Hash the immutable action identity used by approval/idempotency checks."""
    payload = request.model_dump(mode="json", exclude={"approval_fingerprint"})
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _target_values(scope: TargetScope, target_type: str) -> frozenset[str]:
    if target_type == "cluster":
        return frozenset({scope.cluster_id})
    return getattr(scope, _TARGET_SCOPE_FIELDS[target_type])


class TypedActionGateway:
    """Validate a typed action before a worker may acquire an execution lease."""

    def __init__(
        self,
        *,
        allowed_action_ids: frozenset[str] | set[str],
        allowed_capabilities: frozenset[str] | set[str],
        target_scope: TargetScope,
        approval_required: frozenset[str] | set[str] = frozenset(),
        capability_check: Callable[[TypedActionRequest], bool] | None = None,
    ) -> None:
        self.allowed_action_ids = frozenset(allowed_action_ids)
        self.allowed_capabilities = frozenset(allowed_capabilities)
        self.target_scope = target_scope
        self.approval_required = frozenset(approval_required)
        self.capability_check = capability_check

    def authorize(
        self,
        request: TypedActionRequest | Mapping[str, Any],
        *,
        approved_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> TypedActionRequest:
        try:
            contract = request if isinstance(request, TypedActionRequest) else TypedActionRequest.model_validate(request)
        except Exception as exc:
            raise ActionContractError(f"typed action không hợp lệ: {exc}") from exc

        if contract.action_id not in self.allowed_action_ids:
            raise ActionContractError("action_id không nằm trong allowlist")
        if contract.capability not in self.allowed_capabilities:
            raise ActionContractError("capability không được cấp")
        if contract.cluster_id != self.target_scope.cluster_id:
            raise ActionContractError("cluster_id không khớp target scope")
        if contract.target_id not in _target_values(self.target_scope, contract.target_type):
            raise ActionContractError("target nằm ngoài scope hiện tại")

        current = _utc(now or datetime.now(timezone.utc))
        if contract.expires_at <= current:
            raise ActionContractError("evidence/action contract đã hết hạn")
        if self.capability_check is not None:
            try:
                capability_allowed = self.capability_check(contract)
            except Exception as exc:
                raise ActionContractError("capability matrix không khả dụng") from exc
            if not capability_allowed:
                raise ActionContractError("capability matrix từ chối action")

        if contract.action_id in self.approval_required:
            expected = execution_fingerprint(contract)
            if contract.approval_fingerprint != expected:
                raise ActionContractError("approval fingerprint không khớp action contract")
            if approved_fingerprint != expected:
                raise ActionContractError("chưa có approval server-side cho action contract")
        return contract
