from datetime import datetime, timedelta, timezone

from shared.delayed_feedback_evaluator import (
    FeedbackStatus,
    classify_feedback,
    compare_estimate_with_verified,
    estimate_performance,
)


NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)


def _row(index, *, actual=None, status=None):
    return {
        "scope_key": "cluster-a|node-1|cpu",
        "sample_id": f"sample-{index}",
        "observed_at": (NOW - timedelta(hours=index)).isoformat(),
        "predicted": 40.0,
        "actual": actual,
        "feedback_status": status,
    }


def test_delayed_feedback_classifies_pending_and_overdue_no_label():
    assert classify_feedback(_row(1), now=NOW).status == FeedbackStatus.PENDING_LABEL
    assert classify_feedback(_row(12), now=NOW).status == FeedbackStatus.NO_LABEL


def test_delayed_feedback_requires_verified_coverage_before_estimation():
    rows = [_row(1, actual=41.0), _row(2, actual=70.0)] + [_row(index) for index in range(3, 5)]
    result = estimate_performance(rows, now=NOW, minimum_verified=2, minimum_coverage=0.6)
    assert result[0].status == "INSUFFICIENT_CONFIDENCE"
    assert result[0].estimated_mae is None
    assert result[0].promotion_allowed is False


def test_delayed_feedback_ready_is_still_evidence_only_and_scope_isolated():
    rows = [_row(index, actual=41.0) for index in range(1, 5)]
    rows += [{**_row(index, actual=41.0), "scope_key": "cluster-a|node-2|cpu"} for index in range(1, 5)]
    result = estimate_performance(rows, now=NOW, minimum_verified=3, minimum_coverage=0.6)
    assert [item.scope_key for item in result] == [
        "cluster-a|node-1|cpu", "cluster-a|node-2|cpu",
    ]
    assert all(item.status == "READY" and not item.promotion_allowed for item in result)


def test_delayed_estimate_is_reconciled_when_verified_labels_arrive():
    comparison = compare_estimate_with_verified(
        {"cluster-a|node-1|cpu": 1.0},
        [_row(1, actual=41.0), _row(2, actual=42.0)],
        now=NOW,
        tolerance=1.0,
    )
    assert comparison[0].verified_count == 2
    assert comparison[0].verified_mae == 1.5
    assert comparison[0].absolute_delta == 0.5
    assert comparison[0].status == "MATCH"
