from datetime import datetime, timedelta, timezone

from shared.online_learning_evaluator import evaluate_rows


def _row(scope="cluster-a", host="node-1", metric="cpu", index=0, actual=42.0):
    observed_at = datetime(2026, 9, 20, tzinfo=timezone.utc) + timedelta(hours=index)
    return {
        "cluster_id": scope,
        "host": host,
        "metric": metric,
        "sample_id": f"{scope}-{host}-{metric}-{index}",
        "observed_at": observed_at.isoformat(),
        "predicted": actual - 1,
        "actual": actual,
        "alert": False,
        "quality_status": "OK",
    }


def test_evaluator_keeps_scopes_separate_and_computes_metrics():
    rows = [_row(index=index) for index in range(10)]
    rows += [_row(host="node-2", index=index, actual=60.0) for index in range(10)]
    report = evaluate_rows(
        rows, now=datetime(2026, 9, 20, 10, tzinfo=timezone.utc),
        max_age_hours=24, minimum_samples=10,
    )
    assert report.quality.status == "PASS"
    assert [item.scope_key for item in report.scopes] == [
        "cluster-a|node-1|cpu", "cluster-a|node-2|cpu",
    ]
    assert all(item.evaluated == 10 and item.mae == 1.0 and item.alert_volume == 0
               for item in report.scopes)


def test_evaluator_fails_closed_on_missing_scope_duplicate_gap_and_range():
    rows = [_row(index=index) for index in range(2)]
    rows.append({**_row(index=1), "sample_id": "duplicate"})
    rows.append({**_row(index=1), "sample_id": "duplicate", "predicted": 101})
    rows.append(_row(index=8))
    rows.append({**_row(index=9), "host": ""})
    report = evaluate_rows(
        rows, now=datetime(2026, 9, 20, 10, tzinfo=timezone.utc),
        max_age_hours=24, max_gap_hours=2,
    )
    assert report.quality.status == "FAIL"
    assert report.quality.missing_fields["host"] == 1
    assert report.quality.duplicate_count == 1
    assert report.quality.out_of_range_count == 1
    assert report.quality.gap_count == 1
    assert report.quality.promotion_safe is False


def test_evaluator_does_not_require_or_write_database():
    report = evaluate_rows(
        [_row(index=0)],
        now=datetime(2026, 9, 20, 1, tzinfo=timezone.utc),
        minimum_samples=2,
    )
    assert report.quality.status == "PASS"
    assert report.scopes[0].status == "INSUFFICIENT_DATA"


def test_evaluator_compares_active_and_candidate_without_promoting():
    rows = [
        {**_row(index=index), "model_role": "active"}
        for index in range(10)
    ] + [
        {**_row(index=index, actual=42.0), "predicted": 42.0, "model_role": "candidate"}
        for index in range(10)
    ]
    report = evaluate_rows(
        rows, now=datetime(2026, 9, 20, 10, tzinfo=timezone.utc),
        max_age_hours=24, minimum_samples=10,
    )
    assert len(report.comparisons) == 1
    assert report.comparisons[0]["mae_delta"] == -1.0
    assert report.comparisons[0]["status"] == "READY"
