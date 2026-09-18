"""Dedicated, notification-only Telegram delivery for Vault security alerts."""
from __future__ import annotations
from config.settings import settings
from shared import env_config
from shared.notification_channels import enqueue_external_alert
from shared.telegram_alerts import send_managed_channel_alert, send_telegram_alert_with_ai

def send_vault_alert(title: str, severity: str, detail: str, remediation: str | None = None) -> bool:
    """Send only through the Vault channel; never fall back to another chat."""
    prefix = {"critical": "🚨", "warning": "⚠️", "info": "✅"}.get(severity, "⚠️")
    lines = [f"{prefix} VAULT · {title}", detail]
    if remediation:
        lines.append(f"🛠 Xử lý: {remediation}")
    text = "\n".join(line for line in lines if line)
    enqueue_external_alert(category="vault", severity=severity, message=text)
    live = env_config.read_env_values([
        "TELEGRAM_VAULT_BOT_TOKEN", "TELEGRAM_VAULT_CHAT_ID", "TELEGRAM_VAULT_ENABLED",
    ])
    bot_token = live.get("TELEGRAM_VAULT_BOT_TOKEN", settings.telegram_vault_bot_token)
    chat_id = live.get("TELEGRAM_VAULT_CHAT_ID", settings.telegram_vault_chat_id)
    enabled = live.get("TELEGRAM_VAULT_ENABLED", str(settings.telegram_vault_enabled)).lower() not in {"0", "false", "no", "off"}
    managed_sent = send_managed_channel_alert(text, category="vault")
    if not enabled or not bot_token or not chat_id:
        return managed_sent
    delivered = send_telegram_alert_with_ai(
        bot_token,
        chat_id,
        enabled,
        text,
        context=f"cảnh báo bảo mật Vault: {title}",
    )
    return delivered or managed_sent
