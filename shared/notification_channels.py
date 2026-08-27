"""Best-effort generic webhook, Slack and email alert delivery."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)
TIMEOUT_SECONDS = 10


def send_external_alert(*, category: str, severity: str, message: str, cluster_name: str = "") -> dict[str, bool]:
    payload = {
        "category": category,
        "severity": severity,
        "message": message,
        "cluster_name": cluster_name or None,
    }
    result = {"webhook": False, "slack": False, "email": False}
    if settings.alert_webhook_url:
        try:
            response = httpx.post(settings.alert_webhook_url, json=payload, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            result["webhook"] = True
        except Exception:
            logger.exception("generic alert webhook delivery failed")
    if settings.alert_slack_webhook_url:
        prefix = f"[{cluster_name}] " if cluster_name else ""
        try:
            response = httpx.post(
                settings.alert_slack_webhook_url,
                json={"text": f"{prefix}{severity.upper()} · {category}\n{message}"},
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            result["slack"] = True
        except Exception:
            logger.exception("Slack alert delivery failed")
    recipients = [item.strip() for item in settings.alert_email_to.split(",") if item.strip()]
    if settings.alert_email_smtp_host and settings.alert_email_from and recipients:
        email = EmailMessage()
        email["From"] = settings.alert_email_from
        email["To"] = ", ".join(recipients)
        email["Subject"] = f"[{severity.upper()}] {cluster_name + ' · ' if cluster_name else ''}{category}"
        email.set_content(message)
        try:
            with smtplib.SMTP(settings.alert_email_smtp_host, settings.alert_email_smtp_port, timeout=TIMEOUT_SECONDS) as smtp:
                if settings.alert_email_starttls:
                    smtp.starttls()
                if settings.alert_email_smtp_username:
                    smtp.login(settings.alert_email_smtp_username, settings.alert_email_smtp_password)
                smtp.send_message(email)
            result["email"] = True
        except Exception:
            logger.exception("email alert delivery failed")
    return result
