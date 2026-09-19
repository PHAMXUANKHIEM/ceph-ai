from pathlib import Path

from scripts.forecast_benchmark import load_series, run_benchmark


DATASET = Path(__file__).parents[1] / "docs" / "benchmark" / "ceph-node-cpu-anonymized.csv"


def test_anonymized_dataset_is_nab_like_and_benchmark_is_read_only():
    points = load_series(DATASET)

    assert len(points) == 72
    assert sum(point.anomaly for point in points) == 3

    report = run_benchmark(points, history_size=24)

    assert report["format"] == "NAB-like"
    assert report["side_effects"].startswith("read-only")
    detectors = {row["detector"] for row in report["results"]}
    assert {"robust_baseline", "river_half_space_trees"} <= detectors
    for row in report["results"]:
        assert 0 <= row["cpu_time_ms"]
        assert row["precision"] is None or 0 <= row["precision"] <= 1
        assert row["recall"] is None or 0 <= row["recall"] <= 1


def test_nab_label_windows_are_merged_with_point_labels(tmp_path):
    source = tmp_path / "series.csv"
    source.write_text(
        "timestamp,value,anomaly\n"
        "2026-01-01T00:00:00Z,1,0\n"
        "2026-01-01T01:00:00Z,2,0\n"
        "2026-01-01T02:00:00Z,3,0\n",
        encoding="utf-8",
    )
    labels = tmp_path / "labels.csv"
    labels.write_text(
        "window_start,window_end\n"
        "2026-01-01T01:00:00Z,2026-01-01T01:00:00Z\n",
        encoding="utf-8",
    )

    points = load_series(source, labels)

    assert [point.anomaly for point in points] == [False, True, False]
