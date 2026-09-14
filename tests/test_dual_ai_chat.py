import asyncio
import sys

import pytest

from dashboard import dual_ai_chat
from dashboard.dual_ai_chat import (
    DualAIChatError,
    DUAL_WORKSPACE_ENV,
    _execution_repo,
)


def test_dual_write_mode_uses_configured_isolated_workspace(monkeypatch, tmp_path):
    workspace = tmp_path / "dual-workspace"
    workspace.mkdir()
    (workspace / ".git").write_text("gitdir: isolated\n")
    monkeypatch.setenv(DUAL_WORKSPACE_ENV, str(workspace))

    assert _execution_repo(allow_writes=True, full_access=False) == workspace


def test_dual_write_mode_uses_default_isolated_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv(DUAL_WORKSPACE_ENV, raising=False)
    workspace = tmp_path / "default-dual-workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    monkeypatch.setattr("dashboard.dual_ai_chat.DEFAULT_DUAL_WORKSPACE", workspace)

    assert _execution_repo(allow_writes=True, full_access=False) == workspace


def test_dual_write_mode_refuses_live_source_without_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv(DUAL_WORKSPACE_ENV, raising=False)
    monkeypatch.setattr("dashboard.dual_ai_chat.DEFAULT_DUAL_WORKSPACE", tmp_path / "missing")

    with pytest.raises(DualAIChatError, match="từ chối sửa source thật"):
        _execution_repo(allow_writes=True, full_access=False)


def test_single_full_has_no_application_runtime_timeout(monkeypatch):
    captured = {}

    def provider_command(_provider, _repo, _prompt, timeout, **_kwargs):
        captured["timeout"] = timeout
        return "test", [
            sys.executable,
            "-c",
            "import sys; sys.stdin.buffer.read(); print('done')",
        ]

    monkeypatch.setattr(dual_ai_chat, "_provider_command", provider_command)
    monkeypatch.setattr(dual_ai_chat, "check_ai_budget", lambda *_args: None)
    monkeypatch.setattr(dual_ai_chat, "record_ai_attempt", lambda **_kwargs: None)

    result = asyncio.run(
        dual_ai_chat._ask(
            "implementer",
            "finish the requested work",
            provider_spec="codex",
            full_access=True,
        )
    )

    assert captured["timeout"] is None
    assert result["content"] == "done"
