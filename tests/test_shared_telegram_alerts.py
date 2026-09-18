import asyncio
import time

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
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args, **kwargs: calls.append(args[3]))

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
    assert "🔎 Bằng chứng kỹ thuật:" in calls[0]


def test_incident_alert_humanizes_machine_excerpt(monkeypatch):
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_bot_token", "token")
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_chat_id", "chat")
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True)
    calls = []
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args, **kwargs: calls.append(args[3]))
    seen = []
    monkeypatch.setattr(
        telegram_alerts,
        "_humanize_sync",
        lambda value, **kwargs: (seen.append((value, kwargs)), "OSD 2 đang DOWN trên node 10.20.1.195.")[1],
    )

    telegram_alerts.send_incident_alert(
        "OSD_DOWN",
        "HEALTH_WARN",
        "status: DOWN\nnode: 10.20.1.195",
    )

    assert len(seen) == 1
    assert calls and "📖 Giải thích chi tiết:" in calls[0]


def test_incident_alert_background_mode_queues_delivery(monkeypatch):
    queued = []
    monkeypatch.setattr(
        telegram_alerts._BACKGROUND_ALERT_EXECUTOR,
        "submit",
        lambda callback: queued.append(callback),
    )

    telegram_alerts.send_incident_alert(
        "MON_DOWN", "HEALTH_ERR", "mon.a is down", background=True
    )

    assert len(queued) == 1



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


def test_humanize_telegram_alert_detail_returns_ai_text_and_flag(monkeypatch):
    monkeypatch.setattr(telegram_alerts.settings, "telegram_ai_humanize_enabled", True)
    monkeypatch.setattr(
        telegram_alerts,
        "_humanize_sync",
        lambda value, **kwargs: "RGW trên host rgw-1 không lấy được khóa từ Vault.",
    )

    detail, was_humanized = telegram_alerts.humanize_telegram_alert_detail(
        "ERROR: retrieve actual key from Vault failed on host rgw-1",
        context="lỗi RGW trên host rgw-1",
    )

    assert was_humanized is True
    assert detail == "RGW trên host rgw-1 không lấy được khóa từ Vault."


def test_humanize_telegram_alert_detail_keeps_short_detail_without_ai(monkeypatch):
    def unexpected_humanizer(*_args, **_kwargs):
        raise AssertionError("short deterministic detail must not call the AI")

    monkeypatch.setattr(telegram_alerts, "_humanize_sync", unexpected_humanizer)
    detail, was_humanized = telegram_alerts.humanize_telegram_alert_detail(
        "Vault timeout", context="lỗi RGW"
    )

    assert was_humanized is False
    assert detail == "Vault timeout"


def test_humanize_telegram_alert_detail_has_bounded_wait(monkeypatch):
    def slow_humanizer(*_args, **_kwargs):
        time.sleep(0.2)
        return "OSD 2 đã dừng hoạt động trên node rgw-1."

    monkeypatch.setattr(telegram_alerts, "_humanize_sync", slow_humanizer)
    started = time.monotonic()
    detail, was_humanized = telegram_alerts.humanize_telegram_alert_detail(
        "status: DOWN\n" + ("daemon output " * 40),
        context="RGW error",
        timeout_seconds=0.01,
    )

    assert time.monotonic() - started < 0.15
    assert was_humanized is False
    assert "status: DOWN" in detail


def test_common_ai_pipeline_keeps_concurrent_alert_contexts_separate(monkeypatch):
    """Mỗi message ID phải nhận đúng phần diễn giải của chính nó."""
    monkeypatch.setattr(telegram_alerts.settings, "telegram_ai_humanize_enabled", True)
    queued = []
    sent = []
    edited = []

    def fake_send(_token, _chat_id, text):
        message_id = len(sent) + 100
        sent.append((message_id, text))
        return message_id

    def fake_humanize(value, **_kwargs):
        marker = value.split("marker=", 1)[1].split()[0]
        return f"Cảnh báo marker {marker} thuộc đúng sự kiện này.", True

    monkeypatch.setattr(telegram_alerts, "send_telegram_message", fake_send)
    monkeypatch.setattr(telegram_alerts, "humanize_telegram_alert_detail", fake_humanize)
    monkeypatch.setattr(
        telegram_alerts._BACKGROUND_ALERT_EXECUTOR,
        "submit",
        lambda callback: queued.append(callback),
    )
    monkeypatch.setattr(
        telegram_alerts,
        "edit_telegram_message",
        lambda _token, _chat, message_id, text: edited.append((message_id, text)),
    )

    for marker in ("A", "B", "C", "D"):
        assert telegram_alerts.send_telegram_alert_with_ai(
            "token", "chat", True, f"HEALTH_WARN marker={marker} raw=DOWN",
            context=f"test marker {marker}",
        ) is True

    assert [message_id for message_id, _text in sent] == [100, 101, 102, 103]
    assert len(queued) == 4
    for callback in queued:
        callback()

    assert [message_id for message_id, _text in edited] == [100, 101, 102, 103]
    for message_id, text in edited:
        marker = chr(ord("A") + message_id - 100)
        assert f"marker {marker}" in text
        assert f"marker {chr(ord('A') + (message_id - 100 + 1) % 4)}" not in text.split("Giải thích dễ hiểu:", 1)[1]


def test_humanizer_redacts_secret_before_provider(monkeypatch):
    monkeypatch.setattr(telegram_humanizer.settings, "telegram_ai_humanize_enabled", True)
    monkeypatch.setattr(telegram_humanizer.settings, "codex_chat_enabled", False)
    monkeypatch.setattr(telegram_humanizer.settings, "claude_chat_enabled", False)
    monkeypatch.setattr(telegram_humanizer.settings, "router_enabled", True)
    monkeypatch.setattr(telegram_humanizer.settings, "router_model", "model")
    captured = []

    async def fake_router(source, _context):
        captured.append(source)
        return "OSD 2 đang không hoạt động trên node rgw-1."

    monkeypatch.setattr(telegram_humanizer, "_call_router", fake_router)
    result = asyncio.run(
        telegram_humanizer.humanize_log_for_telegram(
            "status: DOWN\nosd.2 trên node rgw-1 token=supersecretvalue",
            context="RGW error",
        )
    )

    assert result == "OSD 2 đang không hoạt động trên node rgw-1."
    assert captured and "supersecretvalue" not in captured[0]
    assert "<REDACTED>" in captured[0]


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


_PODMAN_EVENT_LOG = (
    "--- 10.20.1.39 (osd.0) ---\n"
    "Sep 15 15:31:21 rnd-khiempx-lab-ceph1.novalocal podman[100895]: "
    "2026-09-15 15:31:21.200235759 +0700 +07 m=+5.670333057 container died "
    "602ce1459defd9401535b4c94c98091d62800a6b2678af17596c352fd50bae52 "
    "(image=quay.io/ceph/ceph@sha256:09ee90f6f3e0c7b9954f71d214ee05e9bbaaaea3716b1dd619603283b829f8b8, "
    "name=ceph-7174a09e-7a72-11f1-b25e-fa163ec4d544-osd-0-deactivate, "
    "GANESHA_REPO_BASEURL=https://buildlogs.centos.org/centos/$releasever-stream/storage/, "
    "org.label-schema.vendor=CentOS, "
    "org.opencontainers.image.authors=Ceph Release Team <ceph-maintainers@ceph.io>, "
    "org.label-schema.license=GPLv2, org.label-schema.schema-version=1.0)"
)


def test_container_image_labels_are_stripped_from_the_evidence_block(monkeypatch):
    """Nhãn build của image chiếm gần hết 700 ký tự cho phép và đẩy phần có
    nghĩa ra ngoài. Bộ lọc này chạy tất định — khi humanizer bị tắt (router
    chưa cấu hình) thì đây là đường duy nhất còn lại."""
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_ai_humanize_enabled", False, raising=False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert("BLUESTORE_SLOW_OP_ALERT", "HEALTH_WARN", _PODMAN_EVENT_LOG)

    evidence = calls[0].split("🔎 Bằng chứng kỹ thuật:\n", 1)[1]
    # Phần nhiễu biến mất...
    for noise in ("org.label-schema", "GANESHA_REPO_BASEURL", "sha256:", "m=+", "ceph-maintainers"):
        assert noise not in evidence
    # ...phần là bằng chứng thật thì còn nguyên.
    assert "10.20.1.39 (osd.0)" in evidence
    assert "container died" in evidence
    assert "name=ceph-7174a09e-7a72-11f1-b25e-fa163ec4d544-osd-0-deactivate" in evidence
    assert "602ce1459def" in evidence  # id rút còn 12 ký tự như podman vẫn hiển thị
    assert "…" not in evidence  # không còn bị cắt cụt vì hết chỗ


def test_noise_filter_leaves_capacity_evidence_untouched(monkeypatch):
    """Khối "Dung lượng chi tiết:" có bố cục nhiều dòng riêng; bộ lọc chỉ
    được nhắm vào log container, không đụng tới nó."""
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )
    excerpt = (
        "Dung lượng chi tiết:\n"
        "- Pool áp lực: volumes 94.00%\n"
        "- OSD áp lực: osd.1 trên rnd-khiempx-lab-ceph2 87.26%"
    )

    telegram_alerts.send_incident_alert("POOL_NEARFULL", "HEALTH_WARN", excerpt)

    assert "Pool áp lực: volumes 94.00%" in calls[0]
    assert "OSD áp lực: osd.1 trên rnd-khiempx-lab-ceph2 87.26%" in calls[0]


def test_every_prose_field_is_reflowed_to_one_paragraph(monkeypatch):
    """`_natural` là chốt duy nhất: một chuỗi do monitor sinh ra, có xuống
    dòng và khoảng trắng thừa, không được lên Telegram ở dạng thô."""
    _configure_node(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_node_enabled", True, raising=False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_node_alert("10.0.0.5", "CPU 95%\n\n   RAM    60%\n")

    # Hai dòng riêng thành hai câu, không dính liền thành "CPU 95% RAM 60%".
    assert "🟠 Node 10.0.0.5: CPU 95%. RAM 60%" in calls[0]


def test_synchronous_watcher_alerts_never_call_the_router(monkeypatch):
    """Các sender này chạy thẳng trong vòng quét của Watcher. Một lần gọi
    humanizer treo tới ~9 giây, nên chốt `_natural` chỉ chuẩn hoá chứ không
    được đụng tới router ở những đường này."""
    _configure_node(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_node_enabled", True, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_ai_humanize_enabled", True, raising=False)
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *_args: None)

    def unexpected_humanizer(*_args, **_kwargs):
        raise AssertionError("đường quét đồng bộ không được gọi humanizer")

    monkeypatch.setattr(telegram_alerts, "_humanize_sync", unexpected_humanizer)

    # Chuỗi kiểu máy (`degraded=0`) — đủ để `_needs_humanization` kêu True.
    telegram_alerts.send_node_alert("10.0.0.5", "status: DOWN, degraded=0")
    telegram_alerts.send_osd_latency_alert(2, "node1", "latency: 512 ms")
    telegram_alerts.send_crush_skew_alert("USE", "osd.3", "skew=1.8")
    telegram_alerts.send_database_size_alert("size=95%")
    telegram_alerts.send_vitastor_alert("vita", "WARNING", "Data integrity: degraded=0 bytes")


def test_incident_headline_names_the_problem_and_keeps_the_code(monkeypatch):
    """Tiêu đề cũ là mã thô ("Cụm Ceph: PG_NOT_DEEP_SCRUBBED"). Chỉ 8 mã có
    câu Diễn giải riêng, phần còn lại rơi vào một câu chung chung — nên với
    đa số cảnh báo thật, người trực không đọc được chuyện gì đang xảy ra."""
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert(
        "PG_NOT_DEEP_SCRUBBED", "HEALTH_WARN", "12 pgs not deep-scrubbed in time"
    )

    assert "🟡 HEALTH_WARN · Có Placement Group quá hạn deep-scrub (PG_NOT_DEEP_SCRUBBED)" in calls[0]


def test_unknown_ceph_code_headline_stays_readable(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert("WEIRD_NEW_CHECK", "HEALTH_WARN", "something odd")

    assert "🟡 HEALTH_WARN · Cảnh báo Ceph: WEIRD_NEW_CHECK" in calls[0]


def test_periodic_health_status_names_each_open_check(monkeypatch):
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True, raising=False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_periodic_health_status("HEALTH_OK", [])
    telegram_alerts.send_periodic_health_status("HEALTH_WARN", ["OSD_DOWN", "POOL_NEARFULL"])

    assert "✅ Không có cảnh báo nào đang mở." in calls[0]
    # Mỗi cảnh báo một dòng — gộp một dòng thì trên điện thoại chỉ là khối chữ.
    assert "⚠️ Đang mở 2 cảnh báo:\n" in calls[1]
    assert "\n• Một OSD đã ngừng hoạt động (OSD_DOWN)" in calls[1]
    assert "\n• Pool sắp đầy (POOL_NEARFULL)" in calls[1]


def test_auth_health_checks_have_vietnamese_titles():
    for code in (
        "AUTH_INSECURE_CLIENT_KEY_TYPE",
        "AUTH_INSECURE_KEYS_ALLOWED",
        "AUTH_INSECURE_GLOBAL_ID_RECLAIM",
    ):
        assert not telegram_alerts._incident_title(code).startswith("Cảnh báo Ceph:")


def test_periodic_health_status_caps_a_long_check_list(monkeypatch):
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True, raising=False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_periodic_health_status(
        "HEALTH_WARN",
        ["OSD_DOWN", "PG_DEGRADED", "POOL_NEARFULL", "RECENT_CRASH", "SLOW_OPS", "TOO_MANY_PGS"],
    )

    assert "⚠️ Đang mở 6 cảnh báo:\n" in calls[0]
    assert calls[0].rstrip().endswith("• và 2 cảnh báo khác")
    assert calls[0].count("\n• ") == 5  # 4 mục + dòng "còn lại"


def _capture_external_alerts(monkeypatch):
    """Bắt payload gửi ra webhook/Slack/email của shared.notification_channels."""
    sent = []
    monkeypatch.setattr(
        telegram_alerts, "enqueue_external_alert", lambda **kwargs: sent.append(kwargs) or True
    )
    monkeypatch.setattr(telegram_alerts, "send_telegram_message", lambda *_args: None)
    return sent


def test_external_alert_category_does_not_depend_on_message_wording(monkeypatch):
    """Trước đây category được đoán bằng cách dò chuỗi trong tin nhắn đã
    dựng: một cảnh báo cụm có chữ "log daemon" bị xếp nhầm vào
    log-intelligence, có chữ "node " thì thành hardware. Nó vừa sai, vừa
    khoá cứng cách phân loại vào câu chữ — sửa lời văn cho tự nhiên hơn là
    alert lặng lẽ đổi kênh."""
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True, raising=False)
    sent = _capture_external_alerts(monkeypatch)

    telegram_alerts.send_ai_unavailable_alert("OSD_DOWN", "HEALTH_ERR")

    assert sent[0]["category"] == "incident"
    assert sent[0]["severity"] == "critical"


def test_external_alert_category_is_per_sender(monkeypatch):
    _configure_incident(monkeypatch)
    _configure_node(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_incident_enabled", True, raising=False)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_node_enabled", True, raising=False)
    sent = _capture_external_alerts(monkeypatch)

    telegram_alerts.send_node_alert("10.0.0.5", "CPU 95%")
    telegram_alerts.send_capacity_threshold_alert("pool", "volumes", 96.0, 95, 96, 100)
    telegram_alerts.send_capacity_recovery_alert("pool", "volumes", 70.0, 90, 0)
    telegram_alerts.send_log_finding_alert("RGW key invalid", "CRITICAL", "HIGH", "x", "y")

    assert [(item["category"], item["severity"]) for item in sent] == [
        ("hardware", "warning"),
        ("capacity", "critical"),
        ("capacity", "info"),
        ("log-intelligence", "critical"),
    ]


def test_ai_prose_is_cut_at_a_sentence_boundary_not_mid_word(monkeypatch):
    """Humanizer trả về tối đa 3 câu; cắt cứng theo số ký tự là dựng lại
    đúng câu cụt mà humanizer vừa loại bỏ."""
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )
    diagnosis = (
        "Ceph ghi nhận osd.7 trên node ceph-node03 đã dừng hoạt động từ khoảng 08:12 "
        "sáng nay và không còn phản hồi heartbeat với các OSD còn lại trong cụm. "
        "Toàn cụm đang ở trạng thái HEALTH_WARN với khoảng 2,17% dữ liệu thiếu bản "
        "sao và 12 nhóm dữ liệu đang suy giảm. Đây là thông tin quan sát được từ log, "
        "chưa đủ căn cứ để khẳng định nguyên nhân gốc là hỏng ổ đĩa."
    )
    assert len(diagnosis) > telegram_alerts._MAX_FOLLOWUP_FIELD_CHARS

    telegram_alerts.send_ai_incident_alert("OSD_DOWN", "HEALTH_WARN", diagnosis, "Kiểm tra daemon.")

    line = next(line for line in calls[0].splitlines() if line.startswith("🧠 Ý kiến AI:"))
    assert len(line) < len(diagnosis)  # vẫn bị rút gọn
    assert line.endswith(".") and "…" not in line


def test_short_ai_prose_is_not_truncated(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_ai_incident_alert(
        "OSD_DOWN", "HEALTH_WARN", "osd.7 đã dừng hoạt động.", "Kiểm tra daemon."
    )

    assert "🧠 Ý kiến AI: osd.7 đã dừng hoạt động." in calls[0]


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
    assert "🔎 Bằng chứng kỹ thuật:" in calls[0]


def test_bluesstore_slow_ops_explains_missing_osd_heartbeat(monkeypatch):
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_ai_humanize_enabled", False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )
    excerpt = (
        "--- 10.20.1.195 (osd.2) ---\n"
        "osd.2 heartbeat_check: no reply from 10.20.1.153:6806 osd.1"
    )

    telegram_alerts.send_incident_alert("BLUESTORE_SLOW_OP_ALERT", "HEALTH_WARN", excerpt)

    assert "OSD 2 trên node 10.20.1.195 không nhận được phản hồi heartbeat" in calls[0]
    assert "OSD 1 tại 10.20.1.153:6806" in calls[0]
    assert "chưa đủ để kết luận ổ đĩa đã hỏng" in calls[0]
    assert "🔎 Bằng chứng kỹ thuật:" in calls[0]


def test_large_omap_placeholder_is_explained_without_empty_key_values(monkeypatch):
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_ai_humanize_enabled", True)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )
    monkeypatch.setattr(
        telegram_alerts,
        "_humanize_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("placeholder must not call AI")),
    )

    telegram_alerts.send_incident_alert(
        "LARGE_OMAP_OBJECTS",
        "HEALTH_WARN",
        "LARGE_OMAP_EVIDENCE bucket= object= keys= threshold= shards= pg=",
    )

    assert "object chỉ mục OMAP quá lớn" in calls[0]
    assert "Chưa đủ bằng chứng để xác định object nào" in calls[0]
    assert "bucket= object= keys=" not in calls[0]
    assert "Chưa thu thập được chi tiết bucket/object" in calls[0]


def test_large_omap_evidence_is_summarized_with_populated_fields(monkeypatch):
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_ai_humanize_enabled", False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert(
        "LARGE_OMAP_OBJECTS",
        "HEALTH_WARN",
        "LARGE_OMAP_EVIDENCE bucket=test-bucket object=.dir.uuid keys=10922 threshold=2000 shards=32 pg=1.2",
    )

    assert "bucket test-bucket" in calls[0]
    assert "object .dir.uuid" in calls[0]
    assert "10922 key" in calls[0]
    assert "PG 1.2" in calls[0]


def test_unknown_incident_alert_still_has_human_explanation(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert("NEW_CEPH_CHECK", "HEALTH_WARN", "raw detail")

    assert "📝 Diễn giải:" in calls[0]
    assert "🔎 Bằng chứng kỹ thuật:" in calls[0]


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
    assert "Độ tin cậy: 84.0%" in text
    assert "📊 Chỉ số: Độ trễ ghi" in text
    assert "🔬 Mô hình dự báo: seasonal-trend-v1" in text
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
    assert "🎯 Đối tượng: 10.3.53.1" in calls[0]
    assert "đang xác minh Ceph" in calls[0]


def test_auto_remediation_alert_renders_node_list_without_json_syntax(monkeypatch):
    # `Action.target_nodes` được lưu bằng json.dumps(list); in thẳng lên
    # Telegram thì người trực nhận được `["10.3.53.1", "10.3.53.2"]`.
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_auto_remediation_alert(
        "OSD_DOWN", None, None, None, True,
        target_nodes='["10.3.53.1", "10.3.53.2"]',
    )

    assert "🎯 Đối tượng: 10.3.53.1, 10.3.53.2" in calls[0]
    assert "[" not in calls[0] and '"' not in calls[0]


def test_auto_remediation_alert_keeps_unparsable_target_nodes_readable(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_auto_remediation_alert(
        "OSD_DOWN", None, None, None, True, target_nodes="ceph-node03",
    )

    assert "🎯 Đối tượng: ceph-node03" in calls[0]


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
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args, **kwargs: calls.append(args))
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
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args, **kwargs: calls.append(args))
    calls = []
    telegram_alerts.send_log_finding_recovery_pending_alert(
        "RGW key invalid", "still invalid", ("ceph_health=HEALTH_OK",),
        verification_code="RGW_DEFAULT_ENCRYPTION_KEY_INVALID",
    )
    text = calls[0][3]
    assert "Gợi ý tốt nhất" in text
    assert "Vault SSE-S3" in text
    assert "Phương án khác" in text


_MON_ROCKSDB_LOG = """--- 10.3.53.1 (mon.rnd-khiempx-lab-ceph1) ---
Cumulative WAL: 0 writes, 0 syncs, 0.00 writes per sync, written: 0.00 GB, 0.00 MB/s
Cumulative stall: 00:00:0.000 H:M:S, 0.0 percent
Interval writes: 0 writes, 0 keys, 0 commit groups, ingest: 0.00 MB, 0.00 MB/s
Interval WAL: 0 writes, 0 syncs, 0.00 writes per sync, written: 0.00 GB
Interval stall: 00:00:0.000 H:M:S, 0.0 percent
** Compaction Stats [default] **
Level Files Size Score Read(GB) Rn(GB) Rnp1(GB) Write(GB) Wnew(GB) W-Amp
------------------------------------------------------------------------
mon.rnd-khiempx-lab-ceph1 cho phép client dùng khoá xác thực không an toàn"""


def test_rocksdb_statistics_are_stripped_from_mon_evidence(monkeypatch):
    """Log MON định kỳ nhả nguyên bảng thống kê RocksDB. Không dòng nào nói
    gì về health check đang cảnh báo, nhưng chúng dài hàng nghìn ký tự và
    đẩy dòng bằng chứng thật ra ngoài giới hạn 700 ký tự."""
    _configure_incident(monkeypatch)
    monkeypatch.setattr(telegram_alerts.settings, "telegram_ai_humanize_enabled", False, raising=False)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert("AUTH_INSECURE_KEYS_ALLOWED", "HEALTH_WARN", _MON_ROCKSDB_LOG)

    for noise in ("Compaction Stats", "Cumulative WAL", "Interval stall", "W-Amp", "-----"):
        assert noise not in calls[0]
    assert "cho phép client dùng khoá xác thực không an toàn" in calls[0]


def test_generic_explanation_is_dropped_when_the_title_already_says_it(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert("AUTH_INSECURE_KEYS_ALLOWED", "HEALTH_WARN", "chi tiết")
    telegram_alerts.send_incident_alert("NEW_CEPH_CHECK", "HEALTH_WARN", "chi tiết")

    assert "📝 Diễn giải:" not in calls[0]  # tiêu đề đã nói rõ
    assert "📝 Diễn giải:" in calls[1]      # mã lạ thì vẫn cần một câu


def test_log_anomaly_is_not_described_as_a_ceph_health_check(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert("LOG_ANOMALY:81b4f2d560ac", "", "rgw keystore error x3")

    assert "Bất thường phát hiện từ log (LOG_ANOMALY:81b4f2d560ac)" in calls[0]
    assert "không phải một health check của Ceph" in calls[0]


def test_multiline_rationale_does_not_run_two_sentences_together(monkeypatch):
    _configure_incident(monkeypatch)
    calls = []
    monkeypatch.setattr(
        telegram_alerts, "send_telegram_message", lambda token, chat_id, text: calls.append(text)
    )

    telegram_alerts.send_incident_alert(
        "LOG_ANOMALY:81b4f2d5", "", "chi tiết", reminder=True,
        rationale="RGW không truy xuất được khóa mã hóa mặc định\nPhát hiện ba lỗi liên tiếp.",
    )

    assert "khóa mã hóa mặc định. Phát hiện ba lỗi liên tiếp." in calls[0]


def test_every_rocksdb_table_variant_is_recognised_as_noise():
    """Bảng thống kê RocksDB có nhiều biến thể — Level/Priority ở header,
    L0/Sum/Int/User ở hàng số liệu, cộng dòng Blob riêng — nên kể tên từng
    tiền tố là đuổi không xuể. Hai luật bền hơn: từ khoá chỉ RocksDB mới có,
    và mật độ token thuần số của một hàng số liệu."""
    noise = [
        "Priority Files Size Score Read(GB) Rn(GB) Rnp1(GB) Write(GB) W-Amp KeyIn KeyDrop",
        "User 0/0 0.00 KB 0.0 0.0 0.0 0.0 0.0 0.0 0.0 0.0 24.2 0.03 1 0.032 0 0 0.0",
        "Blob file count: 0, total size: 0.0 GB, garbage size: 0.0 GB, space amp: 0.0",
        "L0      2/0   1.00 KB   0.5      0.0     0.0      0.0       0.1      0.1",
        "** Compaction Stats [default] **",
        "Cumulative WAL: 0 writes, 0 syncs, 0.00 writes per sync, written: 0.00 GB",
    ]
    for line in noise:
        assert telegram_alerts._is_stats_noise(line), line


def test_real_log_lines_are_not_mistaken_for_statistics():
    """Luật mật độ số không được ăn nhầm bằng chứng thật."""
    kept = [
        "Sep 04 15:49:46 rnd-khiempx-lab-ceph1 ceph-mon[1782072]: starting mon.rnd rank 0 "
        "at public addrs [v2:10.20.1.39:3300/0]",
        "- OSD áp lực: osd.1 trên rnd-khiempx-lab-ceph2 87.26%",
        "osd.7 down since 2026-09-15 08:12:03, last heartbeat no reply from 10.10.20.13:6802 osd.9",
        "--- 10.3.53.1 (mon.rnd-khiempx-lab-ceph1) ---",
    ]
    for line in kept:
        assert not telegram_alerts._is_stats_noise(line), line
