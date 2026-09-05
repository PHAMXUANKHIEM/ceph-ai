import os
import time
from pathlib import Path

import pytest

from worker import code_repair


def test_extract_latest_error_uses_newest_log_and_redacts_secret(tmp_path):
    old = tmp_path / "old.log"
    old.write_text("ERROR old failure")
    new = tmp_path / "new.log"
    new.write_text("prefix\nTraceback (most recent call last):\nAPI_KEY=very-secret\nValueError: broken")
    os.utime(old, (1, 1))
    os.utime(new, (2, 2))

    evidence = code_repair.extract_latest_error([old, new])

    assert "ValueError: broken" in evidence
    assert "very-secret" not in evidence
    assert "API_KEY=<redacted>" in evidence


def test_redact_evidence_handles_file_style_credentials_and_private_keys():
    evidence = (
        '{"api_key": "abcdefghijklmnop123456", '
        '"password": "secret value", '
        '"authorization": "Bearer abcdefghijklmnop"}\n'
        "-----BEGIN PRIVATE KEY-----\nprivate-content\n-----END PRIVATE KEY-----"
    )
    redacted = code_repair.redact_evidence(evidence)

    assert "abcdefghijklmnop123456" not in redacted
    assert "secret value" not in redacted
    assert "Bearer abcdefghijklmnop" not in redacted
    assert "private-content" not in redacted
    assert redacted.count("<redacted>") >= 3
    assert "<private-key-redacted>" in redacted


def test_fingerprint_ignores_timestamps_ids_and_line_numbers():
    first = "2026-08-24T01:00:00 ERROR incident abcdef123456 line 42"
    second = "2026-08-25T02:00:00 ERROR incident fedcba654321 line 99"
    assert code_repair.fingerprint(first) == code_repair.fingerprint(second)


def test_validate_changes_rejects_deployment_script(monkeypatch, tmp_path):
    outputs = iter([
        " M scripts/deploy/restart_services.sh\n",
        "diff --git a/scripts/deploy/restart_services.sh b/scripts/deploy/restart_services.sh\n",
    ])
    monkeypatch.setattr(
        code_repair, "_run",
        lambda *args, **kwargs: type("R", (), {"stdout": next(outputs), "returncode": 0})(),
    )
    with pytest.raises(code_repair.RepairError, match="outside the repair allowlist"):
        code_repair._validate_changes(tmp_path)


def test_validate_changes_ignores_supervisor_venv_symlink(monkeypatch, tmp_path):
    outputs = iter([
        "?? .venv\n M worker/example.py\n",
        "diff --git a/worker/example.py b/worker/example.py\n",
    ])
    monkeypatch.setattr(
        code_repair, "_run",
        lambda *args, **kwargs: type("R", (), {"stdout": next(outputs), "returncode": 0})(),
    )
    assert code_repair._validate_changes(tmp_path) == ["worker/example.py"]


def test_validate_changes_rejects_nested_dotenv_before_staging(monkeypatch, tmp_path):
    outputs = iter(["?? worker/generated/.env\n"])
    monkeypatch.setattr(
        code_repair, "_run",
        lambda *args, **kwargs: type("R", (), {"stdout": next(outputs), "returncode": 0})(),
    )
    with pytest.raises(code_repair.RepairError, match="dotenv file"):
        code_repair._validate_changes(tmp_path)


def test_validate_changes_scans_untracked_file_contents(monkeypatch, tmp_path):
    candidate = tmp_path / "worker" / "generated.py"
    candidate.parent.mkdir()
    candidate.write_text('API_KEY = "abcdefghijklmnop123456"\n')
    outputs = iter([
        "?? worker/generated.py\n",
        "",
    ])
    monkeypatch.setattr(
        code_repair, "_run",
        lambda *args, **kwargs: type("R", (), {"stdout": next(outputs), "returncode": 0})(),
    )
    with pytest.raises(code_repair.RepairError, match="candidate file appears"):
        code_repair._validate_changes(tmp_path)


def test_validate_changes_allows_untracked_setting_reference(monkeypatch, tmp_path):
    candidate = tmp_path / "worker" / "generated.py"
    candidate.parent.mkdir()
    candidate.write_text("api_key = settings.router_api_key\n")
    outputs = iter(["?? worker/generated.py\n", ""])
    monkeypatch.setattr(
        code_repair, "_run",
        lambda *args, **kwargs: type("R", (), {"stdout": next(outputs), "returncode": 0})(),
    )
    assert code_repair._validate_changes(tmp_path) == ["worker/generated.py"]


def test_validate_changes_diff_includes_staged_content(monkeypatch, tmp_path):
    outputs = iter([
        "A  worker/generated.py\n",
        'diff --git a/worker/generated.py b/worker/generated.py\n+API_KEY = "abcdefghijklmnop123456"',
    ])
    commands = []

    def fake_run(args, **kwargs):
        commands.append(list(args))
        return type("R", (), {"stdout": next(outputs), "returncode": 0})()

    monkeypatch.setattr(code_repair, "_run", fake_run)
    with pytest.raises(code_repair.RepairError, match="candidate diff"):
        code_repair._validate_changes(tmp_path)
    assert commands[1][0:4] == ["git", "diff", "HEAD", "--"]


def test_diff_secret_guard_allows_setting_reference_but_blocks_literal():
    assert not code_repair.DIFF_SECRET_RE.search("bot_token = settings.telegram_bot_token")
    assert code_repair.DIFF_SECRET_RE.search('api_key = "abcdefghijklmnop123456"')


def test_duplicate_error_is_not_sent_to_ai(monkeypatch, tmp_path):
    evidence = "ERROR stable failure"
    fp = code_repair.fingerprint(evidence)
    state = tmp_path / "state.json"
    state.write_text('{"attempts":{"%s":{"branch":"ai-repair/existing"}}}' % fp)
    config = code_repair.RepairConfig(repo=tmp_path, state_file=state)
    monkeypatch.setattr(code_repair, "_run", lambda *a, **k: pytest.fail("must not run"))
    result = code_repair.run_repair(evidence, config)
    assert result.status == "SKIPPED_DUPLICATE"
    assert result.branch == "ai-repair/existing"


def test_claude_provider_uses_dashboard_account_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(code_repair.shutil, "which", lambda name: f"/bin/{name}")
    provider, command = code_repair._provider_command(
        "claude", tmp_path, "repair it", 30,
        claude_config_dir=tmp_path / ".claude-account",
    )
    assert provider == "claude"
    assert f"CLAUDE_CONFIG_DIR={tmp_path / '.claude-account'}" in command
    assert command[-1] == "repair it"


def test_codex_provider_uses_dashboard_account_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(code_repair.shutil, "which", lambda name: f"/bin/{name}")
    provider, command = code_repair._provider_command(
        "codex", tmp_path, "repair it", 30,
        codex_home=tmp_path / ".codex-account",
    )
    assert provider == "codex"
    assert f"CODEX_HOME={tmp_path / '.codex-account'}" in command
    assert "--approve-for-me" in command
    assert "--sandbox" not in command
    assert command[-1] == "-"


def test_codex_review_provider_is_read_only_and_accepts_model(monkeypatch, tmp_path):
    monkeypatch.setattr(code_repair.shutil, "which", lambda name: f"/bin/{name}")
    provider, command = code_repair._provider_command(
        "codex", tmp_path, "review it", 30,
        codex_home=tmp_path / ".codex-account", model="review-model", mode="review",
    )
    assert provider == "codex"
    assert "--sandbox" in command
    assert "read-only" in command
    assert "--approve-for-me" not in command
    assert command[command.index("--model") + 1] == "review-model"
    assert command[-1] == "-"


def test_provider_inline_model_and_review_verdict_fail_closed():
    assert code_repair._provider_and_inline_model("codex:gpt-5-codex") == ("codex", "gpt-5-codex")
    assert code_repair._provider_and_inline_model("claude") == ("claude", "")
    assert code_repair._provider_cli_value("codex:gpt-5-codex") == "codex:gpt-5-codex"
    with pytest.raises(code_repair.argparse.ArgumentTypeError):
        code_repair._provider_cli_value("router")
    assert code_repair._review_verdict("notes\nVERDICT: PASS\n") == ("PASS", "notes\nVERDICT: PASS\n")
    with pytest.raises(code_repair.RepairError, match="valid VERDICT"):
        code_repair._review_verdict("The patch looks good.")


def test_review_rounds_are_hard_bounded():
    assert code_repair._review_rounds_cli_value("0") == 0
    assert code_repair._review_rounds_cli_value(str(code_repair.MAX_REVIEW_ROUNDS)) == code_repair.MAX_REVIEW_ROUNDS
    with pytest.raises(code_repair.argparse.ArgumentTypeError):
        code_repair._review_rounds_cli_value(str(code_repair.MAX_REVIEW_ROUNDS + 1))


def test_two_agent_repair_retries_after_review_feedback(monkeypatch, tmp_path):
    """The planner/reviewer and implementer exchange feedback at most N times."""
    evidence = "ERROR two-agent failure"
    state = tmp_path / "state.json"
    calls = []
    responses = iter([
        ("codex", "plan: fix worker/fix.py and add a regression test"),
        ("codex", "implemented initial patch"),
        ("codex", "The test misses the timeout case.\nVERDICT: NEEDS_CHANGES"),
        ("codex", "implemented timeout regression test"),
        ("codex", "The patch is minimal and covered.\nVERDICT: PASS"),
    ])

    class QuietNotifier:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def update(self, *args):
            pass

        def finish(self, *args):
            pass

    def fake_run_agent(provider, worktree, prompt, config, **kwargs):
        calls.append((provider, kwargs.get("mode"), prompt))
        return next(responses)

    def fake_run(args, **kwargs):
        command = list(args)
        if command[:2] == ["git", "worktree"] and command[2] in {"add", "remove"}:
            if command[2] == "add":
                worktree_path = command[4] if "--detach" in command else command[5]
                Path(worktree_path).mkdir(parents=True, exist_ok=True)
            return type("Result", (), {"stdout": "", "returncode": 0})()
        if command[:2] == ["git", "status"]:
            return type("Result", (), {"stdout": "", "returncode": 0})()
        if command[:2] == ["git", "diff"]:
            return type("Result", (), {"stdout": "diff --git a/worker/fix.py b/worker/fix.py", "returncode": 0})()
        if command[:2] == ["git", "rev-parse"]:
            return type("Result", (), {"stdout": "deadbeef\n", "returncode": 0})()
        return type("Result", (), {"stdout": "", "returncode": 0})()

    monkeypatch.setattr(code_repair, "RepairProgressNotifier", QuietNotifier)
    monkeypatch.setattr(code_repair, "_run_agent", fake_run_agent)
    monkeypatch.setattr(code_repair, "_run", fake_run)
    monkeypatch.setattr(code_repair, "_validate_changes", lambda worktree: ["worker/fix.py"])

    result = code_repair.run_repair(
        evidence,
        code_repair.RepairConfig(
            repo=tmp_path, state_file=state, provider="codex",
            planner_model="review-model", implementer_model="coding-model",
            max_review_rounds=2,
        ),
    )

    assert result.status == "COMMITTED"
    assert result.planner_provider == "codex"
    assert result.implementer_provider == "codex"
    assert result.review_rounds == 2
    assert [mode for _, mode, _ in calls] == ["review", "implement", "review", "implement", "review"]
    assert "test misses the timeout case" in calls[3][2]


def test_progress_notifier_sends_start_periodic_and_success(monkeypatch):
    messages = []
    monkeypatch.setattr(code_repair, "send_code_repair_alert", messages.append)
    notifier = code_repair.RepairProgressNotifier(
        "ERROR Telegram timeout", "ai-repair/test", interval_seconds=0.01,
    )
    notifier.start()
    notifier.update(45, "đang chạy test")
    time.sleep(0.03)
    notifier.finish(code_repair.RepairResult(
        status="PUSHED", fingerprint="abc", branch="ai-repair/test",
        commit="deadbeef", changed_files=["shared/telegram_client.py"],
    ))

    assert "BẮT ĐẦU" in messages[0]
    assert any("45%" in message and "đang chạy test" in message for message in messages)
    assert "THÀNH CÔNG" in messages[-1]
    assert "100%" in messages[-1]
