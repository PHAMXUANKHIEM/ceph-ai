from datetime import datetime

from shared.models import ForecastModelEvaluation
from dashboard.routes.ai_learning import _model_quality_summary


def test_model_quality_summary_is_bounded_and_separates_drift_budget():
    rows = [
        ForecastModelEvaluation(
            active_evaluated=20, candidate_evaluated=18,
            active_mae=4.0, candidate_mae=3.0,
            active_smape=10.0, candidate_smape=8.0,
            candidate_bias=-0.5, evidence_json='{"candidate_drift_status":"DRIFT","resource_budget_ok":false}',
            target_at=datetime(2026, 1, 1), evaluated_at=datetime(2026, 1, 2),
            status="HOLD", reason="test",
            candidate_model_id="candidate", active_model_id="active",
        ),
    ]
    result = _model_quality_summary(rows)
    assert result["paired_outcomes"] == 18
    assert result["candidate_mae"] == 3.0
    assert result["drifted"] == 1
    assert result["resource_budget_failures"] == 1
