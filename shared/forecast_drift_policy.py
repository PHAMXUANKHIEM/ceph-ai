"""Pure, fail-closed lifecycle policy for forecast drift evidence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DriftPolicyDecision:
    candidate_status: str
    confidence_multiplier: float
    promotion_allowed: bool
    evaluation_window_multiplier: float
    workload_changed: bool
    requires_operator_review: bool
    model_reset_allowed: bool
    reason: str


def apply_drift_policy(
    *, residual_status: str,
    mae_status: str,
    metric_status: str,
    consecutive_good_outcomes: int,
    required_good_outcomes: int = 3,
) -> DriftPolicyDecision:
    """Translate evidence into bounded lifecycle intent, never a side effect."""
    statuses = {str(residual_status), str(mae_status), str(metric_status)}
    required = max(1, int(required_good_outcomes))
    if "DRIFT" in {str(residual_status), str(mae_status)}:
        mae_drift = str(mae_status) == "DRIFT"
        return DriftPolicyDecision(
            candidate_status="DRIFT",
            confidence_multiplier=0.5,
            promotion_allowed=False,
            evaluation_window_multiplier=2.0 if mae_drift else 1.0,
            workload_changed=str(metric_status) == "DRIFT",
            requires_operator_review=True,
            model_reset_allowed=False,
            reason="residual/MAE drift requires review; model reset and promotion remain blocked",
        )
    if str(metric_status) == "DRIFT":
        return DriftPolicyDecision(
            candidate_status="SHADOW",
            confidence_multiplier=0.75,
            promotion_allowed=False,
            evaluation_window_multiplier=1.0,
            workload_changed=True,
            requires_operator_review=True,
            model_reset_allowed=False,
            reason="metric drift indicates workload changed; it does not prove model failure",
        )
    if "INSUFFICIENT_DATA" in statuses or "UNKNOWN" in statuses:
        return DriftPolicyDecision(
            candidate_status="SHADOW",
            confidence_multiplier=0.5,
            promotion_allowed=False,
            evaluation_window_multiplier=1.0,
            workload_changed=False,
            requires_operator_review=False,
            model_reset_allowed=False,
            reason="drift evidence is incomplete; keep candidate shadow-only",
        )
    eligible = int(consecutive_good_outcomes) >= required
    return DriftPolicyDecision(
        candidate_status="ELIGIBLE" if eligible else "SHADOW",
        confidence_multiplier=1.0,
        promotion_allowed=eligible,
        evaluation_window_multiplier=1.0,
        workload_changed=False,
        requires_operator_review=False,
        model_reset_allowed=False,
        reason=("enough consecutive good outcomes" if eligible else "warm-up outcomes are insufficient"),
    )
