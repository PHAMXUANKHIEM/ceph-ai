from pathlib import Path

from scripts.forecast_benchmark import load_series, run_benchmark


def test_benchmark_loads_anonymized_nab_like_series_and_labels_events():
    path = Path(__file__).parents[1] / "docs" / "benchmark" / "ceph-node-cpu-anonymized.csv"
    points = load_series(path)
    assert len(points) == 72
    assert sum(point.anomaly for point in points) == 3


def test_benchmark_is_offline_and_reports_river_and_optional_pyod():
    path = Path(__file__).parents[1] / "docs" / "benchmark" / "ceph-node-cpu-anonymized.csv"
    report = run_benchmark(load_series(path), history_size=12)
    assert report["format"] == "NAB-like"
    assert report["side_effects"].startswith("read-only")
    names = {row["detector"] for row in report["results"]}
    assert {"robust_baseline", "river_half_space_trees"} <= names
    assert "pyod_iforest" in names or "pyod_iforest" in report["unavailable"]
    for row in report["results"]:
        assert row["cpu_time_ms"] >= 0
