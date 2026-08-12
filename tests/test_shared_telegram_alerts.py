import shared.telegram_alerts as telegram_alerts
from shared.telegram_client import TelegramSendError


def _configure_incident(monkeypatch, *, token="123:ABC", chat_id="-100999"):
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_bot_token", token, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_chat_id", chat_id, raising=False)


def _configure_node(monkeypatch, *, token="123:ABC", chat_id="-100999"):
    monkeypatch.setattr(telegram_alerts.settings, "telegram_node_bot_token", token, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_node_chat_id", chat_id, raising=False)


# --- send_incident_alert ----------------------------------------------------


def test_send_incident_alert_sends_when_configured(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append((token, chat_id, text))
    )

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")

    assert len(calls) == 1
    token, chat_id, text = calls[0]
    assert token == "123:ABC"
    assert chat_id == "-100999"
    assert "MON_DOWN" in text
    assert "mon.a is down" in text
    assert "HEALTH_ERR" in text


def test_send_incident_alert_skips_when_not_configured(monkeypatch):
    _configure_incident(monkeypatch, token="", chat_id="")
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *a: calls.append(a))

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")

    assert calls == []


def test_send_incident_alert_skips_when_only_token_set(monkeypatch):
    _configure_incident(monkeypatch, token="123:ABC", chat_id="")
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *a: calls.append(a))

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")

    assert calls == []


def test_send_incident_alert_skips_when_disabled(monkeypatch):
    # 2026-08-07: `_enabled` is a SEPARATE toggle from token+chat_id being
    # set (Alert Telegram page's "Tắt kênh này" button) -- a fully
    # configured but disabled channel must send nothing.
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", False, raising=False)
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *a: calls.append(a))

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")

    assert calls == []


def test_send_incident_alert_truncates_long_excerpt(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert("SLOW_OPS", "HEALTH_WARN", "x" * 5000)

    assert len(calls[0]) < 1000


def test_send_incident_alert_compacts_multiline_metrics(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert("SLOW_OPS", "HEALTH_WARN", "IOPS: 10\n\n latency:   25 ms")

    assert "IOPS: 10 latency: 25 ms" in calls[0]


def test_send_incident_alert_swallows_send_failure(monkeypatch):
    _configure_incident(monkeypatch)

    def _boom(token, chat_id, text):
        raise TelegramSendError("bad token")

    monkeypatch.setattr(telegram_alerts, "send_telegram_message", _boom)

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")  # must not raise


def test_send_ai_incident_alert_sends_diagnosis_and_rationale(monkeypatch):
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True, raising=False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts,
        "send_telegram_message",
        lambda token, chat_id, text: calls.append((token, chat_id, text)),
    )

    telegram_alerts.send_ai_incident_alert(
        "POOL_TOO_FEW_PGS",
        "HEALTH_WARN",
        "Pool đang có số PG thấp.",
        "Kiểm tra pg_num và tăng dần theo tải.",
        cluster_name="CS-LAB",
    )

    assert len(calls) == 1
    token, chat_id, text = calls[0]
    assert token == "123:ABC"
    assert chat_id == "-100999"
    assert "Cụm: CS-LAB" in text
    assert "Ý kiến AI: Pool đang có số PG thấp." in text
    assert "Đề xuất: Kiểm tra pg_num và tăng dần theo tải." in text


# --- send_node_alert ---------------------------------------------------------


def test_send_node_alert_sends_when_configured(monkeypatch):
    _configure_node(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append((token, chat_id, text))
    )

    telegram_alerts.send_node_alert("10.0.0.5", "CPU 95%, RAM 60%")

    assert len(calls) == 1
    token, chat_id, text = calls[0]
    assert token == "123:ABC"
    assert "10.0.0.5" in text
    assert "CPU 95%" in text


def test_send_node_alert_skips_when_not_configured(monkeypatch):
    _configure_node(monkeypatch, token="", chat_id="")
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *a: calls.append(a))

    telegram_alerts.send_node_alert("10.0.0.5", "CPU 95%")

    assert calls == []


def test_send_node_alert_skips_when_disabled(monkeypatch):
    _configure_node(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_node_enabled", False, raising=False)
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *a: calls.append(a))

    telegram_alerts.send_node_alert("10.0.0.5", "CPU 95%")

    assert calls == []


def test_send_node_alert_swallows_send_failure(monkeypatch):
    _configure_node(monkeypatch)

    def _boom(token, chat_id, text):
        raise TelegramSendError("chat not found")

    monkeypatch.setattr(telegram_alerts, "send_telegram_message", _boom)

    telegram_alerts.send_node_alert("10.0.0.5", "CPU 95%")  # must not raise


def test_incident_and_node_channels_are_independent(monkeypatch):
    """Configuring only the node channel must never also send an incident
    alert, and vice versa — the whole point of splitting these into
    separate, independently-configured channels."""
    _configure_incident(monkeypatch, token="", chat_id="")
    _configure_node(monkeypatch, token="123:ABC", chat_id="-100999")
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text))

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")
    telegram_alerts.send_node_alert("10.0.0.5", "CPU 95%")

    assert len(calls) == 1
    assert "10.0.0.5" in calls[0]
