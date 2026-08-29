import asyncio

import pytest

from shared import codex_app_server as module


class _FakeStdin:
    def write(self, _data):
        pass

    async def drain(self):
        pass


class _FakeProcess:
    def __init__(self, stdout):
        self.returncode = None
        self.stdin = _FakeStdin()
        self.stdout = stdout

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


def test_reader_failure_unblocks_pending_request():
    class BrokenStdout:
        async def readline(self):
            raise ValueError("Separator is found, but chunk is longer than limit")

    async def scenario():
        server = module.CodexAppServer()
        server._process = _FakeProcess(BrokenStdout())
        future = asyncio.get_running_loop().create_future()
        server._pending[1] = future

        await server._read_loop()

        assert server._pending == {}
        with pytest.raises(module.CodexAppServerError, match="reader lỗi"):
            future.result()

    asyncio.run(scenario())


def test_app_server_uses_large_jsonl_stream_limit(monkeypatch):
    captured = []

    async def _empty_readline(_self):
        return b""

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured.append((command, kwargs))
        return _FakeProcess(type("EmptyStdout", (), {"readline": _empty_readline})())

    async def _request(*_args, **_kwargs):
        return {}

    async def scenario():
        server = module.CodexAppServer()
        server._request = _request
        await server._ensure_started()
        await server.close()

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    asyncio.run(scenario())

    assert captured[0][1]["limit"] == module.CODEX_APP_SERVER_STREAM_LIMIT
