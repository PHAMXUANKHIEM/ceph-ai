import json
from datetime import datetime, timezone

from worker import nightly_ai_report


def test_build_morning_report_includes_work_and_recommendations():
    message = nightly_ai_report.build_morning_report({
        "last_run_date": "2026-09-16",
        "status": "PATCH_READY",
        "changed_files": ["worker/example.py"],
        "candidate_worktree": "/var/lib/ceph-ai/nightly-ai-improvement-candidates/ai-repair/repo",
        "analysis_reports": 2,
        "analysis_failures": [],
        "analysis_report_previews": ["Ưu tiên thêm regression test cho budget guard."],
    }, now=datetime(2026, 9, 16, 1, 30, tzinfo=timezone.utc))

    assert "worker/example.py" in message
    assert "Ưu tiên thêm regression test" in message
    assert "commit/push thủ công" in message
    assert message.index("Đề xuất nên làm hôm nay:") > message.index("Đề xuất từ analyst:")
    assert len(message) <= nightly_ai_report.MAX_TELEGRAM_CHARS


def test_failed_retryable_state_uses_finished_date():
    message = nightly_ai_report.build_morning_report({
        "status": "FAILED",
        "finished_at": "2026-09-15T18:50:30+00:00",
        "error": "test gate failed",
    }, now=datetime(2026, 9, 16, 1, 30, tzinfo=timezone.utc))

    assert "Kết quả job: 2026-09-16" in message
    assert "test gate failed" in message


def test_main_sends_once_and_marks_state(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    state_path.write_text(json.dumps({"last_run_date": "2026-09-16", "status": "NO_CHANGE"}))
    report_state_path = nightly_ai_report._report_state_path(state_path)
    saved = {}
    monkeypatch.setattr(
        nightly_ai_report, "_load_state",
        lambda path: json.loads(path.read_text()) if path.exists() else {},
    )
    monkeypatch.setattr(
        nightly_ai_report, "_save_state",
        lambda path, value: saved.__setitem__(str(path), value),
    )
    monkeypatch.setattr(nightly_ai_report.settings if hasattr(nightly_ai_report, "settings") else __import__("config.settings", fromlist=["settings"]).settings, "ai_nightly_improvement_state_file", str(state_path), raising=False)
    sent = []
    monkeypatch.setattr("shared.telegram_alerts.send_code_repair_alert", lambda message: sent.append(message) or True)

    assert nightly_ai_report.main() == 0
    assert len(sent) == 1
    expected_date = datetime.now(timezone.utc).astimezone(nightly_ai_report.NIGHTLY_TIMEZONE).date().isoformat()
    assert saved[str(report_state_path)]["morning_report_date"] == expected_date


def test_long_previews_do_not_hide_recommendation():
    message = nightly_ai_report.build_morning_report({
        "status": "PATCH_READY",
        "changed_files": [f"worker/{index}-very-long-file-name.py" for index in range(12)],
        "candidate_worktree": "/var/lib/ceph-ai/nightly-ai-improvement-candidates/candidate/repo",
        "error": "x" * 700,
        "analysis_report_previews": ["y" * 1200] * 3,
    })

    assert len(message) <= nightly_ai_report.MAX_TELEGRAM_CHARS
    assert "Đề xuất nên làm hôm nay:" in message
    assert "commit/push thủ công" in message
