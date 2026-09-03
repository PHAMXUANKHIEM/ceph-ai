import pytest

import shared.telegram_client as telegram_client
from shared.telegram_client import (
    TelegramSendError,
    answer_telegram_callback,
    edit_telegram_message,
    get_telegram_updates,
    send_telegram_message,
    send_telegram_message_with_keyboard,
    sanitize_telegram_text,
)


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


def test_all_outbound_text_is_sanitized_at_client_boundary(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_client.httpx, "post",
        lambda url, json, timeout: calls.append(json) or FakeResponse(),
    )
    secret_token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    text = (
        "Vault\x00 failed\x01 secret_key=supersecret " + secret_token
        + " [SQL: INSERT INTO x VALUES (1)] [parameters: {'password': 'hidden'}]"
    )

    send_telegram_message("123:ABC", "-100999", text)

    sent = calls[0]["text"]
    assert "\x00" not in sent and "\x01" not in sent
    assert "supersecret" not in sent
    assert secret_token not in sent
    assert "INSERT INTO" not in sent and "[parameters:" not in sent
    assert "<TELEGRAM_BOT_TOKEN>" in sent
    assert "[chi tiết SQL và parameters đã ẩn]" in sent


def test_telegram_text_is_capped_without_cutting_suffix():
    result = sanitize_telegram_text("x" * 5000)
    assert len(result) <= telegram_client.TELEGRAM_TEXT_LIMIT
    assert result.endswith("[đã rút gọn để phù hợp giới hạn Telegram]")


def test_httpx_log_filter_redacts_bot_token_url():
    record = __import__("logging").LogRecord(
        "httpx", 20, __file__, 1, "HTTP Request: %s", (), None
    )
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    record.args = (f"https://api.telegram.org/bot{token}/sendMessage",)

    assert telegram_client._TelegramUrlRedactionFilter().filter(record) is True
    assert token not in record.getMessage()
    assert "bot<REDACTED>" in record.getMessage()


def test_network_error_never_exposes_bot_token(monkeypatch):
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    monkeypatch.setattr(
        telegram_client.httpx, "post",
        lambda url, json, timeout: (_ for _ in ()).throw(RuntimeError(f"failed URL {url}")),
    )

    with pytest.raises(TelegramSendError) as caught:
        send_telegram_message(token, "-100999", "hello")

    assert token not in str(caught.value)


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


# --- send_telegram_message_with_keyboard() ----------------------------------


def test_send_with_keyboard_posts_inline_keyboard_and_returns_message_id(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return FakeResponse(body={"ok": True, "result": {"message_id": 4242}})

    monkeypatch.setattr(telegram_client.httpx, "post", fake_post)

    message_id = send_telegram_message_with_keyboard(
        "123:ABC", "-100999", "Đề xuất X", [("✅ Duyệt", "approve:a1"), ("❌ Từ chối", "reject:a1")]
    )

    assert message_id == 4242
    url, payload, _timeout = calls[0]
    assert url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert payload["chat_id"] == "-100999"
    assert payload["text"] == "Đề xuất X"
    assert payload["reply_markup"]["inline_keyboard"] == [
        [{"text": "✅ Duyệt", "callback_data": "approve:a1"}, {"text": "❌ Từ chối", "callback_data": "reject:a1"}]
    ]


def test_send_with_keyboard_raises_when_not_configured():
    with pytest.raises(TelegramSendError):
        send_telegram_message_with_keyboard("", "-100999", "x", [("a", "b")])


def test_send_with_keyboard_raises_on_api_rejection(monkeypatch):
    monkeypatch.setattr(
        telegram_client.httpx,
        "post",
        lambda url, json, timeout: FakeResponse(status_code=200, body={"ok": False, "description": "bad chat"}),
    )

    with pytest.raises(TelegramSendError, match="bad chat"):
        send_telegram_message_with_keyboard("123:ABC", "-100999", "x", [("a", "b")])


# --- edit_telegram_message() -------------------------------------------------


def test_edit_message_posts_text_and_clears_keyboard(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return FakeResponse()

    monkeypatch.setattr(telegram_client.httpx, "post", fake_post)

    edit_telegram_message("123:ABC", "-100999", 4242, "Đã duyệt")

    url, payload, _timeout = calls[0]
    assert url == "https://api.telegram.org/bot123:ABC/editMessageText"
    assert payload == {
        "chat_id": "-100999",
        "message_id": 4242,
        "text": "Đã duyệt",
        "reply_markup": {"inline_keyboard": []},
    }


def test_edit_message_raises_when_not_configured():
    with pytest.raises(TelegramSendError):
        edit_telegram_message("", "-100999", 4242, "x")


# --- get_telegram_updates() --------------------------------------------------


def test_get_updates_includes_offset_and_allowed_updates(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json, timeout))
        return FakeResponse(body={"ok": True, "result": [{"update_id": 1}]})

    monkeypatch.setattr(telegram_client.httpx, "post", fake_post)

    updates = get_telegram_updates("123:ABC", 55, 30)

    assert updates == [{"update_id": 1}]
    url, payload, timeout = calls[0]
    assert url == "https://api.telegram.org/bot123:ABC/getUpdates"
    assert payload == {"timeout": 30, "allowed_updates": ["callback_query"], "offset": 55}
    # client-side timeout must exceed Telegram's own long-poll timeout.
    assert timeout.read > 30


def test_get_updates_omits_offset_on_first_call(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_client.httpx,
        "post",
        lambda url, json, timeout: calls.append(json) or FakeResponse(body={"ok": True, "result": []}),
    )

    get_telegram_updates("123:ABC", None, 30)

    assert "offset" not in calls[0]


def test_get_updates_returns_empty_list_when_result_missing(monkeypatch):
    monkeypatch.setattr(
        telegram_client.httpx, "post", lambda url, json, timeout: FakeResponse(body={"ok": True})
    )

    assert get_telegram_updates("123:ABC", None, 30) == []


def test_get_updates_treats_read_timeout_as_empty_long_poll(monkeypatch):
    def fake_post(url, json, timeout):
        raise telegram_client.httpx.ReadTimeout("long poll expired")

    monkeypatch.setattr(telegram_client.httpx, "post", fake_post)

    assert get_telegram_updates("123:ABC", None, 30) == []


def test_get_updates_raises_when_token_blank():
    with pytest.raises(TelegramSendError):
        get_telegram_updates("", None, 30)


# --- answer_telegram_callback() ----------------------------------------------


def test_answer_callback_posts_callback_id_and_text(monkeypatch):
    calls = []

    def fake_post(url, json, timeout):
        calls.append((url, json))
        return FakeResponse()

    monkeypatch.setattr(telegram_client.httpx, "post", fake_post)

    answer_telegram_callback("123:ABC", "cbid-1", "Đã duyệt")

    url, payload = calls[0]
    assert url == "https://api.telegram.org/bot123:ABC/answerCallbackQuery"
    assert payload == {"callback_query_id": "cbid-1", "text": "Đã duyệt"}


def test_answer_callback_omits_text_when_none(monkeypatch):
    calls = []
    monkeypatch.setattr(
        telegram_client.httpx, "post", lambda url, json, timeout: calls.append(json) or FakeResponse()
    )

    answer_telegram_callback("123:ABC", "cbid-1")

    assert calls[0] == {"callback_query_id": "cbid-1"}


def test_answer_callback_raises_when_token_blank():
    with pytest.raises(TelegramSendError):
        answer_telegram_callback("", "cbid-1")
