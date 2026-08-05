import shared.telegram_alerts as telegram_alerts
from shared.telegram_client import TelegramSendError


def _configure(monkeypatch, *, incident_enabled=False, node_enabled=False, token="123:ABC", chat_id="-100999"):
    monkeypatch.setattr(telegram_alerts.settings, "telegram_bot_token", token, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_chat_id", chat_id, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_alerts_enabled", incident_enabled, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_node_alerts_enabled", node_enabled, raising=False)


# --- send_incident_alert ----------------------------------------------------


def test_send_incident_alert_sends_when_enabled_and_configured(monkeypatch):
    _configure(monkeypatch, incident_enabled=True)
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


def test_send_incident_alert_skips_when_disabled(monkeypatch):
    _configure(monkeypatch, incident_enabled=False)
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *a: calls.append(a))

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")

    assert calls == []


def test_send_incident_alert_skips_when_enabled_but_not_configured(monkeypatch):
    _configure(monkeypatch, incident_enabled=True, token="", chat_id="")
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *a: calls.append(a))

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")

    assert calls == []


def test_send_incident_alert_truncates_long_excerpt(monkeypatch):
    _configure(monkeypatch, incident_enabled=True)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert("SLOW_OPS", "HEALTH_WARN", "x" * 5000)

    assert len(calls[0]) < 1000


def test_send_incident_alert_swallows_send_failure(monkeypatch):
    _configure(monkeypatch, incident_enabled=True)

    def _boom(token, chat_id, text):
        raise TelegramSendError("bad token")

    monkeypatch.setattr(telegram_alerts, "send_telegram_message", _boom)

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")  # must not raise


# --- send_node_alert ---------------------------------------------------------


def test_send_node_alert_sends_when_enabled_and_configured(monkeypatch):
    _configure(monkeypatch, node_enabled=True)
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


def test_send_node_alert_skips_when_disabled(monkeypatch):
    _configure(monkeypatch, node_enabled=False)
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *a: calls.append(a))

    telegram_alerts.send_node_alert("10.0.0.5", "CPU 95%")

    assert calls == []


def test_send_node_alert_swallows_send_failure(monkeypatch):
    _configure(monkeypatch, node_enabled=True)

    def _boom(token, chat_id, text):
        raise TelegramSendError("chat not found")

    monkeypatch.setattr(telegram_alerts, "send_telegram_message", _boom)

    telegram_alerts.send_node_alert("10.0.0.5", "CPU 95%")  # must not raise


def test_incident_and_node_toggles_are_independent(monkeypatch):
    """Turning ON node alerts must never also send an incident alert, and
    vice versa — the whole point of splitting these into separate
    categories."""
    _configure(monkeypatch, incident_enabled=False, node_enabled=True)
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text))

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")
    telegram_alerts.send_node_alert("10.0.0.5", "CPU 95%")

    assert len(calls) == 1
    assert "10.0.0.5" in calls[0]
