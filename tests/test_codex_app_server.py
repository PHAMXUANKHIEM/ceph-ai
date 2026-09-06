import asyncio
from types import SimpleNamespace

from shared import codex_app_server as codex


class _CompletedProcess:
    returncode = 0

    async def wait(self):
        return self.returncode


def test_refreshing_default_login_never_consumes_a_separate_profile(monkeypatch, tmp_path):
    default_home = tmp_path / "default"
    separate_home = tmp_path / "repair-profile"
    monkeypatch.setattr(codex.codex_app_server, "_codex_home", lambda: default_home)
    closed = []

    async def close():
        closed.append(True)

    monkeypatch.setattr(codex.codex_app_server, "close", close)
    codex._device_login_processes.clear()
    codex._device_login_results.clear()
    codex._device_login_drain_tasks.clear()
    separate_key = str(separate_home.resolve())
    codex._device_login_processes[separate_key] = _CompletedProcess()
    codex._device_login_results[separate_key] = {"loginId": "separate"}
    try:
        assert asyncio.run(codex.refresh_app_server_after_cli_login()) == "none"
        assert separate_key in codex._device_login_processes

        assert asyncio.run(codex.refresh_app_server_after_cli_login(separate_home)) == "completed"
        assert separate_key not in codex._device_login_processes
        assert closed == []
    finally:
        codex._device_login_processes.clear()
        codex._device_login_results.clear()
        codex._device_login_drain_tasks.clear()


def test_auth_file_change_invalidates_a_live_app_server(monkeypatch, tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    auth_file = home / "auth.json"
    auth_file.write_text('{"token":"old"}')
    app_server = codex.CodexAppServer()
    monkeypatch.setattr(app_server, "_codex_home", lambda: home)
    app_server._process = SimpleNamespace(returncode=None)
    app_server._reader_task = SimpleNamespace(done=lambda: False)
    app_server._auth_state = app_server._auth_file_state()

    assert app_server._is_live_for_auth_state(app_server._auth_file_state())
    auth_file.write_text('{"token":"new-account-token"}')
    assert not app_server._is_live_for_auth_state(app_server._auth_file_state())
