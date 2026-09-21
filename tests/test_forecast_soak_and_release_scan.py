from datetime import datetime, timedelta, timezone

from scripts.forecast_benchmark import Point, run_forecast_benchmark, run_benchmark
from scripts.forecast_release_scan import scan_forecast_release
from shared.canary_soak import evaluate_shadow_soak


def test_forecast_benchmark_covers_multiple_forecast_models():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    points = [Point(start + timedelta(hours=i), float(i % 24)) for i in range(72)]
    result = run_forecast_benchmark(points)
    assert {row.model for row in result} == {"naive", "seasonal_naive", "linear"}
    report = run_benchmark(points)
    assert "candidate_d_isolation" in {row["detector"] for row in report["results"]}
    assert len(report["forecast_results"]) == 3


def test_shadow_soak_is_fail_closed_until_duration_and_evidence_exist():
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    row = {
        "active_evaluated": 25, "candidate_evaluated": 25,
        "latest_target_at": now - timedelta(hours=30),
        "status": "PROMISING", "execution_mode": "SHADOW_ONLY",
        "candidate_drift_status": "STABLE", "resource_budget_ok": True,
    }
    recent = dict(row, latest_target_at=now - timedelta(hours=1))
    report = evaluate_shadow_soak([row, recent], minimum_evaluations=20, minimum_duration_hours=24, now=now)
    assert report["status"] == "PASS"
    assert report["side_effects"].startswith("read-only")


def test_release_scan_keeps_optional_benchmark_packages_out_of_production():
    report = scan_forecast_release(".")
    assert report["status"] == "PASS"
    assert report["resource_policy"]["model_state"] == "JSON-only"
