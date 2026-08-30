import asyncio

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
