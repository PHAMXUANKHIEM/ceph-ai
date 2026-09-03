import logging

from shared import logging_redaction


def test_redact_log_text_scrubs_telegram_api_url_and_raw_token():
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    text = f"POST https://api.telegram.org/bot{token}/sendMessage failed token={token}"

    result = logging_redaction.redact_log_text(text)

    assert token not in result
    assert "https://api.telegram.org/bot<REDACTED>/sendMessage" in result
    assert "<TELEGRAM_BOT_TOKEN>" in result


def test_install_logging_redaction_scrubs_message_and_args(caplog):
    logging_redaction.install_logging_redaction()
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
    logger = logging.getLogger("tests.logging_redaction")

    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info(
            "request path=/bot%s/getUpdates url=%s",
            token,
            f"https://api.telegram.org/bot{token}/getUpdates",
        )

    rendered = caplog.text
    assert token not in rendered
    assert "/bot<REDACTED>/getUpdates" in rendered
    assert "https://api.telegram.org/bot<REDACTED>/getUpdates" in rendered


def test_install_logging_redaction_quiets_http_client_info_logs():
    logging_redaction.install_logging_redaction()

    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
