from pathlib import Path


def test_forecast_soak_report_is_read_only_and_has_explicit_gate():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "forecast_soak_report.py").read_text(encoding="utf-8")
    assert "compare_persisted_forecast_runs" in source
    assert "evaluate_shadow_soak" in source
    assert "session.commit" not in source
    assert "read_only" in source
