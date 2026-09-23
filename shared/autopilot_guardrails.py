"""Pure guardrail evaluator for the three-level Safe Autopilot contract."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json


class AutopilotMode(str, Enum):
    ADVISORY = "ADVISORY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    LIMITED_AUTOPILOT = "LIMITED_AUTOPILOT"


@dataclass(frozen=True)
class GuardrailContext:
    mode: AutopilotMode | str
    kill_switch: bool
    cluster_enabled: bool
    action_id: str
    classification: str
    allowlisted: bool
    target_count: int
    max_targets: int
    actions_used: int
    action_budget: int
    cooldown_active: bool
    health_status: str
    maintenance_window_open: bool


@dataclass(frozen=True)
class GuardrailDecision:
    allowed: bool
    reason: str
    mode: AutopilotMode
    requires_approval: bool = False


def _mode(value: AutopilotMode | str) -> AutopilotMode:
    return value if isinstance(value, AutopilotMode) else AutopilotMode(str(value).upper())


def cluster_configured_mode(*, cluster_id: str | None, raw_mapping: str | None,
                            fallback_mode: str | None = None) -> str | None:
    """Resolve one cluster's mode from a JSON environment mapping.

    ``raw_mapping`` is intentionally an operational escape hatch until the
    per-cluster settings migration is deployed.  Once it is present, an
    omitted cluster is approval-only; this prevents a global LIMITED setting
    from silently granting writes to a newly-added cluster.  Invalid JSON or
    non-string values also fail closed by returning ``APPROVAL_REQUIRED``.
    """
    if not raw_mapping or not raw_mapping.strip():
        return fallback_mode
    try:
        mapping = json.loads(raw_mapping)
    except (TypeError, ValueError):
        return AutopilotMode.APPROVAL_REQUIRED.value
    if not isinstance(mapping, dict):
        return AutopilotMode.APPROVAL_REQUIRED.value
    if cluster_id is None:
        return AutopilotMode.APPROVAL_REQUIRED.value
    value = mapping.get(str(cluster_id))
    if not isinstance(value, str) or not value.strip():
        return AutopilotMode.APPROVAL_REQUIRED.value
    return value.strip()


def resolve_mode(*, global_enabled: bool, cluster_enabled: bool,
                 advisory_only: bool = False) -> AutopilotMode:
    """Derive the operator-visible mode without granting execution rights."""
    if advisory_only:
        return AutopilotMode.ADVISORY
    if global_enabled and cluster_enabled:
        return AutopilotMode.LIMITED_AUTOPILOT
    return AutopilotMode.APPROVAL_REQUIRED


def effective_runtime_mode(*, global_enabled: bool, cluster_enabled: bool,
                           configured_mode: str | None = None) -> AutopilotMode:
    """An explicit mode may reduce, but never grant, legacy execution rights.

    An unknown setting fails closed. The two existing switches remain hard
    kill switches until the persisted per-cluster mode migration is complete.
    """
    legacy = resolve_mode(global_enabled=global_enabled, cluster_enabled=cluster_enabled)
    if not configured_mode:
        return legacy
    try:
        requested = _mode(configured_mode.strip())
    except ValueError:
        return AutopilotMode.ADVISORY
    if legacy != AutopilotMode.LIMITED_AUTOPILOT:
        return legacy
    return requested


def evaluate_guardrails(context: GuardrailContext) -> GuardrailDecision:
    try:
        mode = _mode(context.mode)
    except ValueError:
        return GuardrailDecision(False, "unknown autopilot mode", AutopilotMode.APPROVAL_REQUIRED)
    if mode == AutopilotMode.ADVISORY:
        return GuardrailDecision(False, "advisory mode never executes writes", mode, True)
    if not context.kill_switch:
        return GuardrailDecision(False, "global kill switch is active", mode, True)
    if not context.cluster_enabled:
        return GuardrailDecision(False, "cluster autopilot gate is disabled", mode, True)
    if mode == AutopilotMode.APPROVAL_REQUIRED:
        return GuardrailDecision(False, "operator approval is required by current mode", mode, True)
    if str(context.classification).upper() != "SAFE":
        return GuardrailDecision(False, "limited autopilot only permits SAFE actions", mode, True)
    if not context.allowlisted:
        return GuardrailDecision(False, "action is not on the cluster allowlist", mode, True)
    if context.target_count < 1 or context.target_count > context.max_targets:
        return GuardrailDecision(False, "blast-radius limit exceeded", mode, True)
    if context.actions_used >= context.action_budget:
        return GuardrailDecision(False, "action budget is exhausted", mode, True)
    if context.cooldown_active:
        return GuardrailDecision(False, "action cooldown is active", mode, True)
    if not context.maintenance_window_open:
        return GuardrailDecision(False, "outside the maintenance window", mode, True)
    if str(context.health_status).upper() in {"HEALTH_ERR", "CRITICAL", "UNKNOWN"}:
        return GuardrailDecision(False, "health floor blocks autonomous execution", mode, True)
    return GuardrailDecision(True, "all limited-autopilot guardrails passed", mode)


def operator_snapshot(context: GuardrailContext) -> dict:
    """Return a redaction-free, UI-safe explanation of current controls."""
    decision = evaluate_guardrails(context)
    return {
        "mode": decision.mode.value,
        "action_id": context.action_id,
        "classification": str(context.classification).upper(),
        "decision": "ALLOWED" if decision.allowed else "BLOCKED",
        "reason": decision.reason,
        "requires_approval": decision.requires_approval,
        "controls": {
            "kill_switch": context.kill_switch,
            "cluster_enabled": context.cluster_enabled,
            "allowlisted": context.allowlisted,
            "maintenance_window_open": context.maintenance_window_open,
            "cooldown_active": context.cooldown_active,
            "health_status": context.health_status,
            "target_count": context.target_count,
            "max_targets": context.max_targets,
            "actions_used": context.actions_used,
            "action_budget": context.action_budget,
        },
    }
