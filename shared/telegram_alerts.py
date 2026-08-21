"""Category-scoped Telegram senders for Watcher-detected alerts (cluster
health/Incident errors, node hardware) — 2026-08-06: each category is now
its own fully independent Telegram channel with its OWN Bot Token/Chat ID
(previously all 3 categories shared one pair, switched on/off by a
separate per-category toggle). 2026-08-07: that per-category toggle is
back (see config/settings.py's `telegram_*_enabled` fields) — a channel is
only active when it's BOTH "configured" (token AND chat id non-blank) AND
`_enabled`, checked together in `_send` below.

`send_osd_latency_alert` (2026-08-07, watcher/osd_latency_monitor.py)
deliberately reuses the SAME "Phần cứng" channel as `send_node_alert`
rather than getting its own 4th Bot Token/Chat ID pair — an OSD/disk
running abnormally slow is the same category of problem (physical
resource degradation) as a node's CPU/RAM being pegged, and the 3-channel
design is meant to stay exactly 3, not grow a new pair per alert type.

Kept in shared/ (not watcher/) so it stays importable from either process
without crossing any layering boundary, same posture as
shared/router_client.py/shared/telegram_client.py. Deliberately SEPARATE
from worker/backup/alerting.py::send_alert — that module owns its own
independent Backup channel config + webhook delivery, untouched by this
module.

Best-effort like every other alert sender in this codebase: a
TelegramSendError here is logged and swallowed, never raised to the
caller — sending a notification must never fail whatever real check/scan
triggered it.
"""

from __future__ import annotations

import logging

from config.settings import settings
from shared.telegram_client import TelegramSendError, send_telegram_message

logger = logging.getLogger(__name__)

# Truncation length for a ceph_code's log_excerpt — some (e.g. a PG dump or
# a slow-ops list) can run to several KB, well past what's useful to read
# in a phone notification, and needlessly close to Telegram's own 4096-char
# message limit once the rest of the text is added.
_MAX_EXCERPT_CHARS = 320
_MAX_FOLLOWUP_FIELD_CHARS = 240

_INCIDENT_SEVERITY_PREFIX = {
    "HEALTH_ERR": "\U0001f534 HEALTH_ERR",  # red circle
    "HEALTH_WARN": "\U0001f7e1 HEALTH_WARN",  # yellow circle
}


def _with_cluster_prefix(text: str, cluster_name: str | None = None) -> str:
    """Prepends a cluster name as the first line of every message this
    module sends — lets an operator running several ceph-aiops instances
    (or, since 2026-08-07's multi-cluster observability Phase 1, several
    OBSERVED clusters from one instance) into the same Telegram chat tell
    which cluster an alert is from.

    `cluster_name=None` (every caller except send_incident_alert's observed-
    cluster case) falls back to `settings.cluster_name` — unchanged
    behavior for the default cluster. No-op (text unchanged) when the
    resolved name is blank, so a single-cluster deployment's messages stay
    byte-identical to before this existed."""
    name = (cluster_name if cluster_name is not None else settings.cluster_name).strip()
    if not name:
        return text
    return f"\U0001f4cd Cụm: {name}\n{text}"


def _compact(value: str | None, limit: int) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _send(bot_token: str, chat_id: str, enabled: bool, text: str, cluster_name: str | None = None) -> None:
    """`enabled` (2026-08-07, Alert Telegram page) is a SEPARATE on/off
    switch from "configured" (bot_token/chat_id both non-blank) — lets an
    operator pause a channel with one click without losing/retyping its
    Chat ID, unlike the earlier "blank the chat id to pause" design."""
    if not enabled or not bot_token or not chat_id:
        return
    try:
        send_telegram_message(bot_token, chat_id, _with_cluster_prefix(text, cluster_name))
    except TelegramSendError:
        logger.exception("shared.telegram_alerts: Telegram delivery failed")


def send_incident_alert(
    ceph_code: str,
    severity: str | None,
    log_excerpt: str | None,
    cluster_name: str | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
    reminder: bool = False,
    diagnosis_text: str | None = None,
    rationale: str | None = None,
) -> None:
    """Called once per newly-created cluster-health Incident
    (watcher/main.py::build_and_publish_incident, one call per `ceph
    health detail` check) — a genuine cluster problem, NOT a Volume-
    saturation/DeviceHealth-prediction Incident (those are their own
    ceph_code families with their own create/resolve lifecycle and are
    deliberately out of scope for this function; a raw `ceph health
    detail` check code is always what reaches this function via the one
    call site above).

    No-op if the Lỗi cụm channel's bot token/chat id aren't configured yet
    — checked here (not left to send_telegram_message's own "missing
    config" error) so an operator who simply hasn't set up this channel
    never sees a log entry about it "failing".

    `bot_token`/`chat_id`/`enabled` (2026-08-10, multi-tenant remediation
    Phase 2): override the 3 GLOBAL "Lỗi cụm" channel fields when given —
    `watcher/main.py::_build_and_publish_incident_for_observed_cluster`
    passes an OBSERVED cluster's own `Cluster.telegram_*` fields here when
    that cluster has configured its own channel, narrowing delivery to
    just that chat instead of the 3 global ones. `None` (every other
    caller, unchanged) means "use the global settings.telegram_incident_*
    exactly as before this param existed"."""
    prefix = _INCIDENT_SEVERITY_PREFIX.get(severity or "", f"⚠️ {severity or 'SỰ CỐ'}")
    excerpt = _compact(log_excerpt, _MAX_EXCERPT_CHARS)
    reminder_prefix = "🔁 NHẮC LẠI · " if reminder else ""
    text = f"{reminder_prefix}{prefix} Cụm Ceph: {ceph_code}"
    if excerpt:
        text += f"\n{excerpt}"
    if reminder and diagnosis_text:
        text += f"\n🧠 Tóm tắt AI: {_compact(diagnosis_text, _MAX_FOLLOWUP_FIELD_CHARS)}"
    if reminder and rationale:
        text += f"\n🔧 Giải pháp: {_compact(rationale, _MAX_FOLLOWUP_FIELD_CHARS)}"
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
    )


def send_vitastor_alert(cluster_name: str, health: str, detail: str) -> None:
    """Send a Vitastor health transition through the cluster-alert channel."""
    prefix = {
        "CRITICAL": "🔴 CRITICAL",
        "WARNING": "🟡 WARNING",
        "UNREACHABLE": "🔴 UNREACHABLE",
        "HEALTHY": "🟢 RECOVERED",
    }.get(health, f"⚠️ {health}")
    _send(
        settings.telegram_incident_bot_token,
        settings.telegram_incident_chat_id,
        settings.telegram_incident_enabled,
        f"{prefix} Cụm Vitastor\n{_compact(detail, _MAX_EXCERPT_CHARS)}",
        cluster_name,
    )


def send_ai_incident_alert(
    ceph_code: str,
    severity: str | None,
    diagnosis_text: str,
    rationale: str,
    *,
    cluster_name: str | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Send the primary cluster alert after AI diagnosis is available."""
    prefix = _INCIDENT_SEVERITY_PREFIX.get(severity or "", f"⚠️ {severity or 'SỰ CỐ'}")
    text = "\n".join(
        (
            f"{prefix} Cụm Ceph: {ceph_code}",
            f"🧠 Ý kiến AI: {_compact(diagnosis_text, _MAX_FOLLOWUP_FIELD_CHARS)}",
            f"🔧 Đề xuất: {_compact(rationale, _MAX_FOLLOWUP_FIELD_CHARS)}",
        )
    )
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
    )


def send_ai_unavailable_alert(
    ceph_code: str,
    severity: str | None,
    *,
    cluster_name: str | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Make exhausted AI diagnosis failures visible to the operator."""
    prefix = _INCIDENT_SEVERITY_PREFIX.get(severity or "", f"⚠️ {severity or 'SỰ CỐ'}")
    text = "\n".join(
        (
            f"{prefix} Cụm Ceph: {ceph_code}",
            "🧠 Ý kiến AI: Không thể phân tích sau nhiều lần thử.",
            "🔧 Đề xuất tạm thời: kiểm tra `ceph health detail` và log daemon liên quan; không tự động thay đổi cụm khi chưa xác định nguyên nhân.",
        )
    )
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
    )
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
    )


def send_node_alert(host: str, message: str) -> None:
    """Called once per NEWLY-flagged node resource problem
    (watcher/node_health_monitor.py::create_or_resolve_node_health_incidents
    — only when a new Incident is created, not resent on every scan a host
    stays flagged). No-op if the Phần cứng channel's bot token/chat id
    aren't configured yet (same reasoning as send_incident_alert above)."""
    _send(settings.telegram_node_bot_token, settings.telegram_node_chat_id, settings.telegram_node_enabled, f"\U0001f7e0 Node {host}: {_compact(message, _MAX_EXCERPT_CHARS)}")


def send_trash_capacity_alert(trash_bytes: int, total_bytes: int, ratio: float, entry_count: int) -> None:
    """Send RBD Trash capacity warnings through the cluster Alert channel."""
    gib = 1024 ** 3
    text = "\n".join(
        (
            "🟡 HEALTH_WARN RBD Trash vượt ngưỡng 20% dung lượng cụm",
            f"🗑 Trash: {trash_bytes / gib:.2f} GiB / {total_bytes / gib:.2f} GiB ({ratio * 100:.1f}%), {entry_count} volume.",
            "🔧 Đề xuất: kiểm tra các volume trong mục Trash và duyệt xoá vĩnh viễn những volume không còn cần khôi phục.",
        )
    )
    _send(
        settings.telegram_incident_bot_token,
        settings.telegram_incident_chat_id,
        settings.telegram_incident_enabled,
        text,
    )


def send_osd_latency_alert(osd_id: int, host: str | None, message: str) -> None:
    """Called once per NEWLY-flagged OSD latency outlier
    (watcher/osd_latency_monitor.py::create_or_resolve_osd_latency_incidents
    — only when a new Incident is created, same "one notification per
    genuinely new problem" posture as send_node_alert above). Shares the
    Phần cứng channel with send_node_alert — see this module's own
    docstring for why there's no separate 4th channel for this."""
    label = f"osd.{osd_id}" + (f" ({host})" if host else "")
    _send(settings.telegram_node_bot_token, settings.telegram_node_chat_id, settings.telegram_node_enabled, f"\U0001f7e0 OSD chậm: {label}\n{_compact(message, _MAX_EXCERPT_CHARS)}")


def send_crush_skew_alert(signal: str, entity_label: str, message: str) -> None:
    """Called once per NEWLY-flagged CRUSH data-distribution Skew
    (watcher/crush_skew_monitor.py::create_or_resolve_crush_skew_incidents —
    only when a new Incident is created, same "one notification per
    genuinely new problem" posture as send_osd_latency_alert above). Shares
    the Phần cứng channel with send_node_alert/send_osd_latency_alert (AD-31
    — the 2 Skew signals, USE and PG, are deliberately NOT a 4th channel).

    `entity_label` is a pre-formatted string (e.g. "osd.3" or "host node1")
    rather than separate osd_id/host parameters — the flagged entity can be
    either an OSD or a Host, and only one of those two ever has a
    meaningful osd_id, so a single already-formatted label avoids an
    always-None parameter on half of all calls."""
    label = f"{entity_label} ({signal})"
    _send(settings.telegram_node_bot_token, settings.telegram_node_chat_id, settings.telegram_node_enabled, f"\U0001f7e0 Lệch CRUSH: {label}\n{_compact(message, _MAX_EXCERPT_CHARS)}")


def send_database_size_alert(message: str) -> None:
    """Called once per NEWLY-flagged database-size Incident
    (watcher/database_capacity_monitor.py::create_or_resolve_database_size_incident
    — only when a new Incident is created, same "one notification per
    genuinely new problem" posture as send_node_alert/send_osd_latency_alert/
    send_crush_skew_alert above). Shares the Phần cứng channel with those 3
    — this is about a resource running out (the app's own DB storage), not
    a Ceph-cluster check, same reasoning AD-31 already established for not
    opening a 4th channel. No entity/label parameter -- there is only ever
    one database, unlike the per-osd/per-host/per-CRUSH-entity alerts above."""
    _send(settings.telegram_node_bot_token, settings.telegram_node_chat_id, settings.telegram_node_enabled, f"\U0001f7e0 Database ceph-aiops gần đầy\n{_compact(message, _MAX_EXCERPT_CHARS)}")


def send_auto_remediation_alert(
    ceph_code: str,
    diagnosis_text: str | None,
    rationale: str | None,
    command: str | None,
    succeeded: bool,
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Called once per SAFE Action after execution finishes
    (worker/llm/router_client.py::_record_execution_result) — the
    send_incident_alert() call for the same Incident fires the moment it's
    CREATED, before the router has diagnosed anything, so it can only ever
    carry the raw ceph_code + log excerpt. This is the follow-up message
    that actually reports what the AI concluded and did about it, on the
    same Lỗi cụm channel (reusing telegram_incident_bot_token/chat_id
    rather than adding a 4th channel — same reasoning send_osd_latency_alert
    gives for reusing Phần cứng). No-op if that channel isn't configured,
    same as every other function in this module.

    `bot_token`/`chat_id`/`enabled` (2026-08-10, multi-tenant remediation
    Phase 2): same override posture as send_incident_alert() — `worker/
    llm/router_client.py::_record_execution_result` passes the SAFE
    Action's own Incident's cluster's channel here when that cluster has
    configured one of its own; `None` (default) keeps reading the 3
    global settings.telegram_incident_* fields exactly as before."""
    prefix = "⏳ Đã chạy tự động, đang xác minh" if succeeded else "\u274c Tự động xử lý thất bại"
    lines = [f"{prefix}: {ceph_code}"]
    if diagnosis_text:
        lines.append(f"⚠️ Chẩn đoán: {_compact(diagnosis_text, _MAX_FOLLOWUP_FIELD_CHARS)}")
    if rationale:
        lines.append(f"🔧 Giải pháp: {_compact(rationale, _MAX_FOLLOWUP_FIELD_CHARS)}")
    if command:
        lines.append(f"💻 Lệnh: {_compact(command, _MAX_FOLLOWUP_FIELD_CHARS)}")
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        "\n".join(lines),
    )


def send_update_failure_alert(
    ceph_code: str,
    diagnosis_text: str | None,
    failure_summary: str,
    rollback_summary: str,
    *,
    cluster_name: str | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Report a failed cluster update and its safety rollback in Vietnamese."""
    text = "\n".join((
        f"🔴 CẬP NHẬT THẤT BẠI: {ceph_code}",
        f"🧠 Tóm tắt AI: {_compact(diagnosis_text or failure_summary, _MAX_FOLLOWUP_FIELD_CHARS)}",
        f"❌ Lỗi cụ thể: {_compact(failure_summary, _MAX_FOLLOWUP_FIELD_CHARS)}",
        f"↩️ Rollback: {_compact(rollback_summary, _MAX_FOLLOWUP_FIELD_CHARS)}",
        "🔧 Giải pháp: Sửa lỗi trên node được nêu, kiểm tra `ceph health detail`, sau đó chạy lại cập nhật từ giao diện.",
    ))
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
    )


# --- Log Intelligence L3 (Plan/log-intelligence-rca-plan.md) --------------
#
# Dùng chung kênh "Cụm Ceph" (telegram_incident_*) với send_ai_incident_alert
# / send_trash_capacity_alert — KHÔNG mở kênh thứ 4. Đây là chẩn đoán AI về
# sức khoẻ cụm, đúng loại nội dung kênh đó đang mang; và thiết kế 3 kênh
# được giữ nguyên là 3 (xem docstring đầu module + AD-31).

_LOG_FINDING_SEVERITY_PREFIX = {
    "CRITICAL": "\U0001f534 NGHIÊM TRỌNG",  # red circle
    "WARNING": "\U0001f7e1 CẢNH BÁO",       # yellow circle
    "INFO": "\U0001f535 THÔNG TIN",         # blue circle
}


def send_log_finding_alert(
    title: str,
    severity: str,
    confidence: str,
    summary: str | None,
    root_cause: str | None,
    evidence_templates: list[str] | None = None,
    recommended_action_id: str | None = None,
    validation_notes: str | None = None,
    *,
    operator_commands: list[str] | None = None,
    cluster_name: str | None = None,
) -> None:
    """Gửi MỘT lần cho mỗi phát hiện log THỰC SỰ MỚI
    (`watcher/log_analysis.py` chỉ gọi khi `dedupe_key` chưa có bản ghi nào
    đang OPEN) — cùng nếp "một thông báo cho một vấn đề thật sự mới" mà
    send_node_alert/send_osd_latency_alert/send_crush_skew_alert đã theo.

    Luôn kèm EVIDENCE GỐC (mẫu log thật) chứ không chỉ kết luận của AI:
    người trực phải tự đánh giá được, không phải tin lời model. Cùng lý do
    `validation_notes` cũng được đưa vào — nếu server đã phải sửa/hạ cấp câu
    trả lời của model thì người đọc cần biết ngay trên điện thoại, chứ không
    phải mở Dashboard mới thấy."""
    prefix = _LOG_FINDING_SEVERITY_PREFIX.get(severity, f"⚠️ {severity}")
    lines = [
        f"{prefix} Phát hiện từ log: {_compact(title, _MAX_FOLLOWUP_FIELD_CHARS)}",
        f"🎯 Độ tin cậy: {confidence}",
    ]
    if summary:
        lines.append(f"🧠 Tóm tắt: {_compact(summary, _MAX_FOLLOWUP_FIELD_CHARS)}")
    if root_cause:
        lines.append(f"🔎 Nguyên nhân nghi ngờ: {_compact(root_cause, _MAX_FOLLOWUP_FIELD_CHARS)}")
    for template in (evidence_templates or [])[:3]:
        lines.append(f"📄 Log: {_compact(template, _MAX_EXCERPT_CHARS)}")
    if recommended_action_id:
        lines.append(f"🔧 Đề xuất: {recommended_action_id} (cần Duyệt thủ công)")
    if operator_commands:
        lines.append("🖥 Lệnh kiểm tra (chỉ đọc):")
        lines.extend(f"`{_compact(command, _MAX_EXCERPT_CHARS)}`" for command in operator_commands)
    if validation_notes:
        lines.append(f"⚠️ Hệ thống đã chỉnh câu trả lời của AI: {_compact(validation_notes, _MAX_FOLLOWUP_FIELD_CHARS)}")
    _send(
        settings.telegram_incident_bot_token,
        settings.telegram_incident_chat_id,
        settings.telegram_incident_enabled,
        "\n".join(lines),
        cluster_name,
    )


def send_log_finding_resolved_alert(title: str, *, cluster_name: str | None = None) -> None:
    """Gửi khi các mẫu log của một phát hiện đã ngừng xuất hiện — đóng vòng
    đời OPEN -> RESOLVED, để người trực biết vấn đề đã hết mà không phải tự
    vào Dashboard kiểm tra."""
    _send(
        settings.telegram_incident_bot_token,
        settings.telegram_incident_chat_id,
        settings.telegram_incident_enabled,
        f"\U0001f7e2 Đã hết: {_compact(title, _MAX_FOLLOWUP_FIELD_CHARS)}\n"
        f"Các mẫu log liên quan không còn xuất hiện trong các lần quét gần đây.",
        cluster_name,
    )


def send_incident_verified_alert(
    ceph_code: str,
    attempted_command: str | None = None,
    cluster_name: str | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
) -> None:
    """"✅ ĐÃ KHẮC PHỤC" — gửi khi watcher/verify.py đã HỎI LẠI CỤM và xác
    nhận ceph_code không còn trong `ceph health detail` nữa.

    2026-08-20 — trước đây không có thông báo nào cho việc này. Operator
    nhận được đề xuất, bấm Duyệt, rồi im lặng: muốn biết lệnh có ăn thua
    không thì phải tự mở Dashboard hoặc tự SSH vào cụm mà xem. Tệ hơn,
    Incident được đánh RESOLVED chỉ vì lệnh SSH trả về exit 0, nên kể cả
    Dashboard cũng đang nói "đã xong" cho những ca chưa xong.

    Đi qua ĐÚNG kênh "Lỗi cụm" mà `send_incident_alert` đã dùng để báo sự
    cố này lúc đầu — đóng lại đúng chỗ đã mở ra, để một cuộc hội thoại nằm
    gọn trong một chat thay vì rải ra các kênh khác nhau.
    """
    text = f"✅ ĐÃ KHẮC PHỤC · {ceph_code}"
    text += "\nĐã kiểm chứng lại trên cụm: lỗi không còn xuất hiện trong `ceph health detail`."
    if attempted_command:
        text += f"\n💻 Lệnh đã chạy: {_compact(attempted_command, _MAX_FOLLOWUP_FIELD_CHARS)}"
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
    )


def send_incident_verify_exhausted_alert(
    ceph_code: str,
    attempts: int,
    cluster_name: str | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
) -> None:
    """"⚠️ CHƯA KHẮC PHỤC ĐƯỢC" — đã dùng hết
    `settings.incident_verify_max_attempts` vòng chẩn đoán lại mà lỗi vẫn
    còn. Đây là điểm hệ thống chủ động BỎ CUỘC và giao lại cho người, chứ
    không im lặng thử mãi: có những lỗi không lệnh tự động nào chữa được
    (CRUSH skew cần người cân lại weight, đĩa hỏng cần thay), và mỗi vòng
    thêm chỉ tốn một lần gọi router cùng một loạt thông báo.
    """
    text = f"⚠️ CHƯA KHẮC PHỤC ĐƯỢC · {ceph_code}"
    text += (
        f"\nĐã thử {attempts} vòng khắc phục + chẩn đoán lại, kiểm chứng trên cụm vẫn thấy lỗi."
        "\nDừng tự động xử lý — cần vận hành viên vào xem trực tiếp."
    )
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
    )
