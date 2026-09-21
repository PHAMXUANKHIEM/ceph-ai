"""Fail-closed rollback planning for remediation cases."""
from __future__ import annotations

from dataclasses import dataclass

from worker.policy.playbook_registry import resolve_case_rollback


@dataclass(frozen=True)
class RollbackPlan:
    supported: bool
    executable: bool
    original_action_id: str
    rollback_action_id: str | None
    reason: str
    requires_approval: bool = True
    evidence: dict | None = None


def plan_case_rollback(
    *, action_id: str, playbook_version: str, contract_snapshot: dict | None,
    inverse_tested: bool, operator_approved: bool = False,
    classification: str | None = None,
) -> RollbackPlan:
    """Return a plan, never a command.

    The inverse_tested flag must be explicit evidence supplied by a reviewed
    playbook test. Destructive/data-repair actions remain unsupported unless a
    future policy explicitly adds a dedicated safe inverse contract.
    """
    evidence = {
        "contract_frozen": isinstance(contract_snapshot, dict),
        "inverse_tested": bool(inverse_tested),
        "operator_approved": bool(operator_approved),
    }
    if str(classification or "").upper() in {"DESTRUCTIVE", "DATA_REPAIR"}:
        return RollbackPlan(
            False, False, action_id, None,
            "destructive or data-repair action has no assumed inverse", evidence=evidence,
        )
    rollback_id, error = resolve_case_rollback(
        action_id=action_id, playbook_version=playbook_version,
        contract_snapshot=contract_snapshot,
    )
    if error:
        return RollbackPlan(False, False, action_id, None, error, evidence=evidence)
    if not inverse_tested:
        return RollbackPlan(
            False, False, action_id, rollback_id,
            "registered inverse lacks tested rollback evidence", evidence=evidence,
        )
    if not operator_approved:
        return RollbackPlan(
            True, False, action_id, rollback_id,
            "tested inverse is available; operator approval is still required",
            evidence=evidence,
        )
    return RollbackPlan(
        True, True, action_id, rollback_id,
        "tested inverse is available and operator approval is present", evidence=evidence,
    )
