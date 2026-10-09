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


def test_nightly_creates_plan_only_and_never_calls_repair_pipeline(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    notifications = []
    captured = []
    monkeypatch.setattr(supervisor.settings, "code_repair_push", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_deploy_staging", True)
    monkeypatch.setattr(supervisor.settings, "code_repair_promote_main", True)
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: "")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", notifications.append)
    monkeypatch.setattr(supervisor, "run_repair", lambda *a, **k: (_ for _ in ()).throw(AssertionError("nightly must not implement")))
    monkeypatch.setattr(supervisor, "collect_nightly_multi_agent_analysis", lambda repo, evidence: captured.append(evidence) or (["system upgrade plan", "error review plan", "test plan"], []))
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)  # 00:00 Asia/Ho_Chi_Minh

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is True
    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is False
    assert captured == [supervisor.NIGHTLY_IMPROVEMENT_EVIDENCE]
    saved = json.loads(state_path.read_text())
    assert saved["status"] == "PLAN_READY"
    assert saved["mode"] == "PLAN_ONLY"
    assert saved["analysis_report_previews"] == ["system upgrade plan", "error review plan", "test plan"]
    assert saved["changed_files"] == []
    assert saved["commit"] is None
    assert saved["candidate_worktree"] is None
    assert notifications == []


def test_nightly_records_partial_plans_and_does_not_block_on_dirty_checkout(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: " M dashboard/app.py")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", lambda message: None)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_multi_agent_analysis_enabled", True, raising=False)
    monkeypatch.setattr(
        supervisor,
        "collect_nightly_multi_agent_analysis",
        lambda repo, evidence: (["[error_review / claude]\nbounded finding"], ["test_review: timeout"]),
    )
    monkeypatch.setattr(supervisor, "run_repair", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run repair")))
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is True
    saved = json.loads(state_path.read_text())
    assert saved["status"] == "PLAN_READY_WITH_WARNINGS"
    assert saved["mode"] == "PLAN_ONLY"
    assert saved["checkout_dirty"] is True
    assert saved["checkout_changes"] == [" M dashboard/app.py"]
    assert saved["analysis_status"] == "COMPLETED"
    assert saved["analysis_reports"] == 1
    assert saved["analysis_failures"] == ["test_review: timeout"]


def test_nightly_prompts_cover_upgrade_errors_and_tests_without_execution():
    assert {name for name, _ in supervisor.NIGHTLY_ANALYSTS} == {
        "system_upgrade", "error_review", "test_review",
    }
    prompt = supervisor._nightly_analyst_prompt("test_review", "coverage", "evidence")
    assert "planning-only" in prompt
    assert "execute tests" in prompt
    assert "Acceptance criteria" in prompt
    assert "do not claim that runtime incidents were reviewed" in prompt


def test_nightly_collects_bounded_redacted_recent_application_errors(tmp_path):
    log = tmp_path / "ceph-ai-worker.log"
    log.write_text("INFO healthy\nERROR request failed token=very-secret-value\nTraceback (most recent call last):\nRuntimeError: backend unavailable\n")

    evidence = supervisor._collect_recent_nightly_error_evidence(tmp_path)

    assert "ceph-ai-worker.log" in evidence
    assert "backend unavailable" in evidence
    assert "very-secret-value" not in evidence
    assert "<redacted>" in evidence


def test_nightly_passes_runtime_log_evidence_only_to_error_analyst(monkeypatch):
    monkeypatch.setattr(supervisor.settings, "ai_nightly_multi_agent_analysis_enabled", True, raising=False)
    monkeypatch.setattr(supervisor, "_collect_recent_nightly_error_evidence", lambda: "\nRUNTIME_ERROR_EVIDENCE")
    captured = {}

    def fake_analyst(repo, evidence, role, focus, **kwargs):
        captured[role] = evidence
        return role, f"[{role}] plan"

    monkeypatch.setattr(supervisor, "_run_nightly_analyst", fake_analyst)
    reports, failures = supervisor.collect_nightly_multi_agent_analysis(Path("."), "base plan")

    assert not failures
    assert len(reports) == len(supervisor.NIGHTLY_ANALYSTS)
    assert "RUNTIME_ERROR_EVIDENCE" in captured["error_review"]
    assert "RUNTIME_ERROR_EVIDENCE" not in captured["system_upgrade"]
    assert "RUNTIME_ERROR_EVIDENCE" not in captured["test_review"]


def test_nightly_analysis_redacts_assignment_and_json_secrets():
    text = 'TOKEN=super-secret "password":"another-secret" private_key:third-secret'

    redacted = supervisor._redact_nightly_text(text)

    assert "super-secret" not in redacted
    assert "another-secret" not in redacted
    assert "third-secret" not in redacted
    assert redacted.count("<redacted>") == 3


def test_nightly_analyst_uses_budget_guard_and_cli_telemetry(monkeypatch, tmp_path):
    budget_calls = []
    telemetry_calls = []

    monkeypatch.setattr(supervisor, "_role_account_dirs", lambda config, profile: (tmp_path, tmp_path))
    monkeypatch.setattr(supervisor, "_provider_command", lambda *args, **kwargs: ("claude", ["claude"]))
    monkeypatch.setattr(
        supervisor,
        "check_ai_budget",
        lambda provider, model, input_chars: budget_calls.append((provider, model, input_chars)) or "reservation-1",
    )
    monkeypatch.setattr(supervisor, "record_ai_attempt", lambda **values: telemetry_calls.append(values))

    def fake_run(args, **kwargs):
        if args[:3] == ["git", "worktree", "add"]:
            Path(args[4]).mkdir(parents=True)
            return SimpleNamespace(returncode=0, stdout="")
        if args == ["claude"]:
            return SimpleNamespace(returncode=0, stdout="bounded report")
        if args[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="")
        if args[:3] == ["git", "worktree", "remove"]:
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(args)

    monkeypatch.setattr(supervisor, "_run", fake_run)
    role, report = supervisor._run_nightly_analyst(
        tmp_path, "nightly evidence", "ai_product", "AI behavior",
        provider="claude", model="sonnet", account_profile="configured", timeout_seconds=30,
    )

    assert role == "ai_product"
    assert "bounded report" in report
    assert budget_calls == [("claude", "sonnet", budget_calls[0][2])]
    assert telemetry_calls[0]["reservation_id"] == "reservation-1"
    assert telemetry_calls[0]["feature"] == "nightly_multi_agent_analysis"
    assert telemetry_calls[0]["status"] == "SUCCESS"
    assert telemetry_calls[0]["output_chars"] == len("bounded report")


def test_a_codex_analyst_is_measured_by_its_final_answer_not_its_session(monkeypatch, tmp_path):
    telemetry_calls = []
    session_log = "exec rg ...\n" * 50_000 + "tail of the session"
    monkeypatch.setattr(supervisor, "_role_account_dirs", lambda config, profile: (tmp_path, tmp_path))
    monkeypatch.setattr(supervisor, "_provider_command", lambda *args, **kwargs: ("codex", ["codex", "exec", "-"]))
    monkeypatch.setattr(supervisor, "check_ai_budget", lambda *args: "reservation-1")
    monkeypatch.setattr(supervisor, "record_ai_attempt", lambda **values: telemetry_calls.append(values))
    seen = []

    def fake_run(args, **kwargs):
        if args[:3] == ["git", "worktree", "add"]:
            Path(args[4]).mkdir(parents=True)
            return SimpleNamespace(returncode=0, stdout="")
        if args[:2] == ["codex", "exec"]:
            seen.append(args)
            Path(args[args.index("--output-last-message") + 1]).write_text("Final plan: pin apt snapshot.")
            return SimpleNamespace(returncode=0, stdout=session_log)
        if args[:3] in (["git", "status", "--porcelain"], ["git", "worktree", "remove"]):
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(args)

    monkeypatch.setattr(supervisor, "_run", fake_run)
    _role, report = supervisor._run_nightly_analyst(
        tmp_path, "nightly evidence", "system_upgrade", "upgrades",
        provider="codex", model="", account_profile="configured", timeout_seconds=30,
    )

    assert seen[0][-1] == "-"  # the prompt still comes from stdin
    assert "Final plan: pin apt snapshot." in report and "exec rg" not in report
    assert telemetry_calls[0]["output_chars"] == len("Final plan: pin apt snapshot.")


def test_nightly_due_is_idempotent_when_systemd_starts_late():
    now = datetime(2026, 8, 30, 20, 15, tzinfo=timezone.utc)

    assert supervisor._nightly_due({}, now) is True
    assert supervisor._nightly_due({"last_run_date": "2026-08-31"}, now) is False


def test_nightly_due_retries_an_interrupted_or_failed_run():
    now = datetime(2026, 8, 30, 20, 15, tzinfo=timezone.utc)
    for status in ("RUNNING", "FAILED"):
        assert supervisor._nightly_due({"last_run_date": "2026-08-31", "status": status}, now) is True
    assert supervisor._nightly_due({"last_run_date": "2026-08-31", "status": "PLAN_INCOMPLETE"}, now) is True


def test_nightly_dashboard_override_is_scoped_to_today(monkeypatch):
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_date", "2026-08-31", raising=False)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_enabled", True, raising=False)
    assert supervisor.nightly_override_for_today(now) is True
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_enabled", False, raising=False)
    assert supervisor.nightly_override_for_today(now) is False
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_date", "2026-08-30", raising=False)
    assert supervisor.nightly_override_for_today(now) is None


def test_direct_nightly_call_honors_dashboard_skip_override(monkeypatch, tmp_path):
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_date", "2026-08-31", raising=False)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_enabled", False, raising=False)
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: (_ for _ in ()).throw(AssertionError("must not run")))

    assert supervisor.run_nightly_ai_improvement(tmp_path, tmp_path / "nightly.json", now=now) is False


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
    state_path.write_text(json.dumps({
        "last_run_date": "2026-08-30",
        "analysis_report_previews": ["stale yesterday plan"],
        "analysis_reports": 3,
        "changed_files": ["yesterday.py"],
    }))
    notifications = []
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: "")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", notifications.append)
    monkeypatch.setattr(supervisor, "collect_nightly_multi_agent_analysis", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is False

    state = json.loads(state_path.read_text())
    assert "last_run_date" not in state
    assert state["status"] == "FAILED"
    assert "boom" in state["error"]
    assert len(notifications) == 1
    assert state["analysis_report_previews"] == []
    assert state["analysis_reports"] == 0
    assert state["changed_files"] == []


def test_nightly_all_analysts_failed_is_reported_without_implementer_retry(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: "")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", lambda message: None)
    monkeypatch.setattr(
        supervisor,
        "collect_nightly_multi_agent_analysis",
        lambda *args, **kwargs: ([], ["system_upgrade: timeout", "error_review: unavailable"]),
    )
    monkeypatch.setattr(supervisor, "run_repair", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run repair")))
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is True

    state = json.loads(state_path.read_text())
    assert state["status"] == "PLAN_INCOMPLETE"
    assert state["last_run_date"] == "2026-08-31"
    assert state["analysis_failures"] == ["system_upgrade: timeout", "error_review: unavailable"]


def test_nightly_dirty_checkout_is_recorded_but_does_not_block_read_only_plan(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    notifications = []
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: " M compose.yaml")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", notifications.append)
    monkeypatch.setattr(supervisor, "collect_nightly_multi_agent_analysis", lambda *args, **kwargs: (["read-only plan"], []))
    monkeypatch.setattr(supervisor, "run_repair", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run repair")))
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is True

    state = json.loads(state_path.read_text())
    assert state["status"] == "PLAN_READY"
    assert state["last_run_date"] == "2026-08-31"
    assert state["checkout_dirty"] is True
    assert state["checkout_changes"] == [" M compose.yaml"]
    assert notifications == []


def test_nightly_dashboard_override_never_enables_implementation(monkeypatch, tmp_path):
    state_path = tmp_path / "nightly.json"
    notifications = []
    now = datetime(2026, 8, 30, 17, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(supervisor, "_dirty_checkout", lambda repo: " M dashboard/app.py")
    monkeypatch.setattr(supervisor, "send_code_repair_alert", notifications.append)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_date", "2026-08-31", raising=False)
    monkeypatch.setattr(supervisor.settings, "ai_nightly_improvement_override_enabled", True, raising=False)
    monkeypatch.setattr(supervisor, "collect_nightly_multi_agent_analysis", lambda *args, **kwargs: (["plan"], []))
    monkeypatch.setattr(supervisor, "run_repair", lambda *a, **k: (_ for _ in ()).throw(AssertionError("override must not enable repair")))

    assert supervisor.run_nightly_ai_improvement(tmp_path, state_path, now=now) is True
    state = json.loads(state_path.read_text())
    assert state["status"] == "PLAN_READY"
    assert state["mode"] == "PLAN_ONLY"
    assert notifications == []
