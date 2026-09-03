import asyncio
import json

from config.settings import settings
from shared import claude_cli


class _Process:
    returncode = 0

    async def communicate(self, input=None):
        return json.dumps({"result": "ok"}).encode(), b""


def test_run_claude_prompt_passes_model_and_effort(monkeypatch):
    captured = []

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured.append((command, kwargs))
        return _Process()

    monkeypatch.setattr(claude_cli, "claude_executable", lambda: "/usr/bin/claude")
    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(settings, "claude_chat_model", "claude-opus-4-8")
    monkeypatch.setattr(settings, "claude_chat_effort", "xhigh")

    assert asyncio.run(claude_cli.run_claude_prompt("hello")) == "ok"
    assert captured[0][0] == (
        "/usr/bin/claude", "-p", "--output-format", "json", "--tools", "",
        "--model", "claude-opus-4-8", "--effort", "xhigh",
    )


def test_run_claude_prompt_leaves_automatic_choices_to_cli(monkeypatch):
    captured = []

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured.append(command)
        return _Process()

    monkeypatch.setattr(claude_cli, "claude_executable", lambda: "/usr/bin/claude")
    monkeypatch.setattr(claude_cli.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(settings, "claude_chat_model", "default")
    monkeypatch.setattr(settings, "claude_chat_effort", "auto")

    assert asyncio.run(claude_cli.run_claude_prompt("hello")) == "ok"
    assert "--model" not in captured[0]
    assert "--effort" not in captured[0]
