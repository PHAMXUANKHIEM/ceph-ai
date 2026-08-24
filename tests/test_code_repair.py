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


def test_telegram_evidence_drops_paramiko_noise_and_keeps_actual_error():
    evidence = """Source application log: ceph-ai-remediation-watcher.log
2026-08-24 14:06:07 INFO:paramiko.transport:Connected (version 2.0, client OpenSSH_9.9)
2026-08-24 14:06:07 INFO:paramiko.transport:Authentication (publickey) successful!
2026-08-24 14:06:37 ERROR:watcher.verify: Vault token lookup returned HTTP 403
2026-08-24 14:06:47 INFO:paramiko.transport:Authentication (publickey) successful!
"""
    summary = code_repair.summarize_evidence(evidence)
    assert summary == (
        "ceph-ai-remediation-watcher.log: "
        "2026-08-24 14:06:37 ERROR:watcher.verify: Vault token lookup returned HTTP 403"
    )
    assert "Authentication" not in summary


def test_clean_evidence_removes_transport_chatter_but_keeps_traceback():
    evidence = (
        "INFO:paramiko.transport:Connected (version 2.0, client OpenSSH_9.9)\n"
        "ERROR watcher failed\nTraceback (most recent call last):\n"
        "sqlalchemy.orm.exc.DetachedInstanceError: detached\n"
        "INFO:paramiko.transport:Authentication (publickey) successful!"
    )
    cleaned = code_repair.clean_evidence(evidence)
    assert "paramiko" not in cleaned
    assert "DetachedInstanceError" in cleaned


def test_telegram_evidence_is_short_and_redacted():
    evidence = "Source application log: worker.log\nERROR token=very-secret " + "x" * 1000
    summary = code_repair.summarize_evidence(evidence)
    assert len(summary) <= 360
    assert "very-secret" not in summary


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
