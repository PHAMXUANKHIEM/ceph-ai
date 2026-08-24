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


def test_reminder_includes_vietnamese_ai_summary_and_solution(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert(
        "OSD_DOWN",
        "HEALTH_ERR",
        "osd.2 down",
        reminder=True,
        diagnosis_text="OSD.2 đã dừng do tiến trình bị lỗi.",
        rationale="Khởi động lại daemon OSD.2 để phục hồi dịch vụ.",
    )

    assert "🔁 NHẮC LẠI" in calls[0]
    assert "🧠 Tóm tắt AI: OSD.2 đã dừng do tiến trình bị lỗi." in calls[0]
    assert "🔧 Giải pháp: Khởi động lại daemon OSD.2 để phục hồi dịch vụ." in calls[0]


def test_successful_restart_sends_explicit_ok_notification(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_auto_remediation_alert(
        "OSD_DOWN", "osd.0 down", "Restart daemon", "systemctl restart ceph-osd@0",
        True, action_id="restart_osd_daemon", target_nodes='["10.3.53.1"]',
    )

    assert "✅ Khởi động lại thành công" in calls[0]
    assert "10.3.53.1" in calls[0]
    assert "đang xác minh Ceph" in calls[0]


def test_update_failure_alert_contains_error_ai_summary_and_rollback(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_update_failure_alert(
        "CLUSTER_UPGRADE",
        "Gói Ceph trên node MON bị xung đột phiên bản.",
        "Node 10.0.0.1, bước install: package conflict",
        "Đã dừng rollout và gỡ cờ noout.",
    )

    assert "CẬP NHẬT THẤT BẠI" in calls[0]
    assert "🧠 Tóm tắt AI: Gói Ceph trên node MON bị xung đột phiên bản." in calls[0]
    assert "❌ Lỗi cụ thể: Node 10.0.0.1" in calls[0]
    assert "↩️ Rollback: Đã dừng rollout và gỡ cờ noout." in calls[0]


def test_send_trash_capacity_alert_uses_incident_channel(monkeypatch):
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True, raising=False)
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *args: calls.append(args))

    telegram_alerts.send_trash_capacity_alert(25, 100, 0.25, 3)

    assert len(calls) == 1
    assert "vượt ngưỡng 20%" in calls[0][2]
    assert "Đề xuất" in calls[0][2]


# --- send_node_alert ---------------------------------------------------------


def test_send_vitastor_alert_uses_incident_channel_and_cluster_name(monkeypatch):
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True, raising=False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message",
        lambda token, chat_id, text: calls.append((token, chat_id, text)),
    )

    telegram_alerts.send_vitastor_alert("vita-prod", "CRITICAL", "OSD 2/3 up")

    assert calls[0][0:2] == ("123:ABC", "-100999")
    assert "Cụm: vita-prod" in calls[0][2]
    assert "Cụm Vitastor" in calls[0][2]
    assert "OSD 2/3 up" in calls[0][2]


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


def test_log_finding_alert_includes_recommendations_and_review_commands(monkeypatch):
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args: calls.append(args))
    calls = []
    telegram_alerts.send_log_finding_alert(
        "RGW key invalid", "WARNING", "HIGH", "Key is AES256", "Sai định dạng",
        recommended_steps=["Khuyến nghị tốt nhất: gỡ khóa sai"],
        operator_commands=["ceph config dump"], daemon_types=["rgw"],
    )
    text = calls[0][3]
    assert "💡 Gợi ý xử lý:" in text
    assert "Khuyến nghị tốt nhất: gỡ khóa sai" in text
    assert "Lệnh kiểm tra đề xuất (chưa tự chạy)" in text


def test_pending_default_key_alert_includes_best_option(monkeypatch):
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args: calls.append(args))
    calls = []
    telegram_alerts.send_log_finding_recovery_pending_alert(
        "RGW key invalid", "still invalid", ("ceph_health=HEALTH_OK",),
        verification_code="RGW_DEFAULT_ENCRYPTION_KEY_INVALID",
    )
    text = calls[0][3]
    assert "Gợi ý tốt nhất" in text
    assert "Vault SSE-S3" in text
    assert "Phương án khác" in text
