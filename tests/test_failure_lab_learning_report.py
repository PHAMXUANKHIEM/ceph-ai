"""FL6.5 learning reports must not promote incomplete lab evidence."""

import json

from shared.failure_lab_learning_report import STAGES, summarize, telegram_summary


def _record(directory, name, *, family="osd_down", status="DONE", started="2026-10-13T01:00:00+00:00",
            diagnosis=False, recovery=True, include_stages=True):
    stages = {stage: True for stage in STAGES}
    stages.update(diagnosis=diagnosis, recovery=recovery)
    record = {"id": name, "family": family, "status": status, "run_started_at": started}
    if include_stages:
        record["stages"] = stages
        record["passed"] = all(stages.values())
    (directory / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


def test_summarizes_only_completed_scored_lab_runs(tmp_path):
    _record(tmp_path, "repro-0000000001")
    _record(tmp_path, "repro-0000000002", started="2026-10-13T02:00:00+00:00", diagnosis=True)
    _record(tmp_path, "repro-0000000003", status="FAILED", diagnosis=True)
    _record(tmp_path, "repro-0000000004", include_stages=False)
    _record(tmp_path, "repro-0000000005", family="nearfull", diagnosis=True)

    report = summarize(tmp_path)["osd_down"]

    assert report["completed_runs"] == 2
    assert report["passed_runs"] == 1
    assert report["stage_passes"]["diagnosis"] == 1
    assert report["failed_execution_runs"] == 1
    assert report["unscored_done_runs"] == 1
    assert report["first_correct_attempt"] == 2
    assert report["seconds_from_first_run_to_first_correct"] == 3600
    assert report["production_reference_effect"] == "not_measured"
    assert set(summarize(tmp_path, family="nearfull")) == {"nearfull"}
    assert "production" in telegram_summary(report)


def test_missing_or_malformed_records_never_count_as_success(tmp_path):
    (tmp_path / "repro-corrupt.json").write_text("not json", encoding="utf-8")
    _record(tmp_path, "repro-0000000001", include_stages=False)
    _record(tmp_path, "repro-0000000002", diagnosis=True)
    path = tmp_path / "repro-0000000002.json"
    inconsistent = json.loads(path.read_text(encoding="utf-8"))
    inconsistent["passed"] = False
    path.write_text(json.dumps(inconsistent), encoding="utf-8")
    assert summarize(tmp_path)["osd_down"]["passed_runs"] == 0
    assert summarize(tmp_path)["osd_down"]["first_correct_attempt"] is None
    assert summarize(tmp_path / "absent") == {}
