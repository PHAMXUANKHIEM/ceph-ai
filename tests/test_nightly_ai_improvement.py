from worker import nightly_ai_improvement


def test_bootstrap_failure_alert_uses_environment_without_settings(monkeypatch):
    messages = []
    monkeypatch.setenv("TELEGRAM_CODE_REPAIR_BOT_TOKEN", "123456:abcdefghijklmnopqrstuv")
    monkeypatch.setenv("TELEGRAM_CODE_REPAIR_CHAT_ID", "42")
    monkeypatch.setattr(
        nightly_ai_improvement,
        "send_telegram_message",
        lambda token, chat_id, text: messages.append((token, chat_id, text)),
    )

    nightly_ai_improvement._notify_bootstrap_failure(RuntimeError("invalid configuration"))

    assert messages == [(
        "123456:abcdefghijklmnopqrstuv", "42",
        "⚠️ AI NIGHTLY IMPROVEMENT KHÔNG KHỞI ĐỘNG\nLỗi cấu hình/khởi động: invalid configuration",
    )]
