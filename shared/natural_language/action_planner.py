"""Fail-closed action proposal validation for natural-language requests.

This module only creates a typed preview contract.  It deliberately has no
executor, SSH, database, or approval side effect; the existing approval
pipeline remains the only place that can execute a proposal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from shared.ai_redaction import redact_text


class ActionPlanningError(ValueError):
    """Raised when an action proposal is not safe to preview."""


_PARAM_TYPES = {
    "cluster": str,
    "pool": str,
    "pool_name": str,
    "image": str,
    "osd": int,
    "osd_id": int,
    "pg": int,
    "pg_num": int,
    "size": int,
    "node": str,
    "app_name": str,
    "threshold": (int, float),
}
_BYPASS_MARKERS = (
    "bo qua phe duyet", "bo qua approval", "skip approval", "ignore approval",
    "chay ngay", "run immediately", "xoa xac nhan", "remove confirmation",
)


@dataclass(frozen=True)
class ActionPreview:
    schema_version: str
    action_id: str
    cluster_id: str
    target_nodes: tuple[str, ...]
    params: dict[str, Any]
    command_preview: str | None
    classification: str
    approval_required: bool
    expected_impact: str
    rollback_limitation: str
    evidence_timestamp: str | None
    expires_at: str | None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["target_nodes"] = list(self.target_nodes)
        return value


def _fold(value: object) -> str:
    import unicodedata

    text = str(value or "").lower().replace("đ", "d")
    return "".join(
        char for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )


def _validate_params(params: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in params.items():
        expected = _PARAM_TYPES.get(str(key))
        if expected is None:
            raise ActionPlanningError(f"unsupported action parameter: {key}")
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, expected):
            raise ActionPlanningError(f"invalid type for action parameter: {key}")
        if isinstance(value, (int, float)) and value < 0:
            raise ActionPlanningError(f"action parameter must be non-negative: {key}")
        if isinstance(value, str) and not value.strip():
            raise ActionPlanningError(f"action parameter must not be blank: {key}")
        clean[str(key)] = value.strip() if isinstance(value, str) else value
    return clean


def validate_action_preview(
    *,
    action_id: str,
    registered_action_ids: Iterable[str],
    cluster_id: str,
    requested_cluster_id: str | None,
    target_nodes: Iterable[str],
    allowed_nodes: Iterable[str],
    params: Mapping[str, Any] | None = None,
    rationale: str = "",
    command_preview: str | None = None,
    classification: str = "RISKY",
    expected_impact: str = "Chưa xác định; operator phải kiểm tra preview trước approval.",
    rollback_limitation: str = "Rollback phụ thuộc action và phải được xác nhận riêng.",
    evidence_timestamp: str | None = None,
    expires_at: str | None = None,
) -> ActionPreview:
    """Validate a proposal and return a non-executable preview."""

    action = str(action_id or "").strip()
    cluster = str(cluster_id or "").strip()
    requested = str(requested_cluster_id or "").strip() or None
    registered = {str(item).strip() for item in registered_action_ids if str(item).strip()}
    if not action or action not in registered:
        raise ActionPlanningError("action_id is not registered")
    if not cluster or (requested is not None and requested != cluster):
        raise ActionPlanningError("cluster scope mismatch")
    nodes = tuple(str(item).strip() for item in target_nodes if str(item).strip())
    allowed = {str(item).strip() for item in allowed_nodes if str(item).strip()}
    if not nodes or any(node not in allowed for node in nodes):
        raise ActionPlanningError("target node is outside the configured cluster scope")
    if not str(rationale or "").strip():
        raise ActionPlanningError("rationale is required")
    if any(marker in _fold(rationale) for marker in _BYPASS_MARKERS):
        raise ActionPlanningError("approval bypass language is not accepted")
    normalized_classification = str(classification or "RISKY").upper()
    if normalized_classification not in {"SAFE", "RISKY", "DESTRUCTIVE"}:
        raise ActionPlanningError("invalid action classification")
    return ActionPreview(
        schema_version="action-preview-v1",
        action_id=action,
        cluster_id=cluster,
        target_nodes=nodes,
        params=_validate_params(params or {}),
        command_preview=redact_text(command_preview) if command_preview else None,
        classification=normalized_classification,
        approval_required=normalized_classification != "SAFE",
        expected_impact=redact_text(expected_impact),
        rollback_limitation=redact_text(rollback_limitation),
        evidence_timestamp=evidence_timestamp,
        expires_at=expires_at,
    )
