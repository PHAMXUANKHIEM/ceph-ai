import bcrypt

import dashboard.routes.telegram_alerts as telegram_alerts_route
from config.settings import settings
from shared import db as db_module
from shared import env_config
from shared.models import User


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


def _mock_restarts(monkeypatch):
    calls = {"worker": 0, "watcher": 0}
    monkeypatch.setattr(
        telegram_alerts_route,
        "restart_worker",
        lambda: calls.__setitem__("worker", calls["worker"] + 1) or {"restarted": True, "new_pid": 1, "error": None},
    )
    monkeypatch.setattr(
        telegram_alerts_route,
        "restart_watcher",
        lambda: calls.__setitem__("watcher", calls["watcher"] + 1) or {"restarted": True, "new_pid": 2, "error": None},
    )
    return calls


# --- GET /telegram-alerts ----------------------------------------------------


def test_unauthenticated_get_telegram_alerts_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/telegram-alerts", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_telegram_alerts_shows_5_channel_cards_for_admin(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/telegram-alerts")

    assert response.status_code == 200
    assert "Cảnh báo Backup" in response.text
    assert "Cảnh báo lỗi cụm" in response.text
    assert "Cảnh báo phần cứng" in response.text
    assert "AI Code Repair — sửa hệ thống" in response.text
    assert "Cảnh báo RGW — AI phân tích" in response.text
    assert 'action="/telegram-alerts/backup"' in response.text
    assert 'action="/telegram-alerts/incident"' in response.text
    assert 'action="/telegram-alerts/node"' in response.text
    assert 'action="/telegram-alerts/code-repair"' in response.text
    assert 'action="/telegram-alerts/rgw"' in response.text
    assert "NOTIFICATION-ONLY" in response.text
    assert "Muốn tách khỏi chat Hardware?" in response.text
    assert 'href="/telegram-alerts/help#code-repair-private-chat"' in response.text
    assert "Gửi thử — AI Code Repair — sửa hệ thống" in response.text


def test_get_telegram_alerts_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.get("/telegram-alerts")

    assert response.status_code == 403


def test_nav_link_hidden_for_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert 'href="/telegram-alerts"' not in response.text


def test_nav_link_visible_for_admin(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert 'href="/telegram-alerts"' in response.text


# --- GET /telegram-alerts/help ----------------------------------------------


def test_get_telegram_alerts_help_page_renders(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.get("/telegram-alerts/help")

    assert response.status_code == 200
    assert "BotFather" in response.text
    assert "getUpdates" in response.text
    assert 'id="code-repair-private-chat"' in response.text
    assert "Tách riêng Chat ID cho AI Code Repair" in response.text
    assert "CODE_REPAIR_TOKEN" in response.text


def test_get_telegram_alerts_help_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.get("/telegram-alerts/help")

    assert response.status_code == 403


# --- POST /telegram-alerts/{channel} ----------------------------------------


def test_submit_backup_channel_persists_config_and_restarts_only_worker(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    restart_calls = _mock_restarts(monkeypatch)
    # The route mutates config.settings.settings DIRECTLY (real persisted
    # config, not test-local state) — pre-registering these with monkeypatch
    # (even to their current value) means its teardown restores them
    # afterward regardless of that direct mutation. Without this, a fake
    # "real-looking" token/chat id leaks into the process-wide singleton for
    # the rest of the whole test session: dashboard/telegram_approval_bot.py's
    # background threads read this SAME singleton and would treat the leaked
    # value as a genuinely-configured channel (2026-08-06 incident — this
    # exact leak, combined with per-token listener threads, was found to
    # spawn real network-calling threads that starved unrelated tests
    # suite-wide; see that module's own _listen_supervisor_loop docstring).
    monkeypatch.setattr(settings, "telegram_backup_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_backup_chat_id", "", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/telegram-alerts/backup",
        data={"bot_token": "123456:AAExampleToken", "chat_id": "-1001234567890"},
    )

    assert response.status_code == 200
    assert "Đã lưu" in response.text
    assert settings.telegram_backup_bot_token == "123456:AAExampleToken"
    assert settings.telegram_backup_chat_id == "-1001234567890"
    env_contents = tmp_env.read_text()
    assert "TELEGRAM_BACKUP_BOT_TOKEN=123456:AAExampleToken" in env_contents
    assert "TELEGRAM_BACKUP_CHAT_ID=-1001234567890" in env_contents
    assert restart_calls == {"worker": 1, "watcher": 0}


def test_submit_incident_channel_restarts_only_watcher(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    restart_calls = _mock_restarts(monkeypatch)
    # See the leak note in test_submit_backup_channel_... above.
    monkeypatch.setattr(settings, "telegram_incident_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_chat_id", "", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/telegram-alerts/incident",
        data={"bot_token": "123456:AAExampleToken", "chat_id": "-1001234567890"},
    )

    assert response.status_code == 200
    assert settings.telegram_incident_bot_token == "123456:AAExampleToken"
    assert restart_calls == {"worker": 0, "watcher": 1}


def test_submit_rbd_forecast_channel_is_independent_and_restarts_watcher(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    restart_calls = _mock_restarts(monkeypatch)
    monkeypatch.setattr(settings, "telegram_rbd_forecast_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_rbd_forecast_chat_id", "", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/telegram-alerts/rbd-forecast",
        data={"bot_token": "456:RBDToken", "chat_id": "-100777"},
    )

    assert response.status_code == 200
    assert settings.telegram_rbd_forecast_bot_token == "456:RBDToken"
    assert settings.telegram_rbd_forecast_chat_id == "-100777"
    assert "TELEGRAM_RBD_FORECAST_BOT_TOKEN=456:RBDToken" in tmp_env.read_text()
    assert "TELEGRAM_RBD_FORECAST_CHAT_ID=-100777" in tmp_env.read_text()
    assert restart_calls == {"worker": 0, "watcher": 1}


def test_submit_node_channel_restarts_only_watcher(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    restart_calls = _mock_restarts(monkeypatch)
    # See the leak note in test_submit_backup_channel_... above.
    monkeypatch.setattr(settings, "telegram_node_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_node_chat_id", "", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/telegram-alerts/node",
        data={"bot_token": "123456:AAExampleToken", "chat_id": "-1001234567890"},
    )

    assert response.status_code == 200
    assert settings.telegram_node_bot_token == "123456:AAExampleToken"
    assert restart_calls == {"worker": 0, "watcher": 1}


def test_submit_code_repair_channel_needs_no_service_restart(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    restart_calls = _mock_restarts(monkeypatch)
    monkeypatch.setattr(settings, "telegram_code_repair_bot_token", "")
    monkeypatch.setattr(settings, "telegram_code_repair_chat_id", "")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/telegram-alerts/code-repair",
        data={"bot_token": "123456:RepairToken", "chat_id": "-100888"},
    )

    assert response.status_code == 200
    assert settings.telegram_code_repair_bot_token == "123456:RepairToken"
    assert settings.telegram_code_repair_chat_id == "-100888"
    assert "TELEGRAM_CODE_REPAIR_BOT_TOKEN=123456:RepairToken" in tmp_env.read_text()
    assert "TELEGRAM_CODE_REPAIR_CHAT_ID=-100888" in tmp_env.read_text()
    assert restart_calls == {"worker": 0, "watcher": 0}


def test_submit_rgw_channel_persists_separate_config(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    restart_calls = _mock_restarts(monkeypatch)
    monkeypatch.setattr(settings, "telegram_rgw_bot_token", "")
    monkeypatch.setattr(settings, "telegram_rgw_chat_id", "")
    _login(dashboard_client)

    response = dashboard_client.post(
        "/telegram-alerts/rgw",
        data={"bot_token": "123456:RGWToken", "chat_id": "-100777"},
    )

    assert response.status_code == 200
    assert settings.telegram_rgw_bot_token == "123456:RGWToken"
    assert settings.telegram_rgw_chat_id == "-100777"
    assert "TELEGRAM_RGW_BOT_TOKEN=123456:RGWToken" in tmp_env.read_text()
    assert "TELEGRAM_RGW_CHAT_ID=-100777" in tmp_env.read_text()
    assert restart_calls == {"worker": 0, "watcher": 1}


def test_submit_channels_are_independent(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    _mock_restarts(monkeypatch)
    # See the leak note in test_submit_backup_channel_... above — guard
    # BOTH the channel being submitted and the one asserted to stay blank.
    monkeypatch.setattr(settings, "telegram_backup_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_backup_chat_id", "", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_chat_id", "", raising=False)
    _login(dashboard_client)

    dashboard_client.post(
        "/telegram-alerts/incident",
        data={"bot_token": "123456:AAExampleToken", "chat_id": "-1001234567890"},
    )

    assert settings.telegram_incident_bot_token == "123456:AAExampleToken"
    # Configuring "incident" must never also configure "backup".
    assert settings.telegram_backup_bot_token == ""
    assert settings.telegram_backup_chat_id == ""


def test_submit_blank_bot_token_keeps_existing_value(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    _mock_restarts(monkeypatch)
    # See the leak note in test_submit_backup_channel_... above.
    monkeypatch.setattr(settings, "telegram_backup_chat_id", "", raising=False)
    monkeypatch.setattr(settings, "telegram_backup_bot_token", "already-saved-token", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/telegram-alerts/backup",
        data={"bot_token": "", "chat_id": "-100999"},
    )

    assert response.status_code == 200
    assert settings.telegram_backup_bot_token == "already-saved-token"
    assert settings.telegram_backup_chat_id == "-100999"


def test_submit_rejects_unknown_channel(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post(
        "/telegram-alerts/not-a-real-channel",
        data={"bot_token": "x", "chat_id": "-100999"},
    )

    assert response.status_code == 404


def test_submit_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post(
        "/telegram-alerts/backup",
        data={"bot_token": "x", "chat_id": "-100999"},
    )

    assert response.status_code == 403


def test_unauthenticated_submit_redirects_to_login(dashboard_client):
    response = dashboard_client.post(
        "/telegram-alerts/backup",
        data={"bot_token": "x", "chat_id": "-100999"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- POST /telegram-alerts/{channel}/toggle ----------------------------------
# 2026-08-07: separate per-channel Bật/Tắt (operator request) -- unlike
# /telegram-alerts/{channel} above, this must NEVER touch the saved Bot
# Token/Chat ID, only the `*_enabled` flag.


def test_toggle_backup_channel_off_persists_and_restarts_only_worker(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    restart_calls = _mock_restarts(monkeypatch)
    # See the leak note on test_submit_backup_channel_... above -- same
    # direct-mutation-of-the-process-wide-singleton risk applies to
    # `enabled` too.
    monkeypatch.setattr(settings, "telegram_backup_bot_token", "123:ABC", raising=False)
    monkeypatch.setattr(settings, "telegram_backup_chat_id", "-100999", raising=False)
    monkeypatch.setattr(settings, "telegram_backup_enabled", True, raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/telegram-alerts/backup/toggle", data={"enabled": "false"})

    assert response.status_code == 200
    assert "Đã tắt" in response.text
    assert settings.telegram_backup_enabled is False
    # Bot Token/Chat ID themselves must be untouched by a toggle.
    assert settings.telegram_backup_bot_token == "123:ABC"
    assert settings.telegram_backup_chat_id == "-100999"
    assert "TELEGRAM_BACKUP_ENABLED=false" in tmp_env.read_text()
    assert restart_calls == {"worker": 1, "watcher": 0}


def test_toggle_incident_channel_on_restarts_only_watcher(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    restart_calls = _mock_restarts(monkeypatch)
    monkeypatch.setattr(settings, "telegram_incident_bot_token", "123:ABC", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_chat_id", "-100999", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_enabled", False, raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/telegram-alerts/incident/toggle", data={"enabled": "true"})

    assert response.status_code == 200
    assert "Đã bật" in response.text
    assert settings.telegram_incident_enabled is True
    assert restart_calls == {"worker": 0, "watcher": 1}


def test_toggle_performance_rca_off_persists_and_restarts_only_watcher(dashboard_client, monkeypatch, tmp_path):
    tmp_env = tmp_path / ".env"
    tmp_env.write_text("DASHBOARD_USERNAME=admin\n")
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_env)
    restart_calls = _mock_restarts(monkeypatch)
    monkeypatch.setattr(settings, "telegram_performance_rca_enabled", True, raising=False)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/telegram-alerts/performance-rca/toggle", data={"enabled": "false"}
    )

    assert response.status_code == 200
    assert "Đã tắt cảnh báo Performance RCA" in response.text
    assert settings.telegram_performance_rca_enabled is False
    assert "TELEGRAM_PERFORMANCE_RCA_ENABLED=false" in tmp_env.read_text()
    assert restart_calls == {"worker": 0, "watcher": 1}


def test_toggle_node_channel_restarts_only_watcher(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    restart_calls = _mock_restarts(monkeypatch)
    monkeypatch.setattr(settings, "telegram_node_bot_token", "123:ABC", raising=False)
    monkeypatch.setattr(settings, "telegram_node_chat_id", "-100999", raising=False)
    monkeypatch.setattr(settings, "telegram_node_enabled", True, raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/telegram-alerts/node/toggle", data={"enabled": "false"})

    assert response.status_code == 200
    assert settings.telegram_node_enabled is False
    assert restart_calls == {"worker": 0, "watcher": 1}


def test_toggle_rejects_unknown_channel(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/telegram-alerts/not-a-real-channel/toggle", data={"enabled": "false"})
    assert response.status_code == 404


def test_toggle_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")
    response = dashboard_client.post("/telegram-alerts/backup/toggle", data={"enabled": "false"})
    assert response.status_code == 403


def test_unauthenticated_toggle_redirects_to_login(dashboard_client):
    response = dashboard_client.post(
        "/telegram-alerts/backup/toggle", data={"enabled": "false"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_get_telegram_alerts_shows_status_label_per_channel(dashboard_client, monkeypatch, tmp_path):
    monkeypatch.setattr(env_config, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(settings, "telegram_backup_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_backup_chat_id", "", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_bot_token", "123:ABC", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_chat_id", "-100999", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_enabled", True, raising=False)
    monkeypatch.setattr(settings, "telegram_node_bot_token", "123:ABC", raising=False)
    monkeypatch.setattr(settings, "telegram_node_chat_id", "-100999", raising=False)
    monkeypatch.setattr(settings, "telegram_node_enabled", False, raising=False)
    _login(dashboard_client)

    response = dashboard_client.get("/telegram-alerts")

    assert response.status_code == 200
    assert "Chưa cấu hình" in response.text  # backup: no token/chat id yet
    assert "Đang bật" in response.text  # incident: configured + enabled
    assert "Đã tắt" in response.text  # node: configured but disabled


# --- POST /telegram-alerts/{channel}/test -----------------------------------


def test_test_sends_message_using_saved_config(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_backup_bot_token", "123:ABC", raising=False)
    monkeypatch.setattr(settings, "telegram_backup_chat_id", "-100999", raising=False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts_route,
        "send_telegram_message",
        lambda token, chat_id, text: calls.append((token, chat_id, text)),
    )
    _login(dashboard_client)

    response = dashboard_client.post("/telegram-alerts/backup/test")

    assert response.status_code == 200
    assert "Đã gửi tin nhắn thử" in response.text
    assert len(calls) == 1
    assert calls[0][0] == "123:ABC"
    assert calls[0][1] == "-100999"


def test_test_shows_error_when_not_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_incident_bot_token", "", raising=False)
    monkeypatch.setattr(settings, "telegram_incident_chat_id", "", raising=False)
    _login(dashboard_client)

    response = dashboard_client.post("/telegram-alerts/incident/test")

    assert response.status_code == 200
    assert "Chưa lưu Bot token" in response.text


def test_test_shows_error_on_send_failure(dashboard_client, monkeypatch):
    monkeypatch.setattr(settings, "telegram_node_bot_token", "123:ABC", raising=False)
    monkeypatch.setattr(settings, "telegram_node_chat_id", "-100999", raising=False)

    def _boom(token, chat_id, text):
        raise telegram_alerts_route.TelegramSendError("chat not found")

    monkeypatch.setattr(telegram_alerts_route, "send_telegram_message", _boom)
    _login(dashboard_client)

    response = dashboard_client.post("/telegram-alerts/node/test")

    assert response.status_code == 200
    assert "Gửi thử thất bại" in response.text
    assert "chat not found" in response.text


def test_test_rejects_non_admin(dashboard_client):
    _create_user("regular", "s3cret-pw", is_admin=False)
    _login_as(dashboard_client, "regular", "s3cret-pw")

    response = dashboard_client.post("/telegram-alerts/backup/test")

    assert response.status_code == 403


def test_test_rejects_unknown_channel(dashboard_client):
    _login(dashboard_client)

    response = dashboard_client.post("/telegram-alerts/not-a-real-channel/test")

    assert response.status_code == 404
