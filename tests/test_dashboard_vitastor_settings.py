from shared import db
from shared.models import VitastorCluster
import dashboard.routes.vitastor_chat as route
from config.settings import settings


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin", "product": "vitastor"})


def test_settings_contains_cluster_ai_telegram_and_database_panels(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/vitastor/settings")
    assert response.status_code == 200
    assert 'data-panel="cluster"' in response.text
    assert 'data-panel="ai"' in response.text
    assert 'data-panel="telegram"' in response.text
    assert 'data-panel="database"' in response.text
    assert 'data-panel="process-logs"' in response.text
    assert "Vitastor Watcher" in response.text
    assert "Vitastor Worker" in response.text
    assert 'action="/vitastor/settings/cluster/create"' in response.text
    assert 'action="/vitastor/settings/database/save"' in response.text
    assert 'action="/vitastor/settings/telegram"' in response.text
    assert 'id="vita-codex-login"' in response.text
    assert 'id="vita-claude-login"' in response.text


def test_vitastor_process_logs_can_filter_watcher_output(dashboard_client, monkeypatch, tmp_path):
    log_path = tmp_path / "watcher.log"
    log_path.write_text("INFO vitastor healthy\nERROR vitastor timeout\nINFO ceph healthy\n")
    monkeypatch.setitem(route.VITASTOR_PROCESS_LOGS, "watcher", log_path)
    _login(dashboard_client)

    response = dashboard_client.get("/vitastor/settings/process-logs?name=watcher&keyword=timeout")

    assert response.status_code == 200
    assert response.json()["lines"] == ["ERROR vitastor timeout"]


def test_vitastor_process_logs_reject_unknown_source(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/vitastor/settings/process-logs?name=arbitrary")
    assert response.status_code == 400


def test_admin_can_configure_vitastor_telegram_alerts(dashboard_client, monkeypatch):
    writes = []
    monkeypatch.setattr(route, "_update_env_file_batch", lambda values: writes.append(values))
    monkeypatch.setattr(settings, "telegram_incident_bot_token", "")
    monkeypatch.setattr(settings, "telegram_incident_chat_id", "")
    _login(dashboard_client)

    response = dashboard_client.post("/vitastor/settings/telegram", data={
        "bot_token": "123:ABC", "chat_id": "-100999", "enabled": "on",
    })

    assert response.status_code == 200
    assert "Đã lưu kênh cảnh báo Telegram" in response.text
    assert writes[-1]["TELEGRAM_INCIDENT_BOT_TOKEN"] == "123:ABC"
    assert settings.telegram_incident_enabled is True


def test_codex_account_can_be_activated_for_vitastor_only(dashboard_client, monkeypatch):
    async def account(): return {"email": "operator@example.com"}
    writes = []
    monkeypatch.setattr(route.codex_app_server, "account", account)
    monkeypatch.setattr(route, "_update_env_file_batch", lambda values: writes.append(values))
    monkeypatch.setattr(settings, "vitastor_codex_chat_enabled", False)
    monkeypatch.setattr(settings, "vitastor_claude_chat_enabled", True)
    _login(dashboard_client)
    response = dashboard_client.post("/vitastor/settings/codex/activate")
    assert response.status_code == 200
    assert settings.vitastor_codex_chat_enabled is True
    assert settings.vitastor_claude_chat_enabled is False
    assert writes[-1]["VITASTOR_CODEX_CHAT_ENABLED"] == "true"


def test_claude_account_can_be_activated_for_vitastor_only(dashboard_client, monkeypatch):
    async def status(): return {"installed": True, "authenticated": True, "email": "claude@example.com"}
    monkeypatch.setattr(route, "claude_status", status)
    monkeypatch.setattr(route, "_update_env_file_batch", lambda _values: None)
    monkeypatch.setattr(settings, "vitastor_claude_chat_enabled", False)
    _login(dashboard_client)
    response = dashboard_client.post("/vitastor/settings/claude/activate")
    assert response.status_code == 200
    assert settings.vitastor_claude_chat_enabled is True
    assert settings.vitastor_codex_chat_enabled is False


def test_admin_can_save_vitastor_cli_model_overrides(dashboard_client, monkeypatch):
    writes = []
    monkeypatch.setattr(route, "_update_env_file_batch", lambda values: writes.append(values))
    monkeypatch.setattr(settings, "vitastor_codex_chat_model", "")
    monkeypatch.setattr(settings, "vitastor_claude_chat_model", "")
    _login(dashboard_client)

    response = dashboard_client.post("/vitastor/settings/ai/cli-models", data={
        "codex_model": "vita-codex-model", "claude_model": "vita-claude-model",
    })

    assert response.status_code == 200
    assert settings.vitastor_codex_chat_model == "vita-codex-model"
    assert settings.vitastor_claude_chat_model == "vita-claude-model"
    assert writes[-1] == {
        "VITASTOR_CODEX_CHAT_MODEL": "vita-codex-model",
        "VITASTOR_CLAUDE_CHAT_MODEL": "vita-claude-model",
    }


def test_cluster_connection_can_be_added_inside_settings(dashboard_client, monkeypatch):
    monkeypatch.setattr(route, "query_status", lambda *_: {"osd_up": 1})
    _login(dashboard_client)
    response = dashboard_client.post("/vitastor/settings/cluster/create", data={
        "name": "settings-cluster", "management_host": "10.0.0.20",
        "etcd_address": "10.0.0.10:2379", "etcd_prefix": "/vitastor",
        "ssh_user": "root", "ssh_key_path": "/root/.ssh/vita", "exec_mode": "none",
    })
    assert response.status_code == 200
    assert "Đã kết nối cụm" in response.text
    with db.SessionLocal() as session:
        assert session.query(VitastorCluster).filter_by(name="settings-cluster").one()


def test_database_connection_check_is_available_in_vitastor_namespace(dashboard_client, monkeypatch):
    monkeypatch.setattr(route, "_test_database_connection", lambda _url: (True, "Kết nối thành công"))
    _login(dashboard_client)
    response = dashboard_client.post("/vitastor/settings/database/test", data={
        "database_url_raw": "postgresql://user:pw@db.internal:5432/control",
    })
    assert response.status_code == 200
    assert response.json() == {"valid": True, "message": "Kết nối thành công"}
