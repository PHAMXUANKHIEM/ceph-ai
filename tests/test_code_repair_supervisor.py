import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from worker import code_repair_supervisor as supervisor
from worker.code_repair_supervisor import Cursor, read_new_errors


def test_first_start_ignores_historical_errors(tmp_path):
    log = tmp_path / "ceph-ai-worker.log"
    log.write_text("ERROR historical\n")
    cursors = {}
    assert read_new_errors([log], cursors, initialize_at_end=True) == []
    log.write_text(log.read_text() + "ERROR new failure\n")
    found = read_new_errors([log], cursors, initialize_at_end=False)
    assert len(found) == 1
    assert "new failure" in found[0]


def test_rotation_reads_replacement_from_start(tmp_path):
    log = tmp_path / "ceph-ai-dashboard.log"
    log.write_text("ERROR after rotation\n")
    cursors = {str(log): Cursor(inode=-1, offset=999)}
    found = read_new_errors([log], cursors, initialize_at_end=False)
    assert "after rotation" in found[0]


def test_supervisor_ignores_its_own_log(tmp_path):
    log = tmp_path / "ceph-ai-code-repair-supervisor.log"
    log.write_text("ERROR recursive failure\n")
    assert read_new_errors([log], {}, initialize_at_end=False) == []


def test_large_backlog_skips_stale_error_and_reads_fresh_tail(tmp_path):
    log = tmp_path / "ceph-ai-watcher.log"
    stale = "ERROR stale detached instance\n"
    log.write_text(stale + ("routine chatter\n" * 30_000) + "ERROR fresh failure\n")
    cursors = {str(log): Cursor(log.stat().st_ino, 0)}

    found = read_new_errors([log], cursors, initialize_at_end=False)

    assert len(found) == 1
    assert "fresh failure" in found[0]
    assert "stale detached instance" not in found[0]
    assert cursors[str(log)].offset == log.stat().st_size


def test_ceph_learning_uses_same_test_deploy_pipeline(monkeypatch, tmp_path):
    verification = supervisor.ceph_learning.VerificationResult("VERIFIED", "ok", (), True)
    candidate = supervisor.ceph_learning.LearningCandidate("f1", "key1", "CEPH evidence", verification)
    captured = {}
    monkeypatch.setattr(supervisor, "read_new_errors", lambda *a, **k: [])
    monkeypatch.setattr(supervisor.ceph_learning, "load_state", lambda p: {"initialized": True, "findings": {}})
    monkeypatch.setattr(supervisor.ceph_learning, "save_state", lambda *a: None)
    monkeypatch.setattr(supervisor.ceph_learning, "next_candidate", lambda seen: candidate)
    monkeypatch.setattr(supervisor.ceph_learning, "mark", lambda state, item, status, **kwargs: captured.setdefault("statuses", []).append(status))
    monkeypatch.setattr(supervisor.settings, "code_repair_cursor_file", str(tmp_path / "cursor.json"))
    monkeypatch.setattr(supervisor.settings, "ceph_capability_learning_state_file", str(tmp_path / "learning.json"))
    monkeypatch.setattr(supervisor.settings, "ceph_capability_learning_enabled", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_auto_enabled", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_push", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_deploy_staging", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_promote_main", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_max_attempts", 3)
    monkeypatch.setattr(supervisor.settings, "code_repair_running_stale_seconds", 3600)

    def fake_run(evidence, config):
        captured["evidence"] = evidence
        captured["config"] = config
        return SimpleNamespace(status="PROMOTED", fingerprint="fp")

    monkeypatch.setattr(supervisor, "run_repair", fake_run)
    supervisor.run_forever(max_iterations=1)

    assert captured["evidence"] == "CEPH evidence"
    assert captured["config"].task_kind == "ceph-capability-learning"
    assert captured["config"].push is True
    assert captured["config"].deploy_staging is True
    assert captured["config"].promote_main is True
    assert captured["statuses"] == ["RUNNING", "LEARNED"]


def test_nightly_improvement_runs_once_and_uses_test_deploy_pipeline(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    notifications = []
    captured = {}
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_hour", 0, raising=False)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_minute", 0, raising=False)
    monkeypatch.setattr(supervisor.settings, "code_repair_push", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_deploy_staging", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_promote_main", True)
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: "")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", notifications.append)

    def fake_run(evidence, config, *, force):
        captured.update({"evidence": evidence, "config": config, "force": force})
        return SimpleNamespace(
            status="PROMOTED", fingerprint="fp", branch="ai-repair/nightly", commit="abc",
            changed_files=["shared/ai.py"], review_rounds=1, error=None,
        )

    monkeypatch.setattr(supervisor, "run_repair", fake_run)
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)  # 00:00 Asia/Ho_Chi_Minh

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is True
    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is False
    assert captured["evidence"] == supervisor.NIGHTLY_IMPROVEMENT_EVIDENCE
    assert captured["force"] is True
    assert captured["config"].task_kind == "nightly-ai-improvement"
    assert captured["config"].allow_no_change is True
    assert captured["config"].test_command == supervisor.NIGHTLY_REGRESSION_TEST_COMMAND
    assert captured["config"].candidate_test_command == supervisor.NIGHTLY_REGRESSION_TEST_COMMAND
    assert captured["config"].require_changed_tests is True
    assert captured["config"].max_ai_attempts == 1
    assert captured["config"].timeout_seconds == supervisor.NIGHTLY_AI_STEP_TIMEOUT_SECONDS
    assert captured["config"].push is True
    assert captured["config"].deploy_staging is True
    assert captured["config"].promote_main is True
    assert json.loads(state_path.read_text())["status"] == "PROMOTED"
    assert len(notifications) == 2


def test_nightly_due_is_idempotent_when_systemd_starts_late():
    now = datetime(2026, 8, 30, 20, 15, tzinfo=timezone.utc)

    assert supervisor._nightly_due({}, now) is True
    assert supervisor._nightly_due({"last_run_date": "2026-08-31"}, now) is False


def test_nightly_due_retries_an_interrupted_or_failed_run():
    now = datetime(2026, 8, 30, 20, 15, tzinfo=timezone.utc)
    for status in ("RUNNING", "FAILED"):
        assert supervisor._nightly_due({"last_run_date": "2026-08-31", "status": status}, now) is True


def test_nightly_dashboard_override_is_scoped_to_today(monkeypatch):
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_date", "2026-08-31", raising=False)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_enabled", True, raising=False)
    assert supervisor.nightly_override_for_today(now) is True
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_enabled", False, raising=False)
    assert supervisor.nightly_override_for_today(now) is False
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_date", "2026-08-30", raising=False)
    assert supervisor.nightly_override_for_today(now) is None


def test_repair_execution_lock_serializes_timer_and_supervisor(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        supervisor.settings,
        "code_repair_run_lock_file",
        str(tmp_path / "repair-run.lock"),
        raising=False,
    )
    monkeypatch.setattr(
        supervisor,
        "run_repair",
        lambda evidence, config, *, force=False: calls.append((evidence, force)) or "ok",
    )

    assert supervisor.run_repair_exclusively("evidence", object(), force=True) == "ok"
    assert calls == [("evidence", True)]


def test_nightly_failure_is_persisted_and_notified(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    notifications = []
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: "")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", notifications.append)
    monkeypatch.setattr(supervisor, "run_repair", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is False

    state = json.loads(state_path.read_text())
    assert "last_run_date" not in state
    assert state["status"] == "FAILED"
    assert "boom" in state["error"]
    assert len(notifications) == 2


def test_nightly_failed_pipeline_is_left_retryable(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: "")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", lambda message: None)
    monkeypatch.setattr(
        supervisor,
        "run_repair",
        lambda *args, **kwargs: SimpleNamespace(
            status="FAILED", fingerprint="fp", branch="ai-repair/nightly", commit=None,
            changed_files=[], review_rounds=0, error="candidate gate failed",
        ),
    )
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is False

    state = json.loads(state_path.read_text())
    assert state["status"] == "FAILED"
    assert "last_run_date" not in state


def test_nightly_dirty_checkout_stops_before_ai(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    notifications = []
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: " M compose.yaml")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", notifications.append)
    monkeypatch.setattr(supervisor, "run_repair", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")))
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is True

    state = json.loads(state_path.read_text())
    assert state["status"] == "BLOCKED_DIRTY_CHECKOUT"
    assert state["last_run_date"] == "2026-08-31"
    assert notifications and "chưa commit" in notifications[0]


def test_nightly_dashboard_override_allows_dirty_checkout(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    notifications = []
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: " M dashboard/app.py")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", notifications.append)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_date", "2026-08-31", raising=False)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_enabled", True, raising=False)
    monkeypatch.setattr(
        supervisor,
        "run_repair",
        lambda *args, **kwargs: SimpleNamespace(
            status="NO_CHANGE", fingerprint="fp", branch=None, commit=None,
            changed_files=[], review_rounds=0, error=None,
        ),
    )

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is True
    assert json.loads(state_path.read_text())["status"] == "NO_CHANGE"
    assert "cho phép chạy hôm nay" in notifications[0]
