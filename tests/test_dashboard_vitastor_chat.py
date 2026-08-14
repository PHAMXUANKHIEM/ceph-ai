from config.settings import settings
from shared import db
from shared.models import ChatMessage
import dashboard.routes.vitastor_chat as route


def _login(client):
    return client.post("/login", data={"username": "admin", "password": "admin", "product": "vitastor"})


def test_vitastor_home_contains_scoped_chatbox(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/vitastor")
    assert response.status_code == 200
    assert 'data-api-prefix="/vitastor/api/chat"' in response.text
    assert 'href="/vitastor/settings"' in response.text


def test_vitastor_chat_is_separate_and_guides_to_settings(dashboard_client, monkeypatch):
    _login(dashboard_client)
    monkeypatch.setattr(settings, "vitastor_router_enabled", False)
    monkeypatch.setattr(settings, "vitastor_codex_chat_enabled", False)
    monkeypatch.setattr(settings, "vitastor_claude_chat_enabled", False)
    response = dashboard_client.post("/vitastor/api/chat/messages", json={"content": "Vitastor là gì?"})
    assert response.status_code == 200
    payload = response.json()
    assert "Chưa kết nối AI" in payload["assistant_message"]["content"]
    with db.SessionLocal() as session:
        rows = session.query(ChatMessage).all()
        assert len(rows) == 2
        assert all(row.actor.startswith("vita:") for row in rows)
        assert all(row.actor != "admin" for row in rows)


def test_vitastor_chat_uses_activated_codex_account(dashboard_client, monkeypatch):
    async def run_turn(prompt, tools, handler):
        assert "Vitastor" in prompt
        assert tools == []
        return {"reply_text": "Phản hồi từ Codex"}
    monkeypatch.setattr(route.codex_app_server, "run_turn", run_turn)
    monkeypatch.setattr(settings, "vitastor_codex_chat_enabled", True)
    monkeypatch.setattr(settings, "vitastor_claude_chat_enabled", False)
    _login(dashboard_client)
    response = dashboard_client.post("/vitastor/api/chat/messages", json={"content": "Kiểm tra cluster"})
    assert response.status_code == 200
    assert response.json()["assistant_message"]["content"] == (
        "Mình yêu ơi, em là AI. Phản hồi từ Codex"
    )
