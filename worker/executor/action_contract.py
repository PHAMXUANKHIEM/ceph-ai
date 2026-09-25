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
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
_RBD_POOL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_RBD_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

# This is intentionally a Dashboard/Worker contract, not an Incident or
# Autopilot action enum entry.  The API publishes the same shape so clients do
# not have to infer it from a shell command or from the UI form.
RBD_COPY_VOLUME_CONTRACT = {
    "action_id": "rbd_copy_volume",
    "surface": "dashboard_worker_only",
    "incident_autopilot": False,
    "classification": "RISKY",
    "capability": "block_storage.rbd_copy_snapshot",
    "target_type": "volume",
    "typed_params": {
        "pool_name": "source RBD pool",
        "image": "source RBD image",
        "snapshot": "explicit source snapshot",
        "dest_pool": "different destination RBD pool",
        "dest_image": "new destination RBD image",
        "size_bytes": "positive integer approved by preflight",
    },
    "requires_approval": True,
    "idempotency": "Idempotency-Key scoped to user/cluster/intent; replay returns the existing Action",
    "expiry": "Not admitted to Incident/Autopilot typed lease; Dashboard proposal remains approval-gated",
    "preflight": [
        "source snapshot exists",
        "destination pool is allowed, RBD-enabled and has capacity",
        "destination image does not exist",
        "Cinder-managed volume names are rejected",
    ],
    "post_check": [
        "destination name matches approved dest_image",
        "destination size matches approved size_bytes",
    ],
    "source_preserved": True,
}

RBD_MOVE_VOLUME_CONTRACT = {
    "action_id": "rbd_move_volume",
    "surface": "dashboard_worker_only",
    "incident_autopilot": False,
    "classification": "DESTRUCTIVE",
    "capability": "block_storage.rbd_move_unmanaged",
    "target_type": "volume",
    "typed_params": {
        "pool_name": "source RBD pool",
        "image": "source unmanaged RBD image",
        "dest_pool": "different destination RBD pool",
        "dest_image": "new destination RBD image",
        "size_bytes": "positive integer approved by preflight",
        "delete_source": "must be true after explicit operator confirmation",
        "move_token": "server-generated token used to resume only its own destination",
        "checksum": "must be rbd-export-diff-sha256",
    },
    "requires_approval": True,
    "idempotency": "Idempotency-Key scoped to user/cluster/intent; replay returns the existing Action",
    "expiry": "Never admitted to Incident/Autopilot typed lease; Dashboard proposal remains approval-gated",
    "preflight": [
        "source is unmanaged, detached, unlocked, watcher-free and has no snapshot/clone dependency",
        "destination pool is allowed, RBD-enabled and has capacity",
        "destination image does not exist",
        "Cinder-managed volume names are rejected",
        "explicit confirmation to delete source is required",
    ],
    "post_check": [
        "destination name and size match approved parameters",
        "source/destination export-diff SHA-256 values match before source deletion",
        "source is absent only after the verified copy",
    ],
    "source_preserved_until_verified": True,
}

RBD_MOVE_CLEANUP_PARTIAL_CONTRACT = {
    "action_id": "rbd_move_cleanup_partial",
    "surface": "dashboard_worker_only",
    "incident_autopilot": False,
    "classification": "DESTRUCTIVE",
    "capability": "block_storage.rbd_move_cleanup_partial",
    "target_type": "volume",
    "typed_params": {
        "pool_name": "source RBD pool",
        "image": "source RBD image that must remain present",
        "dest_pool": "destination RBD pool",
        "dest_image": "token-owned partial destination image",
        "size_bytes": "positive approved source size",
        "delete_destination": "must be true after explicit operator confirmation",
        "move_token": "original server-generated move token",
        "checksum": "must be rbd-export-diff-sha256",
        "cleanup_of_action_id": "failed rbd_move_volume Action being cleaned up",
    },
    "requires_approval": True,
    "preflight": [
        "the original move Action is FAILED and its source remains present",
        "destination exists and carries the exact original move token",
        "destination has no watcher or snapshot dependency",
        "source size still matches the approved move size",
    ],
    "post_check": [
        "source remains present",
        "destination is absent only after token ownership was verified",
    ],
    "source_preserved": True,
}


class ActionContractError(ValueError):
    """The proposed action cannot cross the typed execution boundary."""


class RbdCopyVolumeParams(BaseModel):
    """Strict parameter schema for the approval-gated cross-pool copy."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    pool_name: str = Field(min_length=1, max_length=128)
    image: str = Field(min_length=1, max_length=128)
    snapshot: str = Field(min_length=1, max_length=128)
    dest_pool: str = Field(min_length=1, max_length=128)
    dest_image: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(gt=0)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    requested_by: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("pool_name", "dest_pool")
    @classmethod
    def validate_pool_name(cls, value: str) -> str:
        if not _RBD_POOL_NAME_RE.fullmatch(value):
            raise ValueError("RBD pool name không hợp lệ")
        return value

    @field_validator("image", "snapshot", "dest_image")
    @classmethod
    def validate_image_name(cls, value: str) -> str:
        if not _RBD_IMAGE_NAME_RE.fullmatch(value):
            raise ValueError("RBD image/snapshot name không hợp lệ")
        return value

    @model_validator(mode="after")
    def validate_copy_boundary(self):
        if self.pool_name == self.dest_pool:
            raise ValueError("copy phải sang pool RBD khác")
        if self.image.startswith("volume-") or self.dest_image.startswith("volume-"):
            raise ValueError("Cinder-managed volume không được copy trực tiếp qua RBD")
        return self


class RbdMoveVolumeParams(BaseModel):
    """Strict parameter schema for the destructive copy-then-remove flow."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    pool_name: str = Field(min_length=1, max_length=128)
    image: str = Field(min_length=1, max_length=128)
    dest_pool: str = Field(min_length=1, max_length=128)
    dest_image: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(gt=0)
    delete_source: bool
    move_token: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{15,63}$")
    checksum: Literal["rbd-export-diff-sha256"]
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    requested_by: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("pool_name", "dest_pool")
    @classmethod
    def validate_pool_name(cls, value: str) -> str:
        if not _RBD_POOL_NAME_RE.fullmatch(value):
            raise ValueError("RBD pool name không hợp lệ")
        return value

    @field_validator("image", "dest_image")
    @classmethod
    def validate_image_name(cls, value: str) -> str:
        if not _RBD_IMAGE_NAME_RE.fullmatch(value):
            raise ValueError("RBD image name không hợp lệ")
        return value

    @model_validator(mode="after")
    def validate_move_boundary(self):
        if self.pool_name == self.dest_pool:
            raise ValueError("move phải sang pool RBD khác")
        if self.image == self.dest_image:
            raise ValueError("image nguồn và đích không được trùng tên")
        if self.image.startswith("volume-") or self.dest_image.startswith("volume-"):
            raise ValueError("Cinder-managed volume không được move trực tiếp qua RBD")
        if self.delete_source is not True:
            raise ValueError("move bắt buộc có xác nhận xóa nguồn")
        return self


class RbdMoveCleanupPartialParams(BaseModel):
    """Strict schema for deleting a token-owned incomplete destination only."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    pool_name: str = Field(min_length=1, max_length=128)
    image: str = Field(min_length=1, max_length=128)
    dest_pool: str = Field(min_length=1, max_length=128)
    dest_image: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(gt=0)
    delete_destination: bool
    move_token: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{15,63}$")
    checksum: Literal["rbd-export-diff-sha256"]
    cleanup_of_action_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    requested_by: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("pool_name", "dest_pool")
    @classmethod
    def validate_pool_name(cls, value: str) -> str:
        if not _RBD_POOL_NAME_RE.fullmatch(value):
            raise ValueError("RBD pool name không hợp lệ")
        return value

    @field_validator("image", "dest_image")
    @classmethod
    def validate_image_name(cls, value: str) -> str:
        if not _RBD_IMAGE_NAME_RE.fullmatch(value):
            raise ValueError("RBD image name không hợp lệ")
        return value

    @model_validator(mode="after")
    def validate_cleanup_boundary(self):
        if self.pool_name == self.dest_pool:
            raise ValueError("partial cleanup phải ở pool đích khác pool nguồn")
        if self.image == self.dest_image:
            raise ValueError("image nguồn và đích không được trùng tên")
        if self.delete_destination is not True:
            raise ValueError("partial cleanup bắt buộc có xác nhận xóa destination")
        return self


def validate_typed_action_params(action_id: str, params: Mapping[str, Any]) -> None:
    """Apply action-specific schemas after the generic JSON boundary."""
    if action_id == "rbd_move_volume":
        try:
            RbdMoveVolumeParams.model_validate(params)
        except Exception as exc:
            raise ActionContractError(f"params của {action_id} không hợp lệ: {exc}") from exc
        return
    if action_id == "rbd_move_cleanup_partial":
        try:
            RbdMoveCleanupPartialParams.model_validate(params)
        except Exception as exc:
            raise ActionContractError(f"params của {action_id} không hợp lệ: {exc}") from exc
        return
    if action_id != "rbd_copy_volume":
        return
    try:
        RbdCopyVolumeParams.model_validate(params)
    except Exception as exc:
        raise ActionContractError(f"params của {action_id} không hợp lệ: {exc}") from exc


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
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("action params không được chứa NaN hoặc Infinity")
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
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
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
        capabilities_by_action: Mapping[str, frozenset[str] | set[str]] | None = None,
    ) -> None:
        self.allowed_action_ids = frozenset(allowed_action_ids)
        self.allowed_capabilities = frozenset(allowed_capabilities)
        self.target_scope = target_scope
        self.approval_required = frozenset(approval_required)
        self.capability_check = capability_check
        self.capabilities_by_action = (
            {action: frozenset(capabilities) for action, capabilities in capabilities_by_action.items()}
            if capabilities_by_action is not None else None
        )

    def authorize(
        self,
        request: TypedActionRequest | Mapping[str, Any],
        *,
        approved_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> TypedActionRequest:
        try:
            # Pydantic's model_copy(update=...) does not validate the update.
            # Revalidate instances as well as mappings at this trust boundary.
            payload = request.model_dump(mode="python") if isinstance(request, TypedActionRequest) else request
            contract = TypedActionRequest.model_validate(payload)
        except Exception as exc:
            raise ActionContractError(f"typed action không hợp lệ: {exc}") from exc
        validate_typed_action_params(contract.action_id, contract.params)

        if contract.action_id not in self.allowed_action_ids:
            raise ActionContractError("action_id không nằm trong allowlist")
        if contract.capability not in self.allowed_capabilities:
            raise ActionContractError("capability không được cấp")
        if self.capabilities_by_action is None:
            if len(self.allowed_action_ids) != 1 or len(self.allowed_capabilities) != 1:
                raise ActionContractError("gateway đa action cần mapping action-capability")
        elif contract.capability not in self.capabilities_by_action.get(contract.action_id, frozenset()):
            raise ActionContractError("capability không khớp action_id")
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
            if capability_allowed is not True:
                raise ActionContractError("capability matrix từ chối action")

        if contract.action_id in self.approval_required:
            expected = execution_fingerprint(contract)
            if contract.approval_fingerprint != expected:
                raise ActionContractError("approval fingerprint không khớp action contract")
            if approved_fingerprint != expected:
                raise ActionContractError("chưa có approval server-side cho action contract")
        return contract
