from types import SimpleNamespace

from config.settings import settings
from shared import notification_channels


def test_generic_and_slack_webhooks_are_independent(monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "https://hooks.example/generic")
    monkeypatch.setattr(settings, "alert_slack_webhook_url", "https://hooks.slack.example/one")
    monkeypatch.setattr(settings, "alert_email_smtp_host", "")
    calls = []
    monkeypatch.setattr(notification_channels.httpx, "post", lambda url, **kwargs: calls.append((url, kwargs["json"])) or SimpleNamespace(raise_for_status=lambda: None))
    result = notification_channels.send_external_alert(category="incident", severity="critical", message="OSD down", cluster_name="prod")
    assert result == {"webhook": True, "slack": True, "email": False}
    assert calls[0][1]["cluster_name"] == "prod"
    assert "CRITICAL" in calls[1][1]["text"]


def test_one_channel_failure_does_not_skip_the_other(monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "https://bad.example")
    monkeypatch.setattr(settings, "alert_slack_webhook_url", "https://good.example")
    monkeypatch.setattr(settings, "alert_email_smtp_host", "")
    def post(url, **kwargs):
        if "bad" in url:
            raise RuntimeError("down")
        return SimpleNamespace(raise_for_status=lambda: None)
    monkeypatch.setattr(notification_channels.httpx, "post", post)
    result = notification_channels.send_external_alert(category="node", severity="warning", message="hot")
    assert result["webhook"] is False
    assert result["slack"] is True


def test_invalid_email_header_is_contained(monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "")
    monkeypatch.setattr(settings, "alert_slack_webhook_url", "")
    monkeypatch.setattr(settings, "alert_email_smtp_host", "smtp.example")
    monkeypatch.setattr(settings, "alert_email_from", "alerts@example.com")
    monkeypatch.setattr(settings, "alert_email_to", "ops@example.com")
    result = notification_channels.send_external_alert(
        category="incident", severity="warning", message="x", cluster_name="bad\nBcc: victim@example.com"
    )
    assert result["email"] is False


def test_enqueue_is_non_blocking_and_bounded(monkeypatch):
    submitted = []
    monkeypatch.setattr(settings, "alert_webhook_url", "https://hooks.example")
    monkeypatch.setattr(notification_channels._EXECUTOR, "submit", lambda fn, **kwargs: submitted.append(kwargs) or SimpleNamespace(add_done_callback=lambda callback: None))
    assert notification_channels.enqueue_external_alert(category="node", severity="warning", message="hot") is True
    assert submitted[0]["category"] == "node"
