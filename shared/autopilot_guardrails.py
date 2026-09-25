"""Pure guardrail evaluator for the three-level Safe Autopilot contract."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
    if isinstance(value, AutopilotMode):
        return value
    normalized = str(value).upper()
    # LEGACY is the persisted compatibility value for clusters created before
    # explicit per-cluster modes. The caller resolves its boolean gates first.
    if normalized == "LEGACY":
        return AutopilotMode.LIMITED_AUTOPILOT
    return AutopilotMode(normalized)


def resolve_mode(*, global_enabled: bool, cluster_enabled: bool,
                 advisory_only: bool = False) -> AutopilotMode:
    """Derive the operator-visible mode without granting execution rights."""
    if advisory_only:
        return AutopilotMode.ADVISORY
    if global_enabled and cluster_enabled:
        return AutopilotMode.LIMITED_AUTOPILOT
    return AutopilotMode.APPROVAL_REQUIRED


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
