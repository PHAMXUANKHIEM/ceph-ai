import re

import bcrypt
import httpx
import openai
import pytest

import dashboard.routes.settings as settings_route
from config.settings import settings
from shared import db as db_module
from shared import env_config
from shared.models import User
from watcher.ceph_client import CephQueryError


def _login(client):
    # dashboard_client fixture (conftest.py) pins these credentials.
    client.post("/login", data={"username": "admin", "password": "admin"})


def _create_user(username, password, *, is_admin=False, is_active=True, created_by="admin"):
    with db_module.SessionLocal() as session:
        session.add(
            User(
                username=username,
                password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                is_admin=is_admin,
                is_active=is_active,
                created_by=created_by,
            )
        )
        session.commit()


def _login_as(client, username, password):
    client.post("/login", data={"username": username, "password": password})


def test_unauthenticated_get_settings_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/settings", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_authenticated_get_settings_returns_form(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/settings")
    assert response.status_code == 200
    assert "API Key" in response.text


# --- POST /settings/9router/save (Step 2 "[Lưu cấu hình]" button) ---------


def test_post_settings_save_router_with_invalid_key_shows_error_and_does_not_persist(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def fake_verify(api_key, base_url):
        return False, "API key không hợp lệ", None

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    monkeypatch.setattr(settings, "router_enabled", False, raising=False)

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-wrong",
            "router_base_url": "http://localhost:20128",
            "router_model_id": "gc/gemini-2.5-pro",
        },
    )

    assert response.status_code == 200
    assert "không hợp lệ" in response.text
    assert "ROUTER_API_KEY" not in tmp_env.read_text()
    assert settings.router_api_key == ""


def test_post_settings_save_router_with_valid_key_persists_updates_settings_and_restarts_worker(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def fake_verify(api_key, base_url):
        return True, "Kết nối thành công — tìm thấy 1 model", ["gc/gemini-2.5-pro"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    monkeypatch.setattr(settings, "router_enabled", False, raising=False)
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 99999, "error": None}
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-real-key-value",
            "router_base_url": "http://localhost:20128",
            "router_model_id": "gc/gemini-2.5-pro",
        },
    )

    assert response.status_code == 200
    assert "Đã lưu" in response.text
    assert "khởi động lại Worker (PID 99999)" in response.text
    env_text = tmp_env.read_text()
    assert "ROUTER_API_KEY=sk-real-key-value" in env_text
    assert "ROUTER_MODEL=gc/gemini-2.5-pro" in env_text
    assert "ROUTER_BASE_URL=http://localhost:20128" in env_text
    assert "ROUTER_ENABLED=true" in env_text
    assert settings.router_api_key == "sk-real-key-value"
    assert settings.router_model == "gc/gemini-2.5-pro"
    assert settings.router_base_url == "http://localhost:20128"
    assert settings.router_enabled is True


def test_post_settings_save_router_persists_selected_provider(dashboard_client, monkeypatch, tmp_path):
    # 2026-07-24: Settings page's "Loại kết nối" picker (Claude/Codex/
    # OpenRouter/9router) — router_provider is UI-only (see config/
    # settings.py's comment: the actual client is provider-agnostic), but
    # it must still round-trip through .env/settings the same way
    # router_api_key/router_base_url/router_model do.
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def fake_verify(api_key, base_url):
        return True, "Kết nối thành công — tìm thấy 1 model", ["gpt-5-codex"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    monkeypatch.setattr(settings, "router_enabled", False, raising=False)
    monkeypatch.setattr(settings, "router_provider", "9router", raising=False)
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None}
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-openai-key",
            "router_base_url": "https://api.openai.com/v1",
            "router_model_id": "gpt-5-codex",
            "router_provider": "openai",
        },
    )

    assert response.status_code == 200
    env_text = tmp_env.read_text()
    assert "ROUTER_PROVIDER=openai" in env_text
    assert settings.router_provider == "openai"

    # A fresh GET now shows the Codex (OpenAI) label and radio pre-selected.
    get_response = dashboard_client.get("/settings")
    assert "Codex (OpenAI)" in get_response.text
    openai_input_tag = re.search(r'<input\b[^>]*value="openai"[^>]*>', get_response.text)
    assert openai_input_tag is not None
    assert "checked" in openai_input_tag.group(0)


def test_post_settings_save_router_with_unknown_provider_falls_back_to_9router(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def fake_verify(api_key, base_url):
        return True, "ok", ["some-model"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    monkeypatch.setattr(settings, "router_enabled", False, raising=False)
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None}
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-key",
            "router_base_url": "http://localhost:20128",
            "router_model_id": "some-model",
            "router_provider": "not-a-real-provider",
        },
    )

    assert response.status_code == 200
    assert settings.router_provider == "9router"
    assert "ROUTER_PROVIDER=9router" in tmp_env.read_text()


def test_post_settings_save_router_persists_selected_model(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def fake_verify(api_key, base_url):
        return True, "ok", ["gc/gemini-2.5-flash", "gc/gemini-2.5-pro"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    monkeypatch.setattr(settings, "router_model", "old-model", raising=False)
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None}
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-real-key",
            "router_base_url": "http://localhost:20128",
            "router_model_id": "gc/gemini-2.5-pro",
        },
    )

    assert response.status_code == 200
    assert "ROUTER_MODEL=gc/gemini-2.5-pro" in tmp_env.read_text()
    assert settings.router_model == "gc/gemini-2.5-pro"


def test_post_settings_save_router_captures_submitted_base_url(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings, "router_base_url", "http://old-router.example", raising=False)
    captured = []

    async def fake_verify(api_key, base_url):
        captured.append(base_url)
        return True, "ok", ["gc/gemini-2.5-pro"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None}
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-router-key",
            "router_model_id": "gc/gemini-2.5-pro",
            "router_base_url": "http://localhost:20128",
        },
    )

    assert response.status_code == 200
    assert captured == ["http://localhost:20128"]
    env_text = tmp_env.read_text()
    assert "ROUTER_MODEL=gc/gemini-2.5-pro" in env_text
    assert "ROUTER_BASE_URL=http://localhost:20128" in env_text
    assert settings.router_base_url == "http://localhost:20128"


def test_post_settings_save_router_blank_base_url_keeps_existing_one(dashboard_client, monkeypatch, tmp_path):
    # Not a password field, but still falls back to the existing value when
    # blank — the "[Đổi model]" flow re-saves without retyping the base_url.
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings, "router_base_url", "http://localhost:20128", raising=False)

    async def fake_verify(api_key, base_url):
        return True, "ok", ["gc/gemini-2.5-pro"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None}
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={"router_api_key": "sk-router-key", "router_model_id": "gc/gemini-2.5-pro"},
    )

    assert response.status_code == 200
    assert "ROUTER_BASE_URL=http://localhost:20128" in tmp_env.read_text()
    assert settings.router_base_url == "http://localhost:20128"


def test_post_settings_save_router_rejects_blank_base_url_with_nothing_already_configured(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings, "router_base_url", "", raising=False)
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={"router_api_key": "sk-router-key", "router_model_id": "gc/gemini-2.5-pro"},
    )

    assert response.status_code == 200
    assert "Base URL" in response.text
    assert "ROUTER_BASE_URL" not in tmp_env.read_text()
    assert settings.router_base_url == ""


def test_post_settings_save_router_blank_model_shows_error_and_does_not_persist(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={"router_api_key": "sk-router-key", "router_base_url": "http://localhost:20128"},
    )

    assert response.status_code == 200
    assert "Chưa chọn model" in response.text
    assert "ROUTER_MODEL" not in tmp_env.read_text()


def test_post_settings_save_router_model_not_in_returned_list_shows_error(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def fake_verify(api_key, base_url):
        return True, "ok", ["gc/gemini-2.5-flash"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-router-key",
            "router_base_url": "http://localhost:20128",
            "router_model_id": "gc/does-not-exist",
        },
    )

    assert response.status_code == 200
    assert "không khả dụng" in response.text
    assert "ROUTER_MODEL" not in tmp_env.read_text()


def test_post_settings_save_router_valid_key_but_worker_restart_fails_still_reports_saved(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def fake_verify(api_key, base_url):
        return True, "ok", ["gc/gemini-2.5-pro"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    monkeypatch.setattr(
        settings_route,
        "restart_worker",
        lambda: {"restarted": False, "new_pid": None, "error": "exited immediately"},
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-real-key-value",
            "router_base_url": "http://localhost:20128",
            "router_model_id": "gc/gemini-2.5-pro",
        },
    )

    assert response.status_code == 200
    # Key save succeeded independently — must still be reported/persisted.
    assert "Đã lưu" in response.text
    assert "ROUTER_API_KEY=sk-real-key-value" in tmp_env.read_text()
    assert settings.router_api_key == "sk-real-key-value"
    # Restart failure is shown separately, not conflated with "wrong key".
    assert "khởi động lại thủ công" in response.text
    assert "không hợp lệ" not in response.text


def test_post_settings_save_router_invalid_key_never_touches_worker_process_management(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def fake_verify(api_key, base_url):
        return False, "API key không hợp lệ", None

    restart_calls = []
    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: restart_calls.append(1) or {}
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-wrong",
            "router_base_url": "http://localhost:20128",
            "router_model_id": "gc/gemini-2.5-pro",
        },
    )

    assert response.status_code == 200
    assert "không hợp lệ" in response.text
    assert restart_calls == []


def test_post_settings_save_router_blank_key_keeps_existing_key_and_revalidates_it(
    dashboard_client, monkeypatch, tmp_path
):
    # The api_key field is a password input that never gets pre-filled with
    # the real (masked) value — an operator picking a different model must
    # be able to save without retyping a key they aren't actually changing.
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\nROUTER_API_KEY=sk-already-configured\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings, "router_api_key", "sk-already-configured", raising=False)
    monkeypatch.setattr(settings, "router_model", "old-model", raising=False)

    captured = []

    async def fake_verify(api_key, base_url):
        captured.append(api_key)
        return True, "ok", ["new-model"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None}
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={"router_base_url": "http://localhost:20128", "router_model_id": "new-model"},
    )

    assert response.status_code == 200
    assert "Đã lưu" in response.text
    assert captured == ["sk-already-configured"]
    env_text = tmp_env.read_text()
    assert "ROUTER_API_KEY=sk-already-configured" in env_text
    assert "ROUTER_MODEL=new-model" in env_text


def test_post_settings_save_router_network_error_shows_the_underlying_reason(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def flaky_verify(api_key, base_url):
        return False, "network unreachable", None

    monkeypatch.setattr(settings_route, "verify_router_connection", flaky_verify)

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-whatever",
            "router_base_url": "http://localhost:20128",
            "router_model_id": "gc/gemini-2.5-pro",
        },
    )

    assert response.status_code == 200
    assert "network unreachable" in response.text
    assert "Đã lưu" not in response.text


def test_post_settings_save_router_valid_key_but_restart_internals_raise_returns_200_not_500(
    dashboard_client, monkeypatch, tmp_path
):
    # HTTP-level version of test_restart_worker_never_raises_on_internal_error
    # — proves the raised exception never escapes as a raw FastAPI 500,
    # exercised through the real POST /settings/9router/save route (not by
    # calling restart_worker() directly).
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def fake_verify(api_key, base_url):
        return True, "ok", ["gc/gemini-2.5-pro"]

    def boom():
        raise RuntimeError("pgrep exploded")

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    monkeypatch.setattr(settings_route, "_find_worker_pids", boom)

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save",
        data={
            "router_api_key": "sk-real-key-value",
            "router_base_url": "http://localhost:20128",
            "router_model_id": "gc/gemini-2.5-pro",
        },
    )

    assert response.status_code == 200
    assert "Đã lưu" in response.text
    assert "ROUTER_API_KEY=sk-real-key-value" in tmp_env.read_text()
    assert "khởi động lại thủ công" in response.text
    assert "pgrep exploded" not in response.text  # no raw exception leaked to the page


# --- POST /settings/9router/disconnect ("[Huỷ kết nối]" button) -----------


def test_unauthenticated_post_9router_disconnect_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/settings/9router/disconnect", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_post_9router_disconnect_clears_config_and_restarts_worker(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text(
        "DASHBOARD_USERNAME=admin\n"
        "ROUTER_API_KEY=sk-currently-connected\n"
        "ROUTER_MODEL=gc/gemini-2.5-pro\n"
        "ROUTER_BASE_URL=http://localhost:20128\n"
        "ROUTER_ENABLED=true\n"
    )
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings, "router_api_key", "sk-currently-connected", raising=False)
    monkeypatch.setattr(settings, "router_model", "gc/gemini-2.5-pro", raising=False)
    monkeypatch.setattr(settings, "router_base_url", "http://localhost:20128", raising=False)
    monkeypatch.setattr(settings, "router_enabled", True, raising=False)
    restart_calls = []
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: restart_calls.append(1) or {"restarted": True, "new_pid": 1, "error": None}
    )

    _login(dashboard_client)
    response = dashboard_client.post("/settings/9router/disconnect")

    assert response.status_code == 200
    assert "Đã huỷ kết nối" in response.text
    env_text = tmp_env.read_text()
    assert "ROUTER_API_KEY=" in env_text and "sk-currently-connected" not in env_text
    assert "ROUTER_ENABLED=false" in env_text
    assert settings.router_api_key == ""
    assert settings.router_model == ""
    assert settings.router_base_url == ""
    assert settings.router_enabled is False
    assert restart_calls == [1]


# --- restart_worker/restart_watcher process management ---------------------


def test_restart_worker_starts_new_before_stopping_old(monkeypatch):
    # Start-before-stop is deliberate: if starting the new process fails, the
    # old one must still be running — never end up with zero Workers.
    calls = []
    monkeypatch.setattr(settings_route, "_find_worker_pids", lambda: [111, 222])
    monkeypatch.setattr(settings_route, "_stop_worker", lambda pids, **kw: calls.append(("stop", pids)))
    monkeypatch.setattr(settings_route, "_start_worker", lambda: calls.append(("start",)) or 333)
    monkeypatch.setattr(settings_route, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(settings_route.time, "sleep", lambda _s: None)

    result = settings_route.restart_worker()

    assert calls == [("start",), ("stop", [111, 222])]
    assert result == {"restarted": True, "new_pid": 333, "error": None}


def test_restart_worker_leaves_old_process_running_when_new_start_fails(monkeypatch):
    stop_calls = []
    monkeypatch.setattr(settings_route, "_find_worker_pids", lambda: [111])
    monkeypatch.setattr(settings_route, "_stop_worker", lambda pids, **kw: stop_calls.append(pids))
    monkeypatch.setattr(settings_route, "_start_worker", lambda: 999)
    monkeypatch.setattr(settings_route, "_pid_alive", lambda pid: False)  # new process died
    monkeypatch.setattr(settings_route.time, "sleep", lambda _s: None)

    result = settings_route.restart_worker()

    assert result["restarted"] is False
    # The old (still-working) process must NOT have been touched.
    assert stop_calls == []


def test_restart_worker_with_no_prior_worker_still_starts_one(monkeypatch):
    calls = []
    monkeypatch.setattr(settings_route, "_find_worker_pids", lambda: [])
    monkeypatch.setattr(
        settings_route, "_stop_worker", lambda pids, **kw: calls.append(("stop", pids))
    )
    monkeypatch.setattr(settings_route, "_start_worker", lambda: 444)
    monkeypatch.setattr(settings_route, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(settings_route.time, "sleep", lambda _s: None)

    result = settings_route.restart_worker()

    assert calls == []  # _stop_worker never called when there's nothing to stop
    assert result == {"restarted": True, "new_pid": 444, "error": None}


def test_restart_worker_reports_failure_when_new_process_dies_immediately(monkeypatch):
    monkeypatch.setattr(settings_route, "_find_worker_pids", lambda: [])
    monkeypatch.setattr(settings_route, "_start_worker", lambda: 555)
    monkeypatch.setattr(settings_route, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(settings_route.time, "sleep", lambda _s: None)

    result = settings_route.restart_worker()

    assert result["restarted"] is False
    assert result["new_pid"] is None
    assert result["error"] is not None


def test_restart_worker_never_raises_on_internal_error(monkeypatch):
    def boom():
        raise RuntimeError("pgrep exploded")

    monkeypatch.setattr(settings_route, "_find_worker_pids", boom)

    result = settings_route.restart_worker()

    assert result["restarted"] is False
    assert result["error"] is not None
    # Raw exception text must not leak to the (eventually user-facing) message.
    assert "pgrep exploded" not in result["error"]


# --- Story 5.1: restart_watcher() mirrors restart_worker() exactly --------


def test_restart_watcher_starts_new_before_stopping_old(monkeypatch):
    calls = []
    monkeypatch.setattr(settings_route, "_find_watcher_pids", lambda: [111, 222])
    monkeypatch.setattr(settings_route, "_stop_worker", lambda pids, **kw: calls.append(("stop", pids)))
    monkeypatch.setattr(settings_route, "_start_watcher", lambda: calls.append(("start",)) or 333)
    monkeypatch.setattr(settings_route, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(settings_route.time, "sleep", lambda _s: None)

    result = settings_route.restart_watcher()

    assert calls == [("start",), ("stop", [111, 222])]
    assert result == {"restarted": True, "new_pid": 333, "error": None}


def test_restart_watcher_leaves_old_process_running_when_new_start_fails(monkeypatch):
    stop_calls = []
    monkeypatch.setattr(settings_route, "_find_watcher_pids", lambda: [111])
    monkeypatch.setattr(settings_route, "_stop_worker", lambda pids, **kw: stop_calls.append(pids))
    monkeypatch.setattr(settings_route, "_start_watcher", lambda: 999)
    monkeypatch.setattr(settings_route, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(settings_route.time, "sleep", lambda _s: None)

    result = settings_route.restart_watcher()

    assert result["restarted"] is False
    assert stop_calls == []


def test_restart_watcher_with_no_prior_watcher_still_starts_one(monkeypatch):
    calls = []
    monkeypatch.setattr(settings_route, "_find_watcher_pids", lambda: [])
    monkeypatch.setattr(
        settings_route, "_stop_worker", lambda pids, **kw: calls.append(("stop", pids))
    )
    monkeypatch.setattr(settings_route, "_start_watcher", lambda: 444)
    monkeypatch.setattr(settings_route, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(settings_route.time, "sleep", lambda _s: None)

    result = settings_route.restart_watcher()

    assert calls == []
    assert result == {"restarted": True, "new_pid": 444, "error": None}


def test_restart_watcher_reports_failure_when_new_process_dies_immediately(monkeypatch):
    monkeypatch.setattr(settings_route, "_find_watcher_pids", lambda: [])
    monkeypatch.setattr(settings_route, "_start_watcher", lambda: 555)
    monkeypatch.setattr(settings_route, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(settings_route.time, "sleep", lambda _s: None)

    result = settings_route.restart_watcher()

    assert result["restarted"] is False
    assert result["new_pid"] is None
    assert result["error"] is not None


def test_restart_watcher_never_raises_on_internal_error(monkeypatch):
    def boom():
        raise RuntimeError("pgrep exploded")

    monkeypatch.setattr(settings_route, "_find_watcher_pids", boom)

    result = settings_route.restart_watcher()

    assert result["restarted"] is False
    assert result["error"] is not None
    assert "pgrep exploded" not in result["error"]


def test_find_watcher_pids_uses_correct_pgrep_pattern_with_double_dash(monkeypatch):
    # Regression guard for the Story 2.4/2.5 bug: pgrep -f "-m\s+..." (no --)
    # makes pgrep parse "-m" as its own flag and exit with an error.
    captured_args = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):
        captured_args.append(args)
        return FakeResult()

    monkeypatch.setattr(settings_route.subprocess, "run", fake_run)

    settings_route._find_watcher_pids()

    assert captured_args == [["pgrep", "-f", "--", settings_route.WATCHER_PGREP_PATTERN]]


@pytest.mark.live
def test_watcher_process_management_against_a_real_spawned_process(monkeypatch):
    """Real OS-level test, mirrors test_worker_process_management_against_a_real_spawned_process —
    actually spawns, finds, and kills a real `watcher.main` subprocess."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        log_path = f.name
    monkeypatch.setattr(settings_route, "WATCHER_LOG_PATH", settings_route.Path(log_path))

    assert (
        settings_route._find_watcher_pids() == []
    ), "test must start with no watcher.main running"

    pid = settings_route._start_watcher()
    try:
        assert settings_route._pid_alive(pid)
        found = settings_route._find_watcher_pids()
        assert pid in found
    finally:
        settings_route._stop_worker([pid])

    assert not settings_route._pid_alive(pid)
    assert pid not in settings_route._find_watcher_pids()


# --- Story 5.1: cluster connection config route ----------------------------


def _cluster_form_data(**overrides):
    data = {
        "ceph_mon_nodes": "10.9.9.1,10.9.9.2",
        "ceph_mon_hostnames": "mon1,mon2",
        "ceph_container_name": "ceph-mon-B",
        "ceph_osd_nodes": "10.9.9.3",
        "ceph_osd_container_name": "ceph-osd-B",
        "ssh_user": "root",
        "ssh_key_path": "/root/.ssh/some_key",
    }
    data.update(overrides)
    return data


def test_get_settings_shows_cluster_form_with_current_values(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/settings")
    assert response.status_code == 200
    assert settings.ceph_mon_nodes in response.text


def test_get_settings_shows_public_key_when_pub_file_exists(dashboard_client, monkeypatch, tmp_path):
    key_file = tmp_path / "test_key"
    key_file.write_text("fake private key")
    pub_file = tmp_path / "test_key.pub"
    pub_file.write_text("ssh-ed25519 AAAAfakepubkey watcher@host\n")
    monkeypatch.setattr(settings, "ssh_key_path", str(key_file), raising=False)

    _login(dashboard_client)
    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert "ssh-ed25519 AAAAfakepubkey watcher@host" in response.text


def test_get_settings_shows_hint_when_no_public_key_file(dashboard_client, monkeypatch, tmp_path):
    key_file = tmp_path / "test_key_without_pub"
    key_file.write_text("fake private key")
    monkeypatch.setattr(settings, "ssh_key_path", str(key_file), raising=False)

    _login(dashboard_client)
    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert "Không tìm thấy file public key" in response.text


def test_post_cluster_settings_missing_required_field_shows_error_and_skips_connection_test(
    dashboard_client, monkeypatch
):
    called = []
    monkeypatch.setattr(
        settings_route,
        "query_cluster_health_with",
        lambda *a, **kw: called.append(1),
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cluster", data=_cluster_form_data(ceph_mon_nodes="")
    )

    assert response.status_code == 200
    assert called == []


def test_post_cluster_settings_missing_ssh_key_path_shows_error_and_skips_ssh(
    dashboard_client, monkeypatch, tmp_path
):
    # ssh_key_path is no longer a form field — a bad path is now a
    # server-config problem (settings.ssh_key_path), not something the
    # submitted form data can express.
    called = []
    monkeypatch.setattr(
        settings_route,
        "query_cluster_health_with",
        lambda *a, **kw: called.append(1),
    )
    missing_path = str(tmp_path / "no_such_key")
    monkeypatch.setattr(settings, "ssh_key_path", missing_path)

    _login(dashboard_client)
    response = dashboard_client.post("/settings/cluster", data=_cluster_form_data())

    assert response.status_code == 200
    assert "không tồn tại" in response.text
    assert called == []  # AC #4: never attempts SSH when the key path itself is bad


def test_post_cluster_settings_blank_ssh_key_path_shows_distinct_server_config_error(
    dashboard_client, monkeypatch
):
    called = []
    monkeypatch.setattr(
        settings_route,
        "query_cluster_health_with",
        lambda *a, **kw: called.append(1),
    )
    monkeypatch.setattr(settings, "ssh_key_path", "")

    _login(dashboard_client)
    response = dashboard_client.post("/settings/cluster", data=_cluster_form_data())

    assert response.status_code == 200
    assert "Chưa cấu hình SSH key path trên server" in response.text
    assert called == []


def test_post_cluster_settings_connection_test_fails_does_not_persist_or_restart(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    key_file = tmp_path / "test_key"
    key_file.write_text("fake key")

    def fake_query(*a, **kw):
        raise CephQueryError("all MON nodes failed")

    monkeypatch.setattr(settings_route, "query_cluster_health_with", fake_query)
    restart_calls = []
    monkeypatch.setattr(
        settings_route, "restart_watcher", lambda: restart_calls.append(1) or {}
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cluster", data=_cluster_form_data(ssh_key_path=str(key_file))
    )

    assert response.status_code == 200
    assert "Không kết nối được" in response.text
    assert "CEPH_MON_NODES" not in tmp_env.read_text()
    assert restart_calls == []


def test_post_cluster_settings_success_persists_updates_settings_and_restarts_watcher(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    key_file = tmp_path / "test_key"
    key_file.write_text("fake key")

    captured_query_args = []

    def fake_query(mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode="docker"):
        captured_query_args.append((mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode))
        return {"status": "HEALTH_OK", "checks": {}}

    monkeypatch.setattr(settings_route, "query_cluster_health_with", fake_query)
    monkeypatch.setattr(
        settings_route,
        "restart_watcher",
        lambda: {"restarted": True, "new_pid": 55555, "error": None},
    )
    # 2026-07-24: cluster_settings_submit now ALSO restarts Worker (not just
    # Watcher) — without mocking this too, this test would spawn a REAL
    # `python -m worker.main` subprocess (verified: it did, live, before this
    # fix — see settings.py's cluster_worker_restart_error docstring for why
    # Worker needs this restart at all).
    monkeypatch.setattr(
        settings_route,
        "restart_worker",
        lambda: {"restarted": True, "new_pid": 55556, "error": None},
    )
    # The route under test mutates the real `settings` singleton directly
    # (not via monkeypatch) for all CLUSTER_ENV_NAMES fields. Priming each
    # one with monkeypatch.setattr(self-value) here registers pytest's
    # teardown to restore whatever the ORIGINAL value was, regardless of the
    # route's later direct assignment — otherwise this test would leak
    # mutated `settings` state into every test that runs after it in this
    # session (Review Story 5.1).
    for field in settings_route.CLUSTER_ENV_NAMES:
        monkeypatch.setattr(settings, field, getattr(settings, field))
    # ssh_key_path is a server-side setting, not a submitted form field —
    # pin it explicitly to what this test expects the connection test/save
    # to see.
    monkeypatch.setattr(settings, "ssh_key_path", str(key_file))

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cluster",
        data=_cluster_form_data(ceph_mon_nodes="9.9.9.1, 9.9.9.2"),
    )

    assert response.status_code == 200
    assert "Kết nối thành công" in response.text
    assert "55555" in response.text
    # Connection was tested with the SUBMITTED cluster values, plus the
    # server-configured (not submitted) ssh_key_path.
    assert captured_query_args == [(["9.9.9.1", "9.9.9.2"], "ceph-mon-B", "root", str(key_file), "docker")]
    env_text = tmp_env.read_text()
    assert "CEPH_MON_NODES=9.9.9.1, 9.9.9.2" in env_text
    # ssh_key_path is never written by this route — it's not part of
    # CLUSTER_ENV_NAMES anymore.
    assert "SSH_KEY_PATH" not in env_text
    assert settings.ceph_mon_nodes == "9.9.9.1, 9.9.9.2"
    assert settings.ssh_key_path == str(key_file)


def test_post_cluster_settings_watcher_restart_failure_still_reports_saved(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    key_file = tmp_path / "test_key"
    key_file.write_text("fake key")

    monkeypatch.setattr(
        settings_route,
        "query_cluster_health_with",
        lambda *a, **kw: {"status": "HEALTH_OK", "checks": {}},
    )
    monkeypatch.setattr(
        settings_route,
        "restart_watcher",
        lambda: {"restarted": False, "new_pid": None, "error": "boom"},
    )
    monkeypatch.setattr(
        settings_route,
        "restart_worker",
        lambda: {"restarted": True, "new_pid": 1, "error": None},
    )
    for field in settings_route.CLUSTER_ENV_NAMES:
        monkeypatch.setattr(settings, field, getattr(settings, field))

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cluster", data=_cluster_form_data(ssh_key_path=str(key_file))
    )

    assert response.status_code == 200
    assert "Kết nối thành công" in response.text
    assert "khởi động lại thủ công" in response.text
    # Config save itself must not be reported as failed just because restart was.
    assert "CEPH_MON_NODES" in tmp_env.read_text()


# --- Multi-deploy-mode support: ceph_exec_mode (docker/podman/none) --------


def test_post_cluster_settings_none_mode_does_not_require_container_name(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    key_file = tmp_path / "test_key"
    key_file.write_text("fake key")

    captured = []
    monkeypatch.setattr(
        settings_route,
        "query_cluster_health_with",
        lambda *a, **kw: captured.append((a, kw)) or {"status": "HEALTH_OK", "checks": {}},
    )
    monkeypatch.setattr(
        settings_route, "restart_watcher", lambda: {"restarted": True, "new_pid": 1, "error": None}
    )
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None}
    )
    for field in settings_route.CLUSTER_ENV_NAMES:
        monkeypatch.setattr(settings, field, getattr(settings, field))

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cluster",
        data=_cluster_form_data(
            ceph_container_name="", ceph_osd_container_name="", ceph_exec_mode="none",
            ssh_key_path=str(key_file),
        ),
    )

    assert response.status_code == 200
    assert "Kết nối thành công" in response.text
    assert "CEPH_EXEC_MODE=none" in tmp_env.read_text()
    assert captured  # the connection test still ran, just without a container name


def test_post_cluster_settings_cephadm_mode_does_not_require_container_name(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    captured = []
    monkeypatch.setattr(
        settings_route,
        "query_cluster_health_with",
        lambda *a, **kw: captured.append((a, kw)) or {"status": "HEALTH_OK", "checks": {}},
    )
    monkeypatch.setattr(
        settings_route, "restart_watcher", lambda: {"restarted": True, "new_pid": 1, "error": None}
    )
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None}
    )
    for field in settings_route.CLUSTER_ENV_NAMES:
        monkeypatch.setattr(settings, field, getattr(settings, field))

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cluster",
        data=_cluster_form_data(
            ceph_container_name="", ceph_osd_container_name="", ceph_exec_mode="cephadm",
            ceph_mgr_nodes="10.20.1.112",
        ),
    )

    assert response.status_code == 200
    assert "Kết nối thành công" in response.text
    assert "CEPH_EXEC_MODE=cephadm" in tmp_env.read_text()
    assert "CEPH_MGR_NODES=10.20.1.112" in tmp_env.read_text()
    assert captured  # the connection test still ran, just without a container name
    # exec_mode is passed through to the connection test as the 5th positional arg.
    assert captured[0][0][4] == "cephadm"


def test_post_cluster_settings_docker_mode_still_requires_container_name(
    dashboard_client, monkeypatch
):
    called = []
    monkeypatch.setattr(
        settings_route, "query_cluster_health_with", lambda *a, **kw: called.append(1)
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cluster",
        data=_cluster_form_data(ceph_container_name="", ceph_exec_mode="docker"),
    )

    assert response.status_code == 200
    assert "Vui lòng điền đủ" in response.text
    assert called == []


def test_post_cluster_settings_rejects_unknown_exec_mode(dashboard_client, monkeypatch):
    called = []
    monkeypatch.setattr(
        settings_route, "query_cluster_health_with", lambda *a, **kw: called.append(1)
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cluster", data=_cluster_form_data(ceph_exec_mode="docker-compose")
    )

    assert response.status_code == 200
    assert "Kiểu deploy không hợp lệ" in response.text
    assert called == []


def test_get_settings_renders_current_exec_mode_as_selected(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "podman", raising=False)

    _login(dashboard_client)
    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert 'value="podman" selected' in response.text


def test_get_settings_renders_cephadm_mode_and_mgr_nodes(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm", raising=False)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "10.20.1.112,10.20.1.95", raising=False)

    _login(dashboard_client)
    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert 'value="cephadm" selected' in response.text
    assert "10.20.1.112,10.20.1.95" in response.text


# --- Review Story 5.1: embedded-newline injection guard + cross-form -------
# independence (findings from Blind Hunter / Edge Case Hunter / Acceptance
# Auditor) --------------------------------------------------------------


def test_post_cluster_settings_rejects_embedded_newline_and_does_not_write_env(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\nSESSION_SECRET_KEY=untouched\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    key_file = tmp_path / "test_key"
    key_file.write_text("fake key")
    monkeypatch.setattr(
        settings_route,
        "query_cluster_health_with",
        lambda *a, **kw: {"status": "HEALTH_OK", "checks": {}},
    )

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cluster",
        data=_cluster_form_data(
            ceph_container_name="ceph-mon-B\nSESSION_SECRET_KEY=pwned", ssh_key_path=str(key_file)
        ),
    )

    assert response.status_code == 200
    env_text = tmp_env.read_text()
    assert "SESSION_SECRET_KEY=untouched" in env_text  # not overwritten
    assert "pwned" not in env_text


def test_cluster_form_submission_does_not_leak_into_9router_form_state(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/cluster", data=_cluster_form_data(ceph_mon_nodes="")
    )

    assert response.status_code == 200
    assert "Vui lòng điền đủ" in response.text  # cluster_error IS shown
    assert "API key không được để trống" not in response.text  # 9router form's error stays unset


def test_9router_form_submission_does_not_leak_into_cluster_form_state(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    # Blank now falls back to whatever's already configured — pin it blank
    # too so a blank submission still hits the "nothing configured" error.
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save", data={"router_api_key": "   ", "router_model_id": "gc/gemini-2.5-pro"}
    )

    assert response.status_code == 200
    assert "API key không được để trống" in response.text
    assert "Vui lòng điền đủ" not in response.text  # cluster form's error stays unset


def test_post_settings_save_router_empty_key_shows_error(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    # Blank now falls back to whatever's already configured (see the
    # "keep existing key" test above) — pin it blank too so this still
    # tests the genuine "nothing configured at all yet" case.
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)

    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/9router/save", data={"router_api_key": "   ", "router_model_id": "gc/gemini-2.5-pro"}
    )

    assert response.status_code == 200
    assert "không được để trống" in response.text


def test_mask_key_shows_only_last_few_chars():
    masked = settings_route._mask_key("sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    assert masked.startswith("...")
    assert "sk-ant-api03" not in masked
    assert masked.endswith("wxyz")
    assert masked == "...wxyz"


@pytest.mark.parametrize("short_key", ["a", "ab", "abc", "abcd", "abcde", "abcdefghijklmnop"])
def test_mask_key_never_reveals_the_full_key_even_when_short(short_key):
    masked = settings_route._mask_key(short_key)
    # This was a real AC violation before: `_mask_key` used to return
    # "..." + the entire key for anything <= 12 chars, i.e. fully unmasked.
    assert masked != "..." + short_key


@pytest.mark.live
def test_worker_process_management_against_a_real_spawned_process(monkeypatch):
    """Real OS-level test for _find_worker_pids/_start_worker/_stop_worker —
    every other test in this file mocks these three; this one actually
    spawns, finds, and kills a real `worker.main` subprocess."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        log_path = f.name
    monkeypatch.setattr(settings_route, "WORKER_LOG_PATH", settings_route.Path(log_path))

    assert settings_route._find_worker_pids() == [], "test must start with no worker.main running"

    pid = settings_route._start_worker()
    try:
        assert settings_route._pid_alive(pid)
        found = settings_route._find_worker_pids()
        assert pid in found
    finally:
        settings_route._stop_worker([pid])

    assert not settings_route._pid_alive(pid)
    assert pid not in settings_route._find_worker_pids()


# --- Dashboard self-restart (POST /settings/restart-dashboard) -------------


def test_unauthenticated_restart_dashboard_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/settings/restart-dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_restart_dashboard_launches_watchdog_with_request_host_and_port(dashboard_client, monkeypatch):
    captured = []
    monkeypatch.setattr(
        settings_route,
        "restart_dashboard_process",
        lambda host, port: captured.append((host, port)),
    )

    _login(dashboard_client)
    response = dashboard_client.post("/settings/restart-dashboard")

    assert response.status_code == 200
    assert "Restarting" in response.text
    assert len(captured) == 1
    host, port = captured[0]
    # TestClient's default base URL is http://testserver — confirms host/port
    # are actually derived from the incoming request, not hardcoded.
    assert host == "testserver"


def test_restart_dashboard_reports_error_when_watchdog_launch_fails(dashboard_client, monkeypatch):
    def fake_restart(host, port):
        raise OSError("could not write watchdog script")

    monkeypatch.setattr(settings_route, "restart_dashboard_process", fake_restart)

    _login(dashboard_client)
    response = dashboard_client.post("/settings/restart-dashboard")

    assert response.status_code == 200
    assert "Không khởi động lại được" in response.text


def test_dashboard_restart_script_contains_pid_host_port_and_execs_uvicorn():
    script = settings_route._dashboard_restart_script(12345, "10.20.1.5", 8000)

    assert "kill 12345" in script
    assert "kill -0 12345" in script
    assert "--host 10.20.1.5 --port 8000" in script
    assert script.strip().endswith(
        f"{settings_route.shlex.quote(str(settings_route.DASHBOARD_LOG_PATH))} 2>&1"
    )


# --- Admin-only restart controls (Worker/Watcher/Dashboard) ----------------
#
# 2026-07-24: this app now has real per-account roles (shared/models.py::User)
# instead of a single hardcoded account — these tests log in as an actual
# DB-created non-admin user to exercise the "not admin" branch, rather than
# monkeypatching an internal constant.


def test_require_admin_privilege_allows_the_env_account():
    settings_route._require_admin_privilege("admin")  # must not raise


def test_require_admin_privilege_rejects_unknown_username():
    with pytest.raises(Exception) as exc_info:
        settings_route._require_admin_privilege("someone-else")
    assert getattr(exc_info.value, "status_code", None) == 403


def test_get_settings_shows_restart_controls_for_admin(dashboard_client):
    _login(dashboard_client)  # logs in as "admin" (TEST_USERNAME)

    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert "Tiến trình hệ thống" in response.text
    assert 'action="/settings/restart-worker"' in response.text
    assert 'action="/settings/restart-watcher"' in response.text
    assert 'action="/settings/restart-dashboard"' in response.text


def test_get_settings_hides_restart_controls_for_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert "Tiến trình hệ thống" not in response.text
    assert "Kết nối Database" not in response.text
    assert 'href="/users"' not in response.text
    assert 'action="/settings/restart-worker"' not in response.text
    assert 'action="/settings/restart-dashboard"' not in response.text


def test_restart_worker_route_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post("/settings/restart-worker")

    assert response.status_code == 403


def test_restart_watcher_route_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post("/settings/restart-watcher")

    assert response.status_code == 403


def test_restart_dashboard_route_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post("/settings/restart-dashboard")

    assert response.status_code == 403


# --- Reset DB connection (2026-07-24) ---------------------------------------


def _use_file_based_test_db(monkeypatch, tmp_path):
    """The dashboard_client fixture's default DB is an in-memory sqlite
    engine with StaticPool (one shared connection — see conftest.py's
    docstring on why: :memory: sqlite is otherwise per-connection). Resetting
    the connection disposes that ONE connection, which would drop the
    in-memory database entirely — so these two tests specifically need a
    file-based sqlite instead (settings.database_url, which
    _reset_database_connection's db.make_engine() actually reads), so the
    fresh engine it creates points at the same real file, schema intact."""
    from sqlalchemy import create_engine

    from shared.db import Base

    db_path = tmp_path / "reset_test.db"
    file_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", file_url)
    Base.metadata.create_all(create_engine(file_url))


def test_reset_database_connection_success(dashboard_client, monkeypatch, tmp_path):
    _use_file_based_test_db(monkeypatch, tmp_path)
    _login(dashboard_client)
    old_engine = db_module.engine

    response = dashboard_client.post("/settings/database/reset-connection")

    assert response.status_code == 200
    assert "Đã khởi động lại kết nối" in response.text
    assert db_module.engine is not old_engine


def test_reset_database_connection_keeps_working_afterward(dashboard_client, monkeypatch, tmp_path):
    _use_file_based_test_db(monkeypatch, tmp_path)
    _login(dashboard_client)
    dashboard_client.post("/settings/database/reset-connection")

    # the fresh engine/session must still be fully usable — e.g. the very
    # next page load, which queries the DB, must not fail
    response = dashboard_client.get("/settings")

    assert response.status_code == 200


def test_reset_database_connection_route_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post("/settings/database/reset-connection")

    assert response.status_code == 403


def test_unauthenticated_reset_database_connection_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/settings/database/reset-connection", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- Run migrations / "Chạy migration (upgrade head)" (2026-07-28) ---------
# Real end-to-end regression test for the exact bug this button exists to
# fix: an old deployment's database missing a table newer code already
# references (shared/models.py::VolumeMetric, added after code was pulled
# without also running `alembic upgrade head` by hand) — cleanup_submit
# crashed with psycopg.errors.UndefinedTable against a real production DB
# from exactly this gap.


def test_migrate_database_route_adds_missing_table(dashboard_client, monkeypatch, tmp_path):
    import sqlite3

    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    db_path = tmp_path / "migrate_test.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")

    # Build the file DB at the revision BEFORE volume_metrics existed —
    # simulates "code got updated, database didn't" exactly.
    cfg = AlembicConfig(str(settings_route.ALEMBIC_INI_PATH))
    cfg.set_main_option("script_location", str(settings_route.ALEMBIC_SCRIPT_LOCATION))
    alembic_command.upgrade(cfg, "9f1c2a7d5e3b")

    con = sqlite3.connect(db_path)
    tables_before = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "volume_metrics" not in tables_before

    _login(dashboard_client)
    response = dashboard_client.post("/settings/database/migrate")

    assert response.status_code == 200
    assert "Đã chạy migration thành công" in response.text

    con = sqlite3.connect(db_path)
    tables_after = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert "volume_metrics" in tables_after


def test_migrate_database_route_does_not_switch_database_url(dashboard_client, monkeypatch, tmp_path):
    # Same url before and after — this button must never behave like
    # settings_save_database's actual database SWITCH.
    db_path = tmp_path / "migrate_test.db"
    file_url = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings, "database_url", file_url)
    from sqlalchemy import create_engine

    from shared.db import Base

    Base.metadata.create_all(create_engine(file_url))

    _login(dashboard_client)
    dashboard_client.post("/settings/database/migrate")

    assert settings.database_url == file_url


def test_migrate_database_route_reports_error_on_failure(dashboard_client, monkeypatch):
    def broken_upgrade(url):
        raise RuntimeError("boom")

    monkeypatch.setattr(settings_route, "_run_alembic_upgrade_head", broken_upgrade)
    _login(dashboard_client)

    response = dashboard_client.post("/settings/database/migrate")

    assert response.status_code == 200
    assert "Chạy migration thất bại" in response.text


def test_migrate_database_route_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post("/settings/database/migrate")

    assert response.status_code == 403


def test_unauthenticated_migrate_database_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/settings/database/migrate", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- Patch pipeline settings (2026-07-24) -----------------------------------


def test_get_settings_shows_patch_pipeline_tab_for_admin(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert "Build &amp; Copy Patch Ceph" in response.text
    assert 'action="/settings/patch-pipeline"' in response.text


def test_get_settings_hides_patch_pipeline_tab_for_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert 'action="/settings/patch-pipeline"' not in response.text


def test_patch_pipeline_settings_submit_persists_and_restarts_worker(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    restart_calls = []
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: restart_calls.append(1) or {"restarted": True, "new_pid": 1, "error": None}
    )
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/patch-pipeline",
        data={
            "ceph_patch_build_node": "10.0.0.20",
            "ceph_patch_source_dir": "/root/ceph",
            "ceph_patch_build_command": "./make-srpm.sh && rpmbuild --rebuild x.src.rpm",
            "ceph_patch_output_dir": "/root/rpmbuild/RPMS/x86_64",
            "ceph_patch_node_staging_dir": "/opt/ceph-aiops-patch-staging",
        },
    )

    assert response.status_code == 200
    assert "Đã lưu cấu hình" in response.text
    assert settings.ceph_patch_build_node == "10.0.0.20"
    assert settings.ceph_patch_source_dir == "/root/ceph"
    assert settings.ceph_patch_build_command == "./make-srpm.sh && rpmbuild --rebuild x.src.rpm"
    assert settings.ceph_patch_output_dir == "/root/rpmbuild/RPMS/x86_64"
    assert settings.ceph_patch_node_staging_dir == "/opt/ceph-aiops-patch-staging"
    env_contents = tmp_env.read_text()
    assert "CEPH_PATCH_BUILD_NODE=10.0.0.20" in env_contents
    assert restart_calls == [1]


def test_patch_pipeline_settings_submit_shows_error_on_write_failure(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / "no-such-dir" / ".env")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/patch-pipeline",
        data={"ceph_patch_build_node": "10.0.0.20"},
    )

    assert response.status_code == 200
    assert "Không ghi được file cấu hình" in response.text


def test_patch_pipeline_settings_submit_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post(
        "/settings/patch-pipeline", data={"ceph_patch_build_node": "10.0.0.20"}
    )

    assert response.status_code == 403


def test_unauthenticated_patch_pipeline_settings_submit_redirects_to_login(dashboard_client):
    response = dashboard_client.post(
        "/settings/patch-pipeline", data={"ceph_patch_build_node": "10.0.0.20"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# Note: user-management ("Users") tests live in test_dashboard_users.py —
# that feature is its own standalone page (/users), not a Settings card,
# as of 2026-07-24.


# --- Lưu trữ Backup (Epic 9, Story 9.2's backend + follow-up Settings UI) ---


def _backup_target_payload(**overrides):
    payload = {
        "backup_target_a_transport": "ssh",
        "backup_target_a_label": "NAS tại chỗ",
        "backup_target_a_ssh_host": "10.20.2.50",
        "backup_target_a_ssh_user": "backup",
        "backup_target_a_ssh_key_path": "/root/.ssh/backup_a_key",
        "backup_target_a_ssh_landing_dir": "/backup/ceph-aiops",
        "backup_target_a_s3_endpoint": "",
        "backup_target_a_s3_access_key": "",
        "backup_target_a_s3_secret_key": "",
        "backup_target_a_s3_bucket": "",
        "backup_target_a_immutable_lock_days": "7",
        "backup_target_b_transport": "s3",
        "backup_target_b_label": "S3 ngoài",
        "backup_target_b_ssh_host": "",
        "backup_target_b_ssh_user": "",
        "backup_target_b_ssh_key_path": "",
        "backup_target_b_ssh_landing_dir": "",
        "backup_target_b_s3_endpoint": "",
        "backup_target_b_s3_access_key": "AKIAEXAMPLE",
        "backup_target_b_s3_secret_key": "supersecret123",
        "backup_target_b_s3_bucket": "ceph-aiops-backups",
        "backup_target_b_immutable_lock_days": "14",
    }
    payload.update(overrides)
    return payload


def test_get_settings_shows_backup_targets_tab_for_admin(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert "Lưu trữ Backup" in response.text
    assert 'action="/settings/backup-targets"' in response.text


def test_get_settings_hides_backup_targets_tab_for_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert 'action="/settings/backup-targets"' not in response.text


def test_backup_targets_settings_submit_persists_both_slots_and_restarts_worker(
    dashboard_client, monkeypatch, tmp_path
):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    restart_calls = []
    monkeypatch.setattr(
        settings_route,
        "restart_worker",
        lambda: restart_calls.append(1) or {"restarted": True, "new_pid": 1, "error": None},
    )
    _login(dashboard_client)

    response = dashboard_client.post("/settings/backup-targets", data=_backup_target_payload())

    assert response.status_code == 200
    assert "Đã lưu cấu hình" in response.text
    assert settings.backup_target_a_transport == "ssh"
    assert settings.backup_target_a_ssh_host == "10.20.2.50"
    assert settings.backup_target_b_transport == "s3"
    assert settings.backup_target_b_s3_access_key == "AKIAEXAMPLE"
    assert settings.backup_target_b_s3_secret_key == "supersecret123"
    assert settings.backup_target_b_immutable_lock_days == 14
    env_contents = tmp_env.read_text()
    assert "BACKUP_TARGET_A_SSH_HOST=10.20.2.50" in env_contents
    assert "BACKUP_TARGET_B_S3_BUCKET=ceph-aiops-backups" in env_contents
    assert restart_calls == [1]


def test_backup_targets_settings_submit_blank_secret_keeps_existing_value(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None})
    monkeypatch.setattr(settings, "backup_target_b_s3_secret_key", "already-saved-secret", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/backup-targets", data=_backup_target_payload(backup_target_b_s3_secret_key="")
    )

    assert response.status_code == 200
    assert settings.backup_target_b_s3_secret_key == "already-saved-secret"


def test_backup_targets_settings_submit_rejects_incomplete_ssh_slot(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/backup-targets",
        data=_backup_target_payload(backup_target_a_ssh_host=""),
    )

    assert response.status_code == 200
    assert "Slot A" in response.text
    assert "Host, User, SSH key path, Thư mục lưu trữ" in response.text


def test_backup_targets_settings_submit_rejects_incomplete_s3_slot(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/backup-targets",
        data=_backup_target_payload(backup_target_b_s3_bucket=""),
    )

    assert response.status_code == 200
    assert "Access key, Secret key, Bucket" in response.text


def test_backup_targets_settings_submit_allows_unconfigured_slot(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    monkeypatch.setattr(settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 1, "error": None})
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/backup-targets",
        data=_backup_target_payload(
            backup_target_b_transport="",
            backup_target_b_s3_access_key="",
            backup_target_b_s3_secret_key="",
            backup_target_b_s3_bucket="",
        ),
    )

    assert response.status_code == 200
    assert "Đã lưu cấu hình" in response.text
    assert settings.backup_target_b_transport == ""


def test_backup_targets_settings_submit_rejects_invalid_immutable_days(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/backup-targets",
        data=_backup_target_payload(backup_target_a_immutable_lock_days="0"),
    )

    assert response.status_code == 200
    assert "immutable" in response.text.lower()


def test_backup_targets_settings_submit_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post("/settings/backup-targets", data=_backup_target_payload())

    assert response.status_code == 403


def test_unauthenticated_backup_targets_settings_submit_redirects_to_login(dashboard_client):
    response = dashboard_client.post(
        "/settings/backup-targets", data=_backup_target_payload(), follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_restart_worker_route_shows_success_message(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        settings_route, "restart_worker", lambda: {"restarted": True, "new_pid": 4242, "error": None}
    )
    _login(dashboard_client)

    response = dashboard_client.post("/settings/restart-worker")

    assert response.status_code == 200
    assert "Đã khởi động lại Worker (PID 4242)" in response.text


def test_restart_worker_route_shows_error_message_on_failure(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        settings_route,
        "restart_worker",
        lambda: {"restarted": False, "new_pid": None, "error": "boom"},
    )
    _login(dashboard_client)

    response = dashboard_client.post("/settings/restart-worker")

    assert response.status_code == 200
    assert "boom" in response.text


def test_restart_watcher_route_shows_success_message(dashboard_client, monkeypatch):
    monkeypatch.setattr(
        settings_route, "restart_watcher", lambda: {"restarted": True, "new_pid": 4343, "error": None}
    )
    _login(dashboard_client)

    response = dashboard_client.post("/settings/restart-watcher")

    assert response.status_code == 200
    assert "Đã khởi động lại Watcher (PID 4343)" in response.text


def test_unauthenticated_restart_worker_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/settings/restart-worker", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_unauthenticated_restart_watcher_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/settings/restart-watcher", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- 9router model picker (Step 2 <select>) --------------------------------


def test_get_settings_shows_currently_configured_model_as_sole_initial_option(
    dashboard_client, monkeypatch
):
    # The model <select> has no fixed/hardcoded option list — it starts with
    # just whatever's currently configured, and gets populated with the
    # router's REAL available models only after "Xác nhận kết nối" succeeds
    # (settings.js).
    monkeypatch.setattr(settings, "router_model", "gc/gemini-2.5-pro", raising=False)

    _login(dashboard_client)
    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert '<option value="gc/gemini-2.5-pro" selected>' in response.text


def test_get_settings_shows_configured_base_url_prefilled(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "router_base_url", "http://localhost:20128", raising=False)

    _login(dashboard_client)
    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert 'id="router-base-url-input"' in response.text
    assert 'value="http://localhost:20128"' in response.text


def test_get_settings_shows_connected_display_state_when_fully_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "router_api_key", "sk-connected-key", raising=False)
    monkeypatch.setattr(settings, "router_base_url", "http://localhost:20128", raising=False)
    monkeypatch.setattr(settings, "router_model", "gc/gemini-2.5-pro", raising=False)
    monkeypatch.setattr(settings, "router_enabled", True, raising=False)

    _login(dashboard_client)
    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert "đã kết nối" in response.text
    assert 'id="router-connected-view"' in response.text


def test_get_settings_shows_wizard_when_not_fully_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    monkeypatch.setattr(settings, "router_enabled", False, raising=False)

    _login(dashboard_client)
    response = dashboard_client.get("/settings")

    assert response.status_code == 200
    assert "Xác nhận kết nối" in response.text


# --- POST /settings/9router/verify (Step 1 "[Xác nhận kết nối]" button) ---


def test_unauthenticated_post_settings_9router_verify_redirects_to_login(dashboard_client):
    response = dashboard_client.post("/settings/9router/verify", data={}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_post_settings_9router_verify_requires_an_api_key(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "router_api_key", "", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/settings/9router/verify", data={})

    assert response.status_code == 400


def test_post_settings_9router_verify_requires_a_base_url(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "router_base_url", "", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/9router/verify", data={"router_api_key": "sk-real-key"}
    )

    assert response.status_code == 400
    assert "Base URL" in response.json()["detail"]


def test_post_settings_9router_verify_reports_valid_key_and_models(dashboard_client, monkeypatch):
    async def fake_verify(api_key, base_url):
        assert api_key == "sk-real-key"
        assert base_url == "http://localhost:20128"
        return True, "Kết nối thành công — tìm thấy 2 model", ["gc/gemini-2.5-flash", "gc/gemini-2.5-pro"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/9router/verify",
        data={"router_api_key": "sk-real-key", "router_base_url": "http://localhost:20128"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "valid": True,
        "message": "Kết nối thành công — tìm thấy 2 model",
        "models": ["gc/gemini-2.5-flash", "gc/gemini-2.5-pro"],
    }


def test_post_settings_9router_verify_reports_invalid_key_with_reason(dashboard_client, monkeypatch):
    async def fake_verify(api_key, base_url):
        return False, "API key không hợp lệ", None

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/9router/verify",
        data={"router_api_key": "sk-wrong", "router_base_url": "http://localhost:20128"},
    )

    assert response.status_code == 200
    assert response.json() == {"valid": False, "message": "API key không hợp lệ", "models": None}


def test_post_settings_9router_verify_never_saves_anything(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)

    async def fake_verify(api_key, base_url):
        return True, "Kết nối thành công — tìm thấy 1 model", ["gc/gemini-2.5-flash"]

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    _login(dashboard_client)

    dashboard_client.post(
        "/settings/9router/verify",
        data={"router_api_key": "sk-should-not-be-saved", "router_base_url": "http://localhost:20128"},
    )

    assert "ROUTER_API_KEY" not in tmp_env.read_text()
    assert settings.router_api_key != "sk-should-not-be-saved"


def test_post_settings_9router_verify_blank_key_falls_back_to_configured_one(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "router_api_key", "sk-already-configured", raising=False)
    captured = []

    async def fake_verify(api_key, base_url):
        captured.append(api_key)
        return True, "Kết nối thành công — tìm thấy 0 model", []

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    _login(dashboard_client)

    dashboard_client.post(
        "/settings/9router/verify", data={"router_base_url": "http://localhost:20128"}
    )

    assert captured == ["sk-already-configured"]


def test_post_settings_9router_verify_passes_submitted_base_url_through(dashboard_client, monkeypatch):
    # router_base_url IS a real, honored form field on this page (the
    # operator's own 9router URL) — a submitted value must reach
    # verify_router_connection, not silently fall back to whatever's saved.
    monkeypatch.setattr(settings, "router_base_url", "http://localhost:20128", raising=False)
    captured = []

    async def fake_verify(api_key, base_url):
        captured.append(base_url)
        return True, "ok", []

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    _login(dashboard_client)

    dashboard_client.post(
        "/settings/9router/verify",
        data={
            "router_api_key": "sk-real-key",
            "router_base_url": "http://a-different-router.example",
        },
    )

    assert captured == ["http://a-different-router.example"]


def test_post_settings_9router_verify_blank_base_url_falls_back_to_configured_one(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "router_base_url", "http://localhost:20128", raising=False)
    captured = []

    async def fake_verify(api_key, base_url):
        captured.append(base_url)
        return True, "ok", []

    monkeypatch.setattr(settings_route, "verify_router_connection", fake_verify)
    _login(dashboard_client)

    dashboard_client.post(
        "/settings/9router/verify", data={"router_api_key": "sk-real-key"}
    )

    assert captured == ["http://localhost:20128"]


# --- verify_router_connection() itself (independent of the route) ---------


def test_verify_router_connection_reports_missing_config_gracefully():
    # shared/router_client.py::build_router_client raises
    # RouterNotConfiguredError when base_url is blank (no direct-to-vendor
    # fallback, by policy) — verify_router_connection's generic except must
    # turn that into a normal (False, reason, None) result, not propagate.
    import asyncio

    is_valid, reason, models = asyncio.run(
        settings_route.verify_router_connection("sk-real-key", "")
    )

    assert is_valid is False
    assert "API AI" in reason
    assert models is None


def test_verify_router_connection_maps_authentication_error_to_invalid_key(monkeypatch):
    import asyncio

    async def failing_list_models(api_key, base_url):
        response = httpx.Response(401, request=httpx.Request("GET", "http://x"))
        raise openai.AuthenticationError("bad key", response=response, body=None)

    monkeypatch.setattr(settings_route, "list_router_models", failing_list_models)

    is_valid, reason, models = asyncio.run(
        settings_route.verify_router_connection("sk-real-key", "http://localhost:20128")
    )

    assert is_valid is False
    assert reason == "API key không hợp lệ"
    assert models is None


def test_verify_router_connection_maps_connection_error_to_host_port_hint(monkeypatch):
    import asyncio

    async def failing_list_models(api_key, base_url):
        raise openai.APIConnectionError(request=httpx.Request("GET", "http://x"))

    monkeypatch.setattr(settings_route, "list_router_models", failing_list_models)

    is_valid, reason, models = asyncio.run(
        settings_route.verify_router_connection("sk-real-key", "http://localhost:20128")
    )

    assert is_valid is False
    assert "http://localhost:20128" in reason
    assert models is None


def test_verify_router_connection_maps_unexpected_error_to_false_with_reason(monkeypatch):
    import asyncio

    async def failing_list_models(api_key, base_url):
        raise RuntimeError("router listing endpoint hiccup")

    monkeypatch.setattr(settings_route, "list_router_models", failing_list_models)

    is_valid, reason, models = asyncio.run(
        settings_route.verify_router_connection("sk-real-key", "http://localhost:20128")
    )

    assert is_valid is False
    assert reason == "router listing endpoint hiccup"
    assert models is None


def test_verify_router_connection_reports_model_count_on_success(monkeypatch):
    import asyncio

    async def fake_list_models(api_key, base_url):
        return ["gc/gemini-2.5-flash", "gc/gemini-2.5-pro"]

    monkeypatch.setattr(settings_route, "list_router_models", fake_list_models)

    is_valid, reason, models = asyncio.run(
        settings_route.verify_router_connection("sk-real-key", "http://localhost:20128")
    )

    assert is_valid is True
    assert reason == "Kết nối thành công — tìm thấy 2 model"
    assert models == ["gc/gemini-2.5-flash", "gc/gemini-2.5-pro"]


@pytest.mark.live
def test_verify_router_connection_rejects_garbage_key_against_real_router():
    import asyncio

    is_valid, _reason, _models = asyncio.run(
        settings_route.verify_router_connection(
            "sk-definitely-not-a-real-key", settings.router_base_url
        )
    )
    assert is_valid is False
