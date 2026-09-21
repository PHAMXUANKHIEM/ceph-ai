"""Server-owned safety contract for RGW/Block/Vitastor writes.

This is a planning/preflight layer. It deliberately does not build or run
shell commands; execution remains in the existing RBAC/policy/executor path.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ControlledAction:
    action_id: str
    domain: str
    classification: str
    max_targets: int
    shadow_required: bool = True
    requires_approval: bool = True


CONTROLLED_ACTIONS = {
    "reshard_rgw_bucket": ControlledAction("reshard_rgw_bucket", "RGW", "RISKY", 1),
    "rgw_user_suspend": ControlledAction("rgw_user_suspend", "RGW", "DESTRUCTIVE", 1),
    "rgw_bucket_policy_update": ControlledAction("rgw_bucket_policy_update", "RGW", "RISKY", 1),
    "rbd_resize_volume": ControlledAction("rbd_resize_volume", "BLOCK", "RISKY", 1),
    "rbd_trash_move_volume": ControlledAction("rbd_trash_move_volume", "BLOCK", "RISKY", 1),
    "rbd_trash_restore_volume": ControlledAction("rbd_trash_restore_volume", "BLOCK", "RISKY", 1),
    "cinder_attach_volume": ControlledAction("cinder_attach_volume", "BLOCK", "RISKY", 1),
    "cinder_detach_volume": ControlledAction("cinder_detach_volume", "BLOCK", "RISKY", 1),
    "vitastor_resize_volume": ControlledAction("vitastor_resize_volume", "VITASTOR", "RISKY", 1),
    "vitastor_snapshot_create": ControlledAction("vitastor_snapshot_create", "VITASTOR", "RISKY", 1),
    "vitastor_snapshot_delete": ControlledAction("vitastor_snapshot_delete", "VITASTOR", "DESTRUCTIVE", 1),
}


@dataclass(frozen=True)
class ControlledActionDecision:
    allowed: bool
    action: ControlledAction | None
    reason: str
    execution_command: None = None
    audit_required: bool = True


def evaluate_controlled_action(
    action_id: str, *, target_count: int, approved: bool,
    capability_verified: bool, target_verified: bool,
    fresh_telemetry: bool, shadow_passed: bool,
) -> ControlledActionDecision:
    action = CONTROLLED_ACTIONS.get(str(action_id).strip())
    if action is None:
        return ControlledActionDecision(False, None, "action is not in the controlled allowlist")
    if target_count < 1 or target_count > action.max_targets:
        return ControlledActionDecision(False, action, "target count exceeds controlled-action blast radius")
    if not capability_verified or not target_verified:
        return ControlledActionDecision(False, action, "capability and target preflight are required")
    if not fresh_telemetry:
        return ControlledActionDecision(False, action, "fresh telemetry is required before a write")
    if action.shadow_required and not shadow_passed:
        return ControlledActionDecision(False, action, "shadow/canary evidence is required")
    if action.requires_approval and not approved:
        return ControlledActionDecision(False, action, "operator approval is required")
    return ControlledActionDecision(True, action, "preflight, shadow and approval gates passed")


def allowed_domains() -> tuple[str, ...]:
    return tuple(sorted({item.domain for item in CONTROLLED_ACTIONS.values()}))
