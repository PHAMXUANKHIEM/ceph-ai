import asyncio

import shared.telegram_alerts as telegram_alerts
import shared.telegram_humanizer as telegram_humanizer
from shared.telegram_client import TelegramSendError
from datetime import datetime


def _mock_humanizer_router(monkeypatch, content):
    monkeypatch.setattr(telegram_humanizer.settings, "telegram_ai_humanize_enabled", True)
    monkeypatch.setattr(telegram_humanizer.settings, "router_enabled", True)
    monkeypatch.setattr(telegram_humanizer.settings, "router_api_key", "configured")
    monkeypatch.setattr(telegram_humanizer.settings, "router_base_url", "http://router")
    monkeypatch.setattr(telegram_humanizer.settings, "router_model", "model")

    class Completions:
        async def create(self, **_kwargs):
            return type(
                "Response",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"message": type("Message", (), {"content": content})()},
                        )()
                    ]
                },
            )()

    class Client:
        chat = type("Chat", (), {"completions": Completions()})()

        async def close(self):
            return None

    monkeypatch.setattr(telegram_humanizer, "build_router_client", lambda *_args: Client())


def test_incident_alert_skips_humanizer_for_clean_short_excerpt(monkeypatch):
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_bot_token", "token")
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_chat_id", "chat")
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True)
    calls = []
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args: calls.append(args[3]))

    def unexpected_humanizer(*_args, **_kwargs):
        raise AssertionError("clean short excerpts must not call the humanizer")

    monkeypatch.setattr(telegram_alerts, "_humanize_sync", unexpected_humanizer)
    telegram_alerts.send_incident_alert(
        "MON_DOWN",
        "HEALTH_ERR",
        "Một Monitor đang không hoạt động.",
    )

    assert len(calls) == 1
    assert "Một Monitor đang không hoạt động." in calls[0]


def test_humanizer_accepts_short_vietnamese_response(monkeypatch):
    _mock_humanizer_router(
        monkeypatch,
        "OSD 2 đang không hoạt động trên node 10.20.1.195.",
    )

    result = asyncio.run(
        telegram_humanizer.humanize_log_for_telegram(
            "OSD 2 DOWN trên node 10.20.1.195", context="log gốc OSD_DOWN"
        )
    )

    assert result == "OSD 2 đang không hoạt động trên node 10.20.1.195."


def test_humanizer_rejects_machine_formatted_response(monkeypatch):
    _mock_humanizer_router(monkeypatch, "status: DOWN\nnode: 10.20.1.195")

    result = asyncio.run(
        telegram_humanizer.humanize_log_for_telegram(
            "OSD 2 DOWN", context="log gốc OSD_DOWN"
        )
    )

    assert result == "OSD 2 DOWN"


def test_humanizer_rejects_numbered_list_response(monkeypatch):
    _mock_humanizer_router(
        monkeypatch,
        "1. OSD 2 đang DOWN trên node 10.20.1.195.",
    )

    result = asyncio.run(
        telegram_humanizer.humanize_log_for_telegram(
            "OSD 2 DOWN trên node 10.20.1.195", context="log gốc OSD_DOWN"
        )
    )

    assert result == "OSD 2 DOWN trên node 10.20.1.195"


def test_humanizer_rejects_changed_protected_ceph_fact(monkeypatch):
    _mock_humanizer_router(
        monkeypatch,
        "OSD 3 đang không hoạt động trên node 10.20.1.195.",
    )

    result = asyncio.run(
        telegram_humanizer.humanize_log_for_telegram(
            "OSD 2 DOWN trên node 10.20.1.195", context="log gốc OSD_DOWN"
        )
    )

    assert result == "OSD 2 DOWN trên node 10.20.1.195"


def test_humanizer_rejects_mixed_english_response(monkeypatch):
    _mock_humanizer_router(
        monkeypatch,
        "OSD 2 is down trên node 10.20.1.195.",
    )

    result = asyncio.run(
        telegram_humanizer.humanize_log_for_telegram(
            "OSD 2 DOWN trên node 10.20.1.195", context="log gốc OSD_DOWN"
        )
    )

    assert result == "OSD 2 DOWN trên node 10.20.1.195"


def test_humanizer_rejects_english_only_response(monkeypatch):
    _mock_humanizer_router(monkeypatch, "OSD 2 is down.")

    result = asyncio.run(
        telegram_humanizer.humanize_log_for_telegram(
            "OSD 2 DOWN", context="log gốc OSD_DOWN"
        )
    )

    assert result == "OSD 2 DOWN"


def test_humanizer_rejects_more_than_three_sentences(monkeypatch):
    _mock_humanizer_router(
        monkeypatch,
        "OSD 2 đang DOWN. Cụm đang cảnh báo. Cần kiểm tra ngay. Đây là câu thừa.",
    )

    result = asyncio.run(
        telegram_humanizer.humanize_log_for_telegram(
            "OSD 2 DOWN", context="log gốc OSD_DOWN"
        )
    )

    assert result == "OSD 2 DOWN"


def test_humanizer_skips_disabled_router_without_building_client(monkeypatch):
    monkeypatch.setattr(telegram_humanizer.settings, "telegram_ai_humanize_enabled", True)
    monkeypatch.setattr(telegram_humanizer.settings, "router_enabled", False)
    monkeypatch.setattr(telegram_humanizer.settings, "router_api_key", "configured")
    monkeypatch.setattr(telegram_humanizer.settings, "router_base_url", "http://router")
    monkeypatch.setattr(telegram_humanizer.settings, "router_model", "model")

    def unexpected_client(*_args, **_kwargs):
        raise AssertionError("disabled Router must not be called")

    monkeypatch.setattr(telegram_humanizer, "build_router_client", unexpected_client)
    result = asyncio.run(
        telegram_humanizer.humanize_log_for_telegram(
            "OSD 2 DOWN", context="log gốc OSD_DOWN"
        )
    )

    assert result == "OSD 2 DOWN"


def test_humanizer_skips_router_without_model(monkeypatch):
    monkeypatch.setattr(telegram_humanizer.settings, "telegram_ai_humanize_enabled", True)
    monkeypatch.setattr(telegram_humanizer.settings, "router_enabled", True)
    monkeypatch.setattr(telegram_humanizer.settings, "router_api_key", "configured")
    monkeypatch.setattr(telegram_humanizer.settings, "router_base_url", "http://router")
    monkeypatch.setattr(telegram_humanizer.settings, "router_model", "")

    def unexpected_client(*_args, **_kwargs):
        raise AssertionError("Router without a model must not be called")

    monkeypatch.setattr(telegram_humanizer, "build_router_client", unexpected_client)
    result = asyncio.run(
        telegram_humanizer.humanize_log_for_telegram(
            "OSD 2 DOWN", context="log gốc OSD_DOWN"
        )
    )

    assert result == "OSD 2 DOWN"


def _configure_incident(monkeypatch, *, token="123:ABC", chat_id="-100999"):
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_bot_token", token, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_chat_id", chat_id, raising=False)


def _configure_node(monkeypatch, *, token="123:ABC", chat_id="-100999"):
    monkeypatch.setattr(telegram_alerts.settings, "telegram_node_bot_token", token, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_node_chat_id", chat_id, raising=False)


def _configure_rbd_forecast(monkeypatch, *, token="456:RBD", chat_id="-100777"):
    monkeypatch.setattr(telegram_alerts.settings, "telegram_rbd_forecast_bot_token", token, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_rbd_forecast_chat_id", chat_id, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_rbd_forecast_enabled", True, raising=False)


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


def test_osd_down_alert_explains_deactivate_container_and_github_metadata(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )
    excerpt = (
        "--- 10.20.1.195 (osd.2) ---\n"
        "container remove osd-2-deactivate\n"
        "CEPH_GIT_REPO=https://github.com/ceph/ceph.git"
    )

    telegram_alerts.send_incident_alert("OSD_DOWN", "HEALTH_WARN", excerpt)

    assert "OSD 2 trên node 10.20.1.195 đang DOWN" in calls[0]
    assert "đã xoá container tạm phục vụ deactivate OSD" in calls[0]
    assert "không phải lỗi kết nối GitHub" in calls[0]
    assert "🔎 Log gốc:" in calls[0]


def test_unknown_incident_alert_still_has_human_explanation(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert("NEW_CEPH_CHECK", "HEALTH_WARN", "raw detail")

    assert "📝 Diễn giải:" in calls[0]
    assert "🔎 Log gốc:" in calls[0]


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


def test_send_capacity_incident_keeps_pool_osd_node_context(monkeypatch):
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True, raising=False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )
    excerpt = (
        ("raw ceph health detail line " * 80)
        + "\nDung lượng chi tiết:\n"
        + "- Toàn cụm: 81.78% đã dùng\n"
        + "- Pool áp lực: volumes 94.00%\n"
        + "- OSD áp lực: osd.1 trên rnd-khiempx-lab-ceph2 87.26%"
    )

    telegram_alerts.send_incident_alert("POOL_NEARFULL", "HEALTH_WARN", excerpt)

    assert len(calls[0]) < 1000
    assert "Dung lượng chi tiết:" in calls[0]
    assert "Pool áp lực: volumes 94.00%" in calls[0]
    assert "OSD áp lực: osd.1 trên rnd-khiempx-lab-ceph2 87.26%" in calls[0]


def test_send_incident_alert_swallows_send_failure(monkeypatch):
    _configure_incident(monkeypatch)

    def _boom(token, chat_id, text):
        raise TelegramSendError("bad token")

    monkeypatch.setattr(telegram_alerts, "send_telegram_message", _boom)

    telegram_alerts.send_incident_alert("MON_DOWN", "HEALTH_ERR", "mon.a is down")  # must not raise


def test_send_volume_forecast_alert_uses_dedicated_channel_and_auditable_format(monkeypatch):
    _configure_rbd_forecast(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message",
        lambda token, chat_id, text: calls.append((token, chat_id, text)),
    )

    sent = telegram_alerts.send_volume_forecast_alert(
        pool="volumes", image="vm-disk", metric="write_latency_ms",
        horizon_hours=6, current_value=12, predicted_value=24,
        threshold_type="latency_slo_ms", threshold_value=20,
        confidence=.84, training_samples=72, training_window_hours=72,
        model_version="seasonal-trend-v1", target_at=datetime(2026, 8, 25, 4),
        cluster_name="LAB",
    )

    assert sent is True
    token, chat_id, text = calls[0]
    assert (token, chat_id) == ("456:RBD", "-100777")
    assert "Cụm: LAB" in text
    assert "CẢNH BÁO SỚM RBD VOLUME" in text
    assert "volumes/vm-disk" in text
    assert "24.00 ms sau 6 giờ" in text
    assert "Confidence: 84.0%" in text
    assert "seasonal-trend-v1" in text
    assert "không tự chỉnh QoS hoặc resize" in text


def test_volume_forecast_does_not_fall_back_to_incident_channel(monkeypatch):
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_rbd_forecast_bot_token", "", raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_rbd_forecast_chat_id", "", raising=False)
    calls = []
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *args: calls.append(args))

    sent = telegram_alerts.send_volume_forecast_alert(
        pool="volumes", image="disk", metric="iops", horizon_hours=1,
        current_value=100, predicted_value=200, threshold_type="measured_knee_iops",
        threshold_value=180, confidence=.9, training_samples=72,
        training_window_hours=72, model_version="v1", target_at=datetime(2026, 8, 25, 4),
    )

    assert sent is False
    assert calls == []


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


def test_send_capacity_threshold_alert_uses_incident_channel(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args, **kwargs: sent.append((args, kwargs)) or True)

    telegram_alerts.send_capacity_threshold_alert(
        "pool", "volumes", 91.25, 90, 91, 100, cluster_name="cluster-b",
    )

    args, kwargs = sent[0]
    assert args[:3] == (
        telegram_alerts.settings.telegram_incident_bot_token,
        telegram_alerts.settings.telegram_incident_chat_id,
        telegram_alerts.settings.telegram_incident_enabled,
    )
    assert "Pool volumes: 91.25%" in args[3]
    assert args[4] == "cluster-b"


def test_send_capacity_recovery_alert_marks_remaining_level(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args, **kwargs: sent.append(args) or True)

    assert telegram_alerts.send_capacity_recovery_alert(
        "osd", "osd.1", 91.0, 95, 90, cluster_name="cluster-b",
    ) is True
    assert "phục hồi dưới mốc 95%" in sent[0][3]
    assert "vẫn ở mức cảnh báo 90%" in sent[0][3]

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


def test_log_finding_alert_is_short_and_keeps_only_first_recommendation(monkeypatch):
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args: calls.append(args))
    calls = []
    telegram_alerts.send_log_finding_alert(
        "RGW key invalid", "WARNING", "HIGH", "Key is AES256", "Sai định dạng",
        recommended_steps=["Khuyến nghị tốt nhất: gỡ khóa sai", "Bước phụ"],
        operator_commands=["ceph config dump"], daemon_types=["rgw"],
    )
    text = calls[0][3]
    assert "💡 Ưu tiên:" in text
    assert "Khuyến nghị tốt nhất: gỡ khóa sai" in text
    assert "Bước phụ" not in text
    assert "ceph config dump" not in text
    assert "Lệnh kiểm tra" not in text


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
