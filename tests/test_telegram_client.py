import pytest

import shared.telegram_client as telegram_client
from shared.telegram_client import TelegramSendError, send_telegram_message


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}
        self.text = text

    def json(self):
        return self._body


def test_send_telegram_message_posts_to_correct_url_and_payload(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return FakeResponse()

    monkeypatch.setattr(telegram_client.httpx, "post", fake_post)

    send_telegram_message("123:ABC", "-100999", "hello")

    assert len(calls) == 1
    url, payload, timeout = calls[0]
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert payload == {"chat_id": "-100999", "text": "hello"}
    assert timeout == telegram_client.TELEGRAM_TIMEOUT_SECONDS


def test_send_telegram_message_raises_when_token_blank():
    with pytest.raises(TelegramSendError):
        send_telegram_message("", "-100999", "hello")


def test_send_telegram_message_raises_when_chat_id_blank():
    with pytest.raises(TelegramSendError):
        send_telegram_message("123:ABC", "", "hello")


def test_send_telegram_message_raises_on_network_failure(monkeypatch):
    def fake_post(url, json, timeout):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(telegram_client.httpx, "post", fake_post)

    with pytest.raises(TelegramSendError):
        send_telegram_message("123:ABC", "-100999", "hello")


def test_send_telegram_message_raises_on_non_200_status(monkeypatch):
    monkeypatch.setattr(
        telegram_client.httpx,
        "post",
        lambda url, json, timeout: FakeResponse(status_code=401, body={"ok": False, "description": "Unauthorized"}),
    )

    with pytest.raises(TelegramSendError, match="Unauthorized"):
        send_telegram_message("bad-token", "-100999", "hello")


def test_send_telegram_message_raises_when_ok_is_false_despite_200(monkeypatch):
    """Verified against Telegram's real API behavior for some rejections
    (e.g. a chat id the bot was never added to) — still HTTP 200, but the
    JSON body's own "ok" field is false, which a bare raise_for_status()
    style check would miss entirely."""
    monkeypatch.setattr(
        telegram_client.httpx,
        "post",
        lambda url, json, timeout: FakeResponse(
            status_code=200, body={"ok": False, "description": "chat not found"}
        ),
    )

    with pytest.raises(TelegramSendError, match="chat not found"):
        send_telegram_message("123:ABC", "-100999", "hello")
