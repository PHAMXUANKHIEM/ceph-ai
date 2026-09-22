"""Fail-closed acceptance gate for self-learning promotion readiness."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


REQUIRED_LIFECYCLE = frozenset({"CANDIDATE", "SHADOW", "ACTIVE", "BLOCKED", "RETIRED", "ROLLBACK"})


@dataclass(frozen=True)
class AcceptanceDecision:
    allowed: bool
    status: str
    failed_checks: tuple[str, ...]
    reason: str


def evaluate_acceptance(
    *,
    lifecycle_states: Iterable[str],
    scopes: Iterable[Mapping[str, object]],
    replay_days: float,
    canary_hours: float,
    resource_budget_ok: bool,
    rollback_verified: bool,
    immutable_artifact: bool,
    operator_signoff: bool,
    security_signoff: bool,
    operations_signoff: bool,
    verified_outcomes: int,
    production_mode: str = "SHADOW_ONLY",
) -> AcceptanceDecision:
    checks: dict[str, bool] = {
        "lifecycle_states": REQUIRED_LIFECYCLE <= {str(item).upper() for item in lifecycle_states},
        "scope_dimensions": all(
            all(scope.get(field) not in (None, "") for field in
                ("cluster_id", "entity_id", "metric", "horizon_hours"))
            for scope in scopes
        ),
        "replay_14_days": replay_days >= 14.0,
        "canary_72_hours": canary_hours >= 72.0,
        "resource_budget": bool(resource_budget_ok),
        "rollback": bool(rollback_verified and immutable_artifact),
        "operator_signoff": bool(operator_signoff),
        "security_signoff": bool(security_signoff),
        "operations_signoff": bool(operations_signoff),
        "production_shadow_only_without_outcomes": (
            production_mode == "SHADOW_ONLY" or verified_outcomes > 0
        ),
    }
    failed = tuple(name for name, passed in checks.items() if not passed)
    if failed:
        return AcceptanceDecision(False, "BLOCKED", failed, "acceptance gate failed: " + ", ".join(failed))
    return AcceptanceDecision(True, "READY_FOR_OPERATOR_REVIEW", (), "all acceptance evidence is present")
