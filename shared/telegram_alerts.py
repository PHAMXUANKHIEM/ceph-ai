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

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import re
import threading
from queue import Queue
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from config.settings import settings
from shared.notification_channels import enqueue_external_alert
from shared.telegram_client import TelegramSendError, send_telegram_message
from shared.telegram_humanizer import (
    HUMANIZER_CLI_TIMEOUT_SECONDS,
    humanize_log_for_telegram,
)

logger = logging.getLogger(__name__)

# Truncation length for a ceph_code's log_excerpt — some (e.g. a PG dump or
# a slow-ops list) can run to several KB, well past what's useful to read
# in a phone notification, and needlessly close to Telegram's own 4096-char
# message limit once the rest of the text is added.
_MAX_EXCERPT_CHARS = 700
_MAX_FOLLOWUP_FIELD_CHARS = 240
# Một câu tóm tắt do humanizer sinh ra dài tối đa 3 câu; cắt nó ở 180 ký tự
# như trước là tự tay dựng lại đúng câu cụt mà humanizer vừa loại bỏ.
_NATURAL_FIELD_CHARS = 360

# Ceph health status -> mức độ dùng cho webhook/Slack/email.
_EXTERNAL_SEVERITY_BY_HEALTH = {
    "HEALTH_ERR": "critical",
    "HEALTH_WARN": "warning",
    "HEALTH_OK": "info",
}

_BACKGROUND_ALERT_EXECUTOR = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="telegram-alert"
)

_INCIDENT_SEVERITY_PREFIX = {
    "HEALTH_ERR": "\U0001f534 HEALTH_ERR",  # red circle
    "HEALTH_WARN": "\U0001f7e1 HEALTH_WARN",  # yellow circle
}

_OSD_HOST_RE = re.compile(r"---\s*([^\s(]+)\s+\(osd\.(\d+)\)", re.IGNORECASE)
_BLUESTORE_HEARTBEAT_RE = re.compile(
    r"no reply from\s+(?P<peer_ip>[^\s:]+):(?P<peer_port>\d+)\s+"
    r"osd\.(?P<peer_osd>\d+)",
    re.IGNORECASE,
)
_LARGE_OMAP_FIELD_RE = re.compile(
    r"\b(?P<name>bucket|object|keys|threshold|shards|pg)=(?P<value>[^\s]*)",
    re.IGNORECASE,
)

_INCIDENT_EXPLANATIONS = {
    "MON_DOWN": "Một Monitor (MON) đang không hoạt động; cụm có thể mất khả năng điều phối nếu không còn đủ MON.",
    "MGR_DOWN": "Một Manager (MGR) đang không hoạt động; dashboard và một số dịch vụ quản lý có thể bị ảnh hưởng.",
    "PG_DEGRADED": "Một hoặc nhiều Placement Group (PG) chưa đủ bản sao; dữ liệu vẫn có thể truy cập nhưng đang thiếu dư thừa.",
    "PG_AVAILABILITY": "Một hoặc nhiều PG có nguy cơ không phục vụ được dữ liệu; cần kiểm tra OSD và trạng thái PG ngay.",
    "OSD_FULL": "OSD đã đầy hoặc gần đầy đến mức Ceph có thể chặn ghi dữ liệu.",
    "OSD_NEARFULL": "OSD sắp đầy; cần kiểm tra dung lượng và kế hoạch cân bằng/mở rộng trước khi đầy.",
    "MON_CLOCK_SKEW": "Thời gian giữa các Monitor bị lệch; cần kiểm tra đồng bộ NTP/chrony trên các node.",
    "POOL_APP_NOT_ENABLED": "Pool chưa bật ứng dụng Ceph tương ứng (thường là rbd/cephfs/rgw); dữ liệu hiện có không đồng nghĩa pool đã được cấu hình đúng.",
}

# Mã health check của Ceph -> tên gọi tiếng Việt cho dòng tiêu đề. Bản thân
# mã vẫn được giữ trong ngoặc: người trực đọc tiêu đề để biết chuyện gì đang
# xảy ra, còn mã là thứ để tra cứu tiếp trên cụm. Khác với
# `_INCIDENT_EXPLANATIONS` (một câu giải thích hệ quả), đây là một cụm danh
# từ ngắn đủ để đọc lướt trên màn hình khoá điện thoại.
_INCIDENT_TITLES = {
    "OSD_DOWN": "Một OSD đã ngừng hoạt động",
    "OSD_HOST_DOWN": "Toàn bộ OSD trên một node đã ngừng hoạt động",
    "OSD_ROOT_DOWN": "Toàn bộ OSD trong một nhánh CRUSH đã ngừng hoạt động",
    "OSD_UNREACHABLE": "Không liên lạc được với một OSD",
    "MON_DOWN": "Một Monitor đã ngừng hoạt động",
    "MGR_DOWN": "Một Manager đã ngừng hoạt động",
    "MDS_ALL_DOWN": "Toàn bộ Metadata Server đã ngừng hoạt động",
    "RECENT_CRASH": "Có daemon vừa crash gần đây",
    "RECENT_MGR_MODULE_CRASH": "Một module của Manager vừa crash",
    "OSD_FULL": "OSD đã đầy, Ceph đang chặn ghi dữ liệu",
    "OSD_NEARFULL": "OSD sắp đầy",
    "OSD_BACKFILLFULL": "OSD đầy tới mức không backfill được",
    "POOL_FULL": "Pool đã đầy",
    "POOL_NEARFULL": "Pool sắp đầy",
    "POOL_NEAR_FULL": "Pool sắp đầy",
    "PG_DEGRADED": "Dữ liệu đang thiếu bản sao",
    "PG_AVAILABILITY": "Một phần dữ liệu có nguy cơ không truy cập được",
    "PG_DAMAGED": "Có Placement Group bị hỏng dữ liệu",
    "PG_BACKFILL_FULL": "Không còn chỗ trống để phục hồi dữ liệu",
    "PG_RECOVERY_FULL": "Không còn chỗ trống để phục hồi dữ liệu",
    "PG_NOT_SCRUBBED": "Có Placement Group quá hạn scrub",
    "PG_NOT_DEEP_SCRUBBED": "Có Placement Group quá hạn deep-scrub",
    "OSD_SCRUB_ERRORS": "Scrub phát hiện dữ liệu không khớp giữa các bản sao",
    "SLOW_OPS": "Có thao tác đang bị chậm bất thường",
    "BLUESTORE_SLOW_OP_ALERT": "BlueStore xử lý chậm bất thường",
    "OSD_SLOW_PING_TIME_BACK": "Mạng giữa các OSD phản hồi chậm",
    "OSD_SLOW_PING_TIME_FRONT": "Mạng giữa các OSD phản hồi chậm",
    "LARGE_OMAP_OBJECTS": "Có object chỉ mục OMAP quá lớn",
    "POOL_APP_NOT_ENABLED": "Pool chưa bật ứng dụng tương ứng",
    "POOL_TOO_FEW_PGS": "Pool có quá ít Placement Group",
    "POOL_TOO_MANY_PGS": "Pool có quá nhiều Placement Group",
    "TOO_FEW_PGS": "Cụm có quá ít Placement Group",
    "TOO_MANY_PGS": "Cụm có quá nhiều Placement Group",
    "MON_CLOCK_SKEW": "Đồng hồ giữa các Monitor bị lệch",
    "MON_DISK_LOW": "Ổ đĩa của Monitor sắp đầy",
    "MON_DISK_CRIT": "Ổ đĩa của Monitor đã gần cạn",
    "CEPHADM_STRAY_DAEMON": "Có daemon không do cephadm quản lý",
    "CEPHADM_STRAY_HOST": "Có node không do cephadm quản lý",
    "CEPHADM_FAILED_DAEMON": "Có daemon cephadm khởi động thất bại",
    "AUTH_INSECURE_CLIENT_KEY_TYPE": "Có client dùng loại khoá xác thực không an toàn",
    "AUTH_INSECURE_KEYS_ALLOWED": "Cụm đang cho phép khoá xác thực không an toàn",
    "AUTH_INSECURE_GLOBAL_ID_RECLAIM": "Có client lấy lại global_id theo cách không an toàn",
    "AUTH_INSECURE_GLOBAL_ID_RECLAIM_ALLOWED": "Cụm đang cho phép lấy lại global_id không an toàn",
    "DEVICE_HEALTH": "Ceph dự đoán có ổ đĩa sắp hỏng",
    "DEVICE_HEALTH_IN_USE": "Ổ đĩa được dự đoán sắp hỏng vẫn đang được dùng",
    "DEVICE_HEALTH_TOOMANY": "Quá nhiều ổ đĩa được dự đoán sắp hỏng cùng lúc",
}

# Thuộc tính duy nhất trong sự kiện podman nói lên được sự cố; phần còn lại
# là nhãn build của image.
_CONTAINER_ATTRIBUTES_KEPT = {"name", "pod", "health_status", "exit_code"}
# Khối "(k=v, k=v, ...)"; chấp nhận cả trường hợp log đã bị cắt mất ngoặc đóng.
_CONTAINER_ATTRS_RE = re.compile(r"\((?=[^()]*=)([^()]*?)(?:\)|$)")
_IMAGE_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]+")
_MONOTONIC_CLOCK_RE = re.compile(r"\s*\bm=\+[0-9.]+")
_LONG_HEX_ID_RE = re.compile(r"\b[0-9a-f]{32,}\b")

_MACHINE_LOG_RE = re.compile(
    r"(?:traceback|stack trace|exception|container\s+(?:remove|create)|"
    r"(?:^|\n)\s*[-*]?\s*[A-Za-z_][\w.-]*\s*[:=]|[{}\[\]])",
    re.IGNORECASE,
)


def _humanize_sync(raw_text: str | None, *, context: str) -> str:
    """Bridge the async humanizer into this module's sync alert API.

    Watcher sends AI-enriched alerts from a background executor, so this
    bounded provider wait cannot block the Ceph polling loop. Direct callers
    retain the synchronous API and still receive the deterministic fallback
    when the router is unavailable.
    """
    fallback = _compact_incident_excerpt(raw_text, _MAX_EXCERPT_CHARS)
    if not fallback or not getattr(settings, "telegram_ai_humanize_enabled", False):
        return fallback

    async def invoke() -> str:
        return await humanize_log_for_telegram(fallback, context=context)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(invoke()) or fallback
        except Exception as exc:
            logger.warning("telegram humanizer bridge failed: %s", exc)
            return fallback

    result_queue: Queue[tuple[str | None, BaseException | None]] = Queue(maxsize=1)

    def run_in_thread() -> None:
        try:
            result_queue.put((asyncio.run(invoke()), None))
        except BaseException as exc:
            result_queue.put((None, exc))

    thread = threading.Thread(target=run_in_thread, name="telegram-humanizer", daemon=True)
    thread.start()
    # Phải rộng hơn timeout của provider, nếu không cầu nối bỏ cuộc trước cả
    # khi Codex/Claude kịp trả lời và humanizer thành vô dụng.
    thread.join(HUMANIZER_CLI_TIMEOUT_SECONDS + 15.0)
    if thread.is_alive():
        logger.warning("telegram humanizer bridge timed out")
        return fallback
    try:
        value, error = result_queue.get_nowait()
    except Exception:
        logger.warning("telegram humanizer bridge returned no result")
        return fallback
    if error is not None or not value:
        if error is not None:
            logger.warning("telegram humanizer bridge failed: %s", error)
        return fallback
    return value


def _needs_humanization(value: str | None) -> bool:
    text = str(value or "").strip()
    return bool(text) and (len(text) > 240 or bool(_MACHINE_LOG_RE.search(text)))


def _run_alert_in_background(name: str, callback) -> None:
    """Queue the complete humanize-then-send operation away from Watcher."""
    def run() -> None:
        try:
            callback()
        except Exception:
            logger.exception("background Telegram alert failed: %s", name)

    try:
        _BACKGROUND_ALERT_EXECUTOR.submit(run)
    except Exception:
        logger.exception("could not queue background Telegram alert: %s", name)


def _large_omap_evidence_is_empty(value: str | None) -> bool:
    """Recognize the collector's placeholder when no object was resolved."""
    lines = [line.strip() for line in (value or "").splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0].startswith("LARGE_OMAP_EVIDENCE"):
        return False
    fields = {
        match.group("name").lower(): match.group("value").strip()
        for match in _LARGE_OMAP_FIELD_RE.finditer(lines[0])
    }
    return not any(fields.get(name) for name in ("bucket", "object", "keys", "shards", "pg"))


def _translate_incident_log(ceph_code: str, log_excerpt: str | None) -> str:
    """Return a short deterministic Vietnamese explanation for an alert.

    This is intentionally not an AI call: the first alert must remain
    available when providers are down, and the original excerpt is retained
    separately as technical evidence below the explanation.
    """
    code = (ceph_code or "").upper()
    raw = log_excerpt or ""
    if code == "OSD_DOWN":
        match = _OSD_HOST_RE.search(raw)
        target = (
            f"OSD {match.group(2)} trên node {match.group(1)}"
            if match
            else "Một OSD"
        )
        explanation = f"{target} đang DOWN, tức daemon OSD hiện không hoạt động hoặc chưa kết nối lại với cụm."
        lowered = raw.lower()
        if "container remove" in lowered and "deactivate" in lowered:
            explanation += " Log cho thấy Podman đã xoá container tạm phục vụ deactivate OSD; đây là bước dọn dẹp, chưa phải nguyên nhân gốc."
        if "ceph_git_repo" in lowered or "github.com/ceph/ceph" in lowered:
            explanation += " Đường dẫn GitHub trong metadata image chỉ là thông tin mã nguồn build, không phải lỗi kết nối GitHub."
        return explanation
    if code == "BLUESTORE_SLOW_OP_ALERT":
        local = _OSD_HOST_RE.search(raw)
        heartbeat = _BLUESTORE_HEARTBEAT_RE.search(raw)
        if heartbeat:
            local_label = (
                f"OSD {local.group(2)} trên node {local.group(1)}"
                if local
                else "Một OSD"
            )
            peer_label = (
                f"OSD {heartbeat.group('peer_osd')} "
                f"tại {heartbeat.group('peer_ip')}:{heartbeat.group('peer_port')}"
            )
            return (
                f"{local_label} không nhận được phản hồi heartbeat từ {peer_label}. "
                "Điều này cho thấy kết nối giữa hai OSD đang chậm hoặc mất phản hồi; "
                "cần kiểm tra mạng và cả hai daemon OSD. Dấu hiệu này chưa đủ để kết luận ổ đĩa đã hỏng."
            )
        return (
            "Ceph phát hiện OSD xử lý chậm trong BlueStore; cần kiểm tra độ trễ, "
            "mạng và log của các OSD liên quan trước khi kết luận nguyên nhân."
        )
    if code == "LARGE_OMAP_OBJECTS":
        fields = {
            match.group("name").lower(): match.group("value").strip()
            for match in _LARGE_OMAP_FIELD_RE.finditer(raw)
        }
        evidence_values = [fields.get(name, "") for name in ("bucket", "object", "keys", "threshold", "shards", "pg")]
        if not any(evidence_values):
            return (
                "Ceph phát hiện một object chỉ mục OMAP quá lớn trong quá trình deep-scrub, "
                "nhưng hệ thống chưa thu thập được tên bucket, object hoặc các chỉ số liên quan. "
                "Chưa đủ bằng chứng để xác định object nào; cần kiểm tra log deep-scrub và metadata RGW."
            )
        details = []
        if fields.get("bucket"):
            details.append(f"bucket {fields['bucket']}")
        if fields.get("object"):
            details.append(f"object {fields['object']}")
        if fields.get("keys"):
            details.append(f"{fields['keys']} key")
        if fields.get("threshold"):
            details.append(f"ngưỡng {fields['threshold']}")
        if fields.get("shards"):
            details.append(f"{fields['shards']} shard")
        if fields.get("pg"):
            details.append(f"PG {fields['pg']}")
        detail_text = ", ".join(details)
        return (
            f"Ceph phát hiện object chỉ mục OMAP lớn ({detail_text}). "
            "Điều này có thể làm các thao tác metadata của RGW chậm hơn; cần kiểm tra bucket "
            "và chỉ lập kế hoạch reshard sau khi xác nhận đầy đủ evidence."
        )
    return _INCIDENT_EXPLANATIONS.get(
        code,
        "Ceph phát hiện một health check bất thường; phần Log gốc bên dưới là bằng chứng cần dùng để xác định nguyên nhân.",
    )


def _incident_title(ceph_code: str | None) -> str:
    """Tiêu đề đọc được cho một mã health check, mã giữ lại trong ngoặc."""
    code = (ceph_code or "").strip()
    if not code:
        return "Health check bất thường"
    title = _INCIDENT_TITLES.get(code.upper())
    return f"{title} ({code})" if title else f"Cảnh báo Ceph: {code}"


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


def _compact_sentence(value: str | None, limit: int) -> str:
    """Như `_compact` nhưng cắt ở ranh giới câu, kém nhất là ranh giới từ.

    Dùng cho mọi trường văn xuôi (câu đã qua humanizer, chẩn đoán/đề xuất do
    AI viết). Cắt giữa từ biến một câu tiếng Việt thành chuỗi máy, đúng thứ
    mà cả lớp humanizer đang cố tránh.
    """
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    sentence_ends = [
        match.end() for match in re.finditer(r"[.!?](?=\s)", text) if match.end() <= limit
    ]
    if sentence_ends and sentence_ends[-1] >= limit // 2:
        return text[: sentence_ends[-1]]
    window = text[: limit - 1]
    word_break = window.rfind(" ")
    return (window[:word_break] if word_break > 0 else window).rstrip() + "…"


def _readable_nodes(value: str | None) -> str:
    """`Action.target_nodes` được lưu bằng `json.dumps(list)`.

    In thẳng lên Telegram thì người trực nhận được `["10.3.53.1"]` — dấu
    ngoặc và dấu nháy của JSON, không phải câu tiếng Việt.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return _compact(raw, _MAX_FOLLOWUP_FIELD_CHARS)
    if isinstance(parsed, list):
        names = [str(item).strip() for item in parsed if str(item).strip()]
        return _compact(", ".join(names), _MAX_FOLLOWUP_FIELD_CHARS)
    return _compact(str(parsed), _MAX_FOLLOWUP_FIELD_CHARS)


def _natural(
    value: str | None,
    *,
    limit: int = _MAX_FOLLOWUP_FIELD_CHARS,
    humanize_context: str | None = None,
) -> str:
    """Chốt DUY NHẤT cho mọi trường văn xuôi đi lên Telegram.

    Trước đây mỗi hàm gửi tự chọn cách rút gọn, và chỉ 2 trong 20 hàm đi qua
    humanizer; 18 hàm còn lại đẩy thẳng chuỗi do monitor sinh ra. Giờ mọi
    trường văn xuôi đều được chuẩn hoá khoảng trắng/xuống dòng và cắt ở ranh
    giới câu tại đúng một chỗ.

    `humanize_context` CHỈ đặt ở trường dẫn của những tin nhắn (a) có nguồn
    là log máy và (b) được gửi từ executor nền. Mỗi lần gọi là một request
    tới router kèm treo tối đa ~9 giây: đặt nó trên đường quét đồng bộ của
    Watcher sẽ chặn vòng poll, còn đặt trên một câu vốn do AI viết bằng
    tiếng Việt thì chỉ là chạy LLM lên đầu ra của LLM.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if humanize_context and _needs_humanization(text):
        text = " ".join(_humanize_sync(text, context=humanize_context).split())
    return _compact_sentence(text, limit)


def _compact_multiline(value: str | None, limit: int) -> str:
    lines = [" ".join(line.split()) for line in (value or "").splitlines()]
    text = "\n".join(line for line in lines if line)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _strip_container_label_noise(value: str) -> str:
    """Bỏ nhãn image của container khỏi khối bằng chứng.

    Log podman đính kèm toàn bộ label của image vào mỗi sự kiện: digest
    sha256, id 64 ký tự hex, đồng hồ monotonic, `org.label-schema.*`,
    `GANESHA_REPO_BASEURL`. Không thứ nào trong đó là bằng chứng của sự cố,
    nhưng chúng chiếm gần hết 700 ký tự cho phép và đẩy phần có nghĩa —
    OSD nào, container nào, sự kiện gì, lúc mấy giờ — ra ngoài.

    Chạy tất định, không cần router: đây là đường DUY NHẤT còn lại khi
    humanizer bị tắt.
    """
    def _keep_known_attributes(match: re.Match) -> str:
        kept = []
        for part in match.group(1).split(","):
            key, separator, attribute = part.partition("=")
            if separator and key.strip().lower() in _CONTAINER_ATTRIBUTES_KEPT:
                kept.append(f"{key.strip()}={attribute.strip()}")
        return f" ({', '.join(kept)})" if kept else ""

    text = _CONTAINER_ATTRS_RE.sub(_keep_known_attributes, value)
    text = _IMAGE_DIGEST_RE.sub("", text)
    text = _MONOTONIC_CLOCK_RE.sub("", text)
    text = _LONG_HEX_ID_RE.sub(lambda match: match.group(0)[:12], text)
    return text


def _compact_incident_excerpt(value: str | None, limit: int) -> str:
    marker = "Dung lượng chi tiết:"
    raw = _strip_container_label_noise(value or "")
    if marker not in raw:
        return _compact(raw, limit)
    before, context = raw.split(marker, 1)
    context_text = _compact_multiline(marker + context, limit)
    remaining = limit - len(context_text) - 1
    if remaining <= 20:
        return context_text
    before_text = _compact(before, remaining)
    return f"{before_text}\n{context_text}" if before_text else context_text


def _send(
    bot_token: str,
    chat_id: str,
    enabled: bool,
    text: str,
    cluster_name: str | None = None,
    *,
    category: str = "incident",
    severity: str = "warning",
) -> bool:
    """`enabled` (2026-08-07, Alert Telegram page) is a SEPARATE on/off
    switch from "configured" (bot_token/chat_id both non-blank) — lets an
    operator pause a channel with one click without losing/retyping its
    Chat ID, unlike the earlier "blank the chat id to pause" design.

    `category`/`severity` (2026-09-15) đến thẳng từ hàm gửi. Trước đây chúng
    được đoán bằng cách dò chuỗi trong chính tin nhắn đã dựng, nên (a) đoán
    sai sẵn — một cảnh báo cụm có chữ "log daemon" bị xếp vào
    log-intelligence, một cảnh báo có chữ "node " bị xếp vào hardware — và
    (b) khoá cứng cách phân loại vào câu chữ: sửa lời văn cho tự nhiên hơn
    là alert lặng lẽ đổi kênh webhook/Slack/email."""
    resolved_cluster = (cluster_name if cluster_name is not None else settings.cluster_name).strip()
    enqueue_external_alert(
        category=category, severity=severity, message=text, cluster_name=resolved_cluster,
    )
    if not enabled or not bot_token or not chat_id:
        return False
    try:
        send_telegram_message(bot_token, chat_id, _with_cluster_prefix(text, cluster_name))
        return True
    except TelegramSendError:
        logger.exception("shared.telegram_alerts: Telegram delivery failed")
        return False


def send_volume_forecast_alert(
    *, pool: str, image: str, metric: str, horizon_hours: int,
    current_value: float, predicted_value: float,
    threshold_type: str | None, threshold_value: float | None,
    confidence: float, training_samples: int, training_window_hours: int,
    model_version: str, target_at: datetime, cluster_name: str | None = None,
    bot_token: str | None = None, chat_id: str | None = None,
    enabled: bool | None = None,
) -> bool:
    """Send one fail-closed warning to the dedicated RBD forecast channel."""
    labels = {
        "iops": "Số thao tác mỗi giây (IOPS)", "read_latency_ms": "Độ trễ đọc",
        "write_latency_ms": "Độ trễ ghi",
    }
    threshold_labels = {
        "latency_slo_ms": "Ngưỡng độ trễ cam kết",
        "measured_knee_iops": "Ngưỡng IOPS bão hoà (90%)",
    }
    unit = " ms" if metric.endswith("latency_ms") else " IOPS"
    if target_at.tzinfo is None:
        target_at = target_at.replace(tzinfo=UTC)
    target_vn = target_at.astimezone(ZoneInfo("Asia/Ho_Chi_Minh")).strftime("%H:%M:%S - %d/%m/%Y")
    threshold = (
        f"{threshold_value:.2f}{unit}" if threshold_value is not None else "Chưa xác định"
    )
    text = (
        "⚠️ CẢNH BÁO SỚM RBD VOLUME\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💽 Volume: {pool}/{image}\n"
        f"📊 Chỉ số: {labels.get(metric, metric)}\n"
        f"⏱️ Dự báo: {predicted_value:.2f}{unit} sau {horizon_hours} giờ\n"
        f"🎯 {threshold_labels.get(threshold_type or '', threshold_type or 'Ngưỡng')}: {threshold}\n"
        f"📈 Hiện tại: {current_value:.2f}{unit}\n"
        f"🧠 Độ tin cậy: {confidence * 100:.1f}%\n"
        f"🗂️ Dữ liệu học: {training_samples} mẫu / cửa sổ {training_window_hours} giờ\n"
        f"🔬 Mô hình dự báo: {model_version}\n"
        f"⏰ Thời điểm dự kiến: {target_vn}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ Chỉ cảnh báo — hệ thống không tự chỉnh QoS hoặc resize."
    )
    return _send(
        bot_token if bot_token is not None else settings.telegram_rbd_forecast_bot_token,
        chat_id if chat_id is not None else settings.telegram_rbd_forecast_chat_id,
        enabled if enabled is not None else settings.telegram_rbd_forecast_enabled,
        text, cluster_name,
        category="incident", severity="warning",
    )


def send_code_repair_alert(text: str) -> None:
    """Send a status update for the application code-repair supervisor."""
    _send(
        settings.telegram_code_repair_bot_token,
        settings.telegram_code_repair_chat_id,
        settings.telegram_code_repair_enabled,
        text,
        category="code-repair", severity="info",
    )


def send_ai_ops_digest_alert(text: str, *, cluster_name: str | None = None) -> bool:
    """Send a periodic read-only operations digest through the incident channel."""
    return _send(
        settings.telegram_incident_bot_token,
        settings.telegram_incident_chat_id,
        settings.telegram_incident_enabled,
        text,
        cluster_name,
        category="incident", severity="info",
    )


def send_performance_rca_alert(
    analysis: dict,
    *,
    cluster_name: str | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
) -> bool:
    """Send one read-only RCA candidate through the incident channel."""
    host_lines = []
    seen_hosts = set()
    for host in (analysis.get("host_evidence") or [])[:4]:
        host_name = host.get("host") or host.get("node_name") or "unknown"
        host_key = str(host_name).strip().lower().rstrip(".")
        if host_key in seen_hosts:
            continue
        seen_hosts.add(host_key)
        flags = ", ".join(host.get("flags") or [])
        suffix = f" · {flags}" if flags else ""
        host_lines.append(
            f"🖥 {host_name}: "
            f"CPU {float(host.get('cpu_percent') or 0):.1f}% · "
            f"RAM {float(host.get('mem_percent') or 0):.1f}% · "
            f"ổ đĩa {float(host.get('disk_latency_ms') or 0):.2f}ms{suffix}"
        )
    text_lines = [
        "🧠 PHÂN TÍCH NGUYÊN NHÂN HIỆU NĂNG",
        "━━━━━━━━━━━━━━━━━━",
        f"💽 Volume: {analysis.get('pool', '—')}/{analysis.get('image', '—')}",
        f"🔎 Ứng viên: {_natural(analysis.get('hypothesis'), limit=100)}",
        f"📈 Độ tin cậy: {float(analysis.get('confidence') or 0) * 100:.1f}%",
        f"⏱️ Độ trễ hiện tại: {float(analysis.get('current_latency_ms') or 0):.2f}ms "
        f"(mức nền {float(analysis.get('baseline_latency_ms') or 0):.2f}ms)",
        f"ℹ️ {_natural(analysis.get('explanation'))}",
    ]
    if host_lines:
        text_lines.append("Bằng chứng theo node:")
        text_lines.extend(host_lines)
    text_lines.extend((
        "━━━━━━━━━━━━━━━━━━",
        "Chỉ là ứng viên tương quan; chưa tự thay đổi Ceph.",
    ))
    return _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        "\n".join(text_lines),
        cluster_name,
        category="incident", severity="warning",
    )


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
    *,
    background: bool = False,
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
    if background:
        _run_alert_in_background(
            "incident",
            lambda: send_incident_alert(
                ceph_code,
                severity,
                log_excerpt,
                cluster_name,
                bot_token,
                chat_id,
                enabled,
                reminder,
                diagnosis_text,
                rationale,
                background=False,
            ),
        )
        return

    prefix = _INCIDENT_SEVERITY_PREFIX.get(severity or "", f"⚠️ {severity or 'SỰ CỐ'}")
    # Ngoại lệ DUY NHẤT của `_natural`: khối bằng chứng phải giữ nguyên bố
    # cục nhiều dòng của "Dung lượng chi tiết:", còn `_natural` thì gộp hết
    # về một dòng.
    excerpt = _compact_incident_excerpt(log_excerpt, _MAX_EXCERPT_CHARS)
    explanation = _translate_incident_log(ceph_code, log_excerpt)
    humanized = False
    if ceph_code.upper() == "LARGE_OMAP_OBJECTS" and _large_omap_evidence_is_empty(log_excerpt):
        excerpt = "Chưa thu thập được chi tiết bucket/object cho cảnh báo này."
    elif _needs_humanization(log_excerpt):
        compact_source = _compact_incident_excerpt(log_excerpt, _MAX_EXCERPT_CHARS)
        excerpt = _humanize_sync(log_excerpt, context=f"log gốc {ceph_code}")
        humanized = bool(excerpt and excerpt != compact_source)
    reminder_prefix = "🔁 NHẮC LẠI · " if reminder else ""
    text = f"{reminder_prefix}{prefix} · {_incident_title(ceph_code)}"
    text += f"\n📝 Diễn giải: {explanation}"
    if excerpt:
        detail_label = "📖 Giải thích chi tiết:" if humanized else "🔎 Bằng chứng kỹ thuật:"
        text += f"\n{detail_label}\n{_compact_multiline(excerpt, _MAX_EXCERPT_CHARS)}"
    if reminder and diagnosis_text:
        text += f"\n🧠 Tóm tắt AI: {_natural(diagnosis_text)}"
    if reminder and rationale:
        text += f"\n🔧 Giải pháp: {_natural(rationale)}"
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
        category="incident",
        severity=_EXTERNAL_SEVERITY_BY_HEALTH.get(severity or "", "warning"),
    )


def send_periodic_health_status(
    status: str, check_codes: list[str], *, cluster_name: str | None = None,
    bot_token: str | None = None, chat_id: str | None = None,
    enabled: bool | None = None,
) -> None:
    """Periodic 10-minute cluster-health heartbeat for operators.

    Đây là tin nhắn người trực nhìn thấy nhiều nhất trong ngày, nên danh
    sách mã thô (`Health checks: OSD_DOWN, PG_DEGRADED`) được đổi thành tên
    gọi tiếng Việt; mã vẫn nằm trong ngoặc để tra cứu tiếp."""
    icon = {"HEALTH_OK": "🟢", "HEALTH_WARN": "🟡", "HEALTH_ERR": "🔴"}.get(status, "⚪")
    codes = sorted(check_codes)
    if not codes:
        details = "✅ Không có cảnh báo nào đang mở."
    else:
        # Mỗi cảnh báo một dòng: gộp chúng vào một dòng ngăn bằng dấu chấm
        # phẩy thì trên điện thoại chỉ còn là một khối chữ chạy dài.
        lines = [f"⚠️ Đang mở {len(codes)} cảnh báo:"]
        lines.extend(f"• {_incident_title(code)}" for code in codes[:4])
        if len(codes) > 4:
            lines.append(f"• và {len(codes) - 4} cảnh báo khác")
        details = "\n".join(lines)
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        f"📊 Trạng thái định kỳ: {icon} {status}\n{details}",
        cluster_name,
        category="incident",
        severity=_EXTERNAL_SEVERITY_BY_HEALTH.get(status, "warning"),
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
        f"{prefix} Cụm Vitastor\n{_natural(detail, limit=_MAX_EXCERPT_CHARS)}",
        cluster_name,
        category="incident",
        severity={"CRITICAL": "critical", "UNREACHABLE": "critical", "HEALTHY": "info"}.get(
            health, "warning"
        ),
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
            f"{prefix} · {_incident_title(ceph_code)}",
            f"🧠 Ý kiến AI: {_natural(diagnosis_text)}",
            f"🔧 Đề xuất: {_natural(rationale)}",
        )
    )
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
        category="incident",
        severity=_EXTERNAL_SEVERITY_BY_HEALTH.get(severity or "", "warning"),
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
            f"{prefix} · {_incident_title(ceph_code)}",
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
        category="incident",
        severity=_EXTERNAL_SEVERITY_BY_HEALTH.get(severity or "", "warning"),
    )
def send_node_alert(host: str, message: str) -> None:
    """Called once per NEWLY-flagged node resource problem
    (watcher/node_health_monitor.py::create_or_resolve_node_health_incidents
    — only when a new Incident is created, not resent on every scan a host
    stays flagged). No-op if the Phần cứng channel's bot token/chat id
    aren't configured yet (same reasoning as send_incident_alert above)."""
    _send(
        settings.telegram_node_bot_token,
        settings.telegram_node_chat_id,
        settings.telegram_node_enabled,
        f"\U0001f7e0 Node {host}: {_natural(message, limit=_MAX_EXCERPT_CHARS)}",
        category="hardware", severity="warning",
    )


def send_node_forecast_alert(
    host: str,
    metric: str,
    current_percent: float,
    predicted_percent: float,
    hours_to_90: float,
    confidence: float,
    samples: int,
    window_hours: int,
    *,
    cluster_name: str | None = None,
) -> bool:
    """Send an early CPU/RAM forecast warning through the hardware channel."""
    label = "RAM" if metric.lower() == "ram" else "CPU"
    text = "\n".join((
        f"🟡 CẢNH BÁO DỰ BÁO {label}",
        f"🖥 Node: {host}",
        f"Hiện tại: {current_percent:.1f}% · Dự báo: {predicted_percent:.1f}% trong {window_hours} giờ tới",
        f"⏱ Ước tính chạm 90%: sau {hours_to_90:.1f} giờ",
        f"Độ tin cậy: {confidence:.2f} · Mẫu học: {samples}",
        "Đây là cảnh báo sớm; chưa tự thay đổi cụm.",
    ))
    return _send(
        settings.telegram_node_bot_token,
        settings.telegram_node_chat_id,
        settings.telegram_node_enabled,
        text,
        cluster_name,
        category="hardware", severity="warning",
    )


def send_trash_capacity_alert(
    trash_bytes: int, total_bytes: int, ratio: float, entry_count: int, *,
    cluster_name: str | None = None, bot_token: str | None = None,
    chat_id: str | None = None, enabled: bool | None = None,
) -> bool:
    """Send RBD Trash capacity warnings through the cluster Alert channel."""
    gib = 1024 ** 3
    text = "\n".join(
        (
            "🟡 HEALTH_WARN RBD Trash vượt ngưỡng 20% dung lượng cụm",
            f"🗑 Trash: {trash_bytes / gib:.2f} GiB / {total_bytes / gib:.2f} GiB ({ratio * 100:.1f}%), {entry_count} volume.",
            "🔧 Đề xuất: kiểm tra các volume trong mục Trash và duyệt xoá vĩnh viễn những volume không còn cần khôi phục.",
        )
    )
    return _send(
        settings.telegram_incident_bot_token if bot_token is None else bot_token,
        settings.telegram_incident_chat_id if chat_id is None else chat_id,
        settings.telegram_incident_enabled if enabled is None else enabled,
        text,
        cluster_name,
        category="capacity", severity="warning",
    )


def send_capacity_threshold_alert(
    entity_type: str,
    entity_name: str,
    used_percent: float,
    threshold: int,
    used_bytes: int,
    total_bytes: int,
    *,
    cluster_name: str | None = None,
) -> bool:
    """Notify once when a cluster, pool or OSD crosses 80/90/95%."""
    severity = "🔴 CRITICAL" if threshold >= 95 else ("🟠 WARNING" if threshold >= 90 else "🟡 WARNING")
    labels = {"cluster": "Toàn cụm", "pool": "Pool", "osd": "OSD"}
    label = labels.get(entity_type, entity_type)
    entity = label if entity_type == "cluster" else f"{label} {entity_name}"
    gib = 1024 ** 3
    text = "\n".join((
        f"{severity} Dung lượng đã vượt mốc {threshold}%",
        f"💾 {entity}: {used_percent:.2f}% ({used_bytes / gib:.2f} / {total_bytes / gib:.2f} GiB)",
        "🔧 Đề xuất: kiểm tra tốc độ tăng trưởng, dọn dữ liệu an toàn hoặc bổ sung dung lượng trước khi cụm đầy.",
    ))
    return _send(
        settings.telegram_incident_bot_token,
        settings.telegram_incident_chat_id,
        settings.telegram_incident_enabled,
        text,
        cluster_name,
        category="capacity",
        severity="critical" if threshold >= 95 else "warning",
    )


def send_capacity_recovery_alert(
    entity_type: str,
    entity_name: str,
    used_percent: float,
    previous_threshold: int,
    current_threshold: int,
    *,
    cluster_name: str | None = None,
) -> bool:
    """Close or downgrade a capacity alert after usage falls."""
    labels = {"cluster": "Toàn cụm", "pool": "Pool", "osd": "OSD"}
    label = labels.get(entity_type, entity_type)
    entity = label if entity_type == "cluster" else f"{label} {entity_name}"
    remaining = (
        f"Hiện vẫn ở mức cảnh báo {current_threshold}%."
        if current_threshold else "Hiện đã dưới mốc cảnh báo 80%."
    )
    text = "\n".join((
        f"🟢 Dung lượng đã phục hồi dưới mốc {previous_threshold}%",
        f"💾 {entity}: {used_percent:.2f}%. {remaining}",
    ))
    return _send(
        settings.telegram_incident_bot_token,
        settings.telegram_incident_chat_id,
        settings.telegram_incident_enabled,
        text,
        cluster_name,
        category="capacity", severity="info",
    )


def send_osd_latency_alert(osd_id: int, host: str | None, message: str) -> None:
    """Called once per NEWLY-flagged OSD latency outlier
    (watcher/osd_latency_monitor.py::create_or_resolve_osd_latency_incidents
    — only when a new Incident is created, same "one notification per
    genuinely new problem" posture as send_node_alert above). Shares the
    Phần cứng channel with send_node_alert — see this module's own
    docstring for why there's no separate 4th channel for this."""
    label = f"osd.{osd_id}" + (f" ({host})" if host else "")
    _send(
        settings.telegram_node_bot_token,
        settings.telegram_node_chat_id,
        settings.telegram_node_enabled,
        f"\U0001f7e0 OSD chậm: {label}\n{_natural(message, limit=_MAX_EXCERPT_CHARS)}",
        category="hardware", severity="warning",
    )


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
    _send(
        settings.telegram_node_bot_token,
        settings.telegram_node_chat_id,
        settings.telegram_node_enabled,
        f"\U0001f7e0 Lệch CRUSH: {label}\n{_natural(message, limit=_MAX_EXCERPT_CHARS)}",
        category="hardware", severity="warning",
    )


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
    _send(
        settings.telegram_node_bot_token,
        settings.telegram_node_chat_id,
        settings.telegram_node_enabled,
        f"\U0001f7e0 Database ceph-aiops gần đầy\n{_natural(message, limit=_MAX_EXCERPT_CHARS)}",
        category="hardware", severity="warning",
    )


def send_auto_remediation_alert(
    ceph_code: str,
    diagnosis_text: str | None,
    rationale: str | None,
    command: str | None,
    succeeded: bool,
    action_id: str | None = None,
    target_nodes: str | None = None,
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
    if succeeded and action_id == "restart_osd_daemon":
        prefix = "✅ Khởi động lại thành công, đang xác minh Ceph"
    elif succeeded:
        prefix = "✅ Thực hiện thành công, đang xác minh"
    else:
        prefix = "\u274c Xử lý thất bại"
    lines = [f"{prefix}: {ceph_code}"]
    readable_nodes = _readable_nodes(target_nodes)
    if readable_nodes:
        lines.append(f"🎯 Đối tượng: {readable_nodes}")
    if diagnosis_text:
        lines.append(f"⚠️ Chẩn đoán: {_natural(diagnosis_text)}")
    if rationale:
        lines.append(f"🔧 Giải pháp: {_natural(rationale)}")
    if command:
        lines.append(f"💻 Lệnh: {_compact(command, _MAX_FOLLOWUP_FIELD_CHARS)}")
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        "\n".join(lines),
        category="incident",
        severity="info" if succeeded else "critical",
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
        f"🧠 Tóm tắt AI: {_natural(diagnosis_text or failure_summary)}",
        f"❌ Lỗi cụ thể: {_natural(failure_summary)}",
        f"↩️ Rollback: {_natural(rollback_summary)}",
        "🔧 Giải pháp: Sửa lỗi trên node được nêu, kiểm tra `ceph health detail`, sau đó chạy lại cập nhật từ giao diện.",
    ))
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
        category="incident", severity="critical",
    )


# --- Log Intelligence L3 (Plan/log-intelligence-rca-plan.md) --------------
#
# Generic findings use the cluster-incident channel. RGW findings use a
# dedicated notification-only channel.

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
    recommended_steps: list[str] | None = None,
    cluster_name: str | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    enabled: bool | None = None,
    daemon_types: list[str] | None = None,
    rca_stage: str | None = None,
    background: bool = False,
) -> None:
    """Gửi MỘT lần cho mỗi phát hiện log THỰC SỰ MỚI
    (`watcher/log_analysis.py` chỉ gọi khi `dedupe_key` chưa có bản ghi nào
    đang OPEN) — cùng nếp "một thông báo cho một vấn đề thật sự mới" mà
    send_node_alert/send_osd_latency_alert/send_crush_skew_alert đã theo.

    Telegram is deliberately a short on-call signal. Full summary, evidence,
    commands and every recommendation remain in the Dashboard."""
    if background:
        _run_alert_in_background(
            "log-finding",
            lambda: send_log_finding_alert(
                title,
                severity,
                confidence,
                summary,
                root_cause,
                evidence_templates,
                recommended_action_id,
                validation_notes,
                operator_commands=operator_commands,
                recommended_steps=recommended_steps,
                cluster_name=cluster_name,
                bot_token=bot_token,
                chat_id=chat_id,
                enabled=enabled,
                daemon_types=daemon_types,
                rca_stage=rca_stage,
                background=False,
            ),
        )
        return

    prefix = _LOG_FINDING_SEVERITY_PREFIX.get(severity, f"⚠️ {severity}")
    daemon_set = {value.strip().lower() for value in (daemon_types or []) if isinstance(value, str)}
    source_label = "Cảnh báo RGW do AI phân tích" if "rgw" in daemon_set else "Phát hiện từ log"
    source_icon = "🌐 " if "rgw" in daemon_set else ""
    lines = [
        f"{prefix} {source_icon}{source_label}: {_compact(title, 160)}",
        f"🎯 Tin cậy: {confidence}",
    ]
    if rca_stage:
        lines.append(f"🧭 Quy trình RCA: {_natural(rca_stage, limit=180)}")
    conclusion = root_cause or summary
    if conclusion:
        lines.append(
            "🔎 Nhận định: "
            + _natural(
                conclusion,
                limit=_NATURAL_FIELD_CHARS,
                humanize_context=f"root cause từ log {title}",
            )
        )
    clean_steps = [str(value).strip() for value in (recommended_steps or []) if str(value).strip()]
    if clean_steps:
        lines.append(f"💡 Ưu tiên: {_natural(clean_steps[0], limit=180)}")
    if validation_notes:
        lines.append(f"⚠️ Đã hiệu chỉnh: {_natural(validation_notes, limit=120)}")
    if "rgw" in daemon_set:
        selected_token = settings.telegram_rgw_bot_token
        selected_chat = settings.telegram_rgw_chat_id
        selected_enabled = settings.telegram_rgw_enabled
    else:
        selected_token = bot_token if bot_token is not None else settings.telegram_incident_bot_token
        selected_chat = chat_id if chat_id is not None else settings.telegram_incident_chat_id
        selected_enabled = enabled if enabled is not None else settings.telegram_incident_enabled
    _send(
        selected_token,
        selected_chat,
        selected_enabled,
        "\n".join(lines),
        cluster_name,
        category="log-intelligence",
        severity={"CRITICAL": "critical", "INFO": "info"}.get(severity, "warning"),
    )


def send_log_finding_resolved_alert(
    title: str, *, cluster_name: str | None = None, bot_token: str | None = None,
    chat_id: str | None = None, enabled: bool | None = None,
    daemon_types: list[str] | None = None, verification_summary: str | None = None,
) -> None:
    """Gửi khi các mẫu log của một phát hiện đã ngừng xuất hiện — đóng vòng
    đời OPEN -> RESOLVED, để người trực biết vấn đề đã hết mà không phải tự
    vào Dashboard kiểm tra."""
    daemon_set = {value.strip().lower() for value in (daemon_types or []) if isinstance(value, str)}
    is_rgw = "rgw" in daemon_set
    selected_token = settings.telegram_rgw_bot_token if is_rgw else (
        bot_token if bot_token is not None else settings.telegram_incident_bot_token
    )
    selected_chat = settings.telegram_rgw_chat_id if is_rgw else (
        chat_id if chat_id is not None else settings.telegram_incident_chat_id
    )
    selected_enabled = settings.telegram_rgw_enabled if is_rgw else (
        enabled if enabled is not None else settings.telegram_incident_enabled
    )
    lines = [
        f"✅ RGW ĐÃ XÁC NHẬN PHỤC HỒI: {_compact(title, _MAX_FOLLOWUP_FIELD_CHARS)}"
        if is_rgw else f"🟢 Đã hết: {_compact(title, _MAX_FOLLOWUP_FIELD_CHARS)}",
        "Các mẫu log liên quan không còn xuất hiện trong các lần quét gần đây.",
    ]
    if verification_summary:
        lines.append(f"🔎 Kiểm chứng trực tiếp: {_natural(verification_summary)}")
    _send(
        selected_token, selected_chat, selected_enabled, "\n".join(lines),
        cluster_name,
        category="log-intelligence", severity="info",
    )


def send_log_finding_recovery_pending_alert(
    title: str, summary: str, live_facts: tuple[str, ...] | list[str], *,
    cluster_name: str | None = None, verification_code: str | None = None,
) -> None:
    """RGW recovery gate failed; rate limiting is persisted by LogFinding."""
    lines = [
        f"⚠️ RGW CHƯA XÁC NHẬN PHỤC HỒI: {_compact(title, _MAX_FOLLOWUP_FIELD_CHARS)}",
        f"Kết luận: {_natural(summary)}",
    ]
    for fact in live_facts:
        if (
            "vault_probe[" in fact or fact.startswith("ceph_health=")
            or fact.startswith("rgw_default_key_status")
        ):
            lines.append(f"• {_compact(fact, _MAX_EXCERPT_CHARS)}")
    if verification_code == "RGW_DEFAULT_ENCRYPTION_KEY_INVALID":
        lines.extend((
            "💡 Gợi ý tốt nhất: cluster dùng Vault SSE-S3 thì gỡ "
            "rgw_crypt_default_encryption_key sai, restart tuần tự RGW và test PUT/GET SSE-S3.",
            "↪️ Phương án khác: chỉ khi chủ đích dùng automatic encryption, "
            "đặt khóa ngẫu nhiên 32 byte dạng base64.",
            "⚠️ Chưa tự chạy; cần duyệt hành động trên hàng chờ.",
        ))
    lines.append("Cảnh báo vẫn đang mở; hệ thống sẽ tự kiểm tra lại.")
    _send(
        settings.telegram_rgw_bot_token, settings.telegram_rgw_chat_id,
        settings.telegram_rgw_enabled, "\n".join(lines), cluster_name,
        category="log-intelligence", severity="warning",
    )


def send_incident_verified_alert(
    ceph_code: str,
    attempted_command: str | None = None,
    display_name: str | None = None,
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
    readable_name = _compact(display_name or ceph_code, _MAX_FOLLOWUP_FIELD_CHARS)
    text = f"✅ ĐÃ KHẮC PHỤC · {readable_name}"
    if display_name and display_name != ceph_code:
        text += f"\n🆔 Mã đối chiếu: {ceph_code}"
    text += "\nĐã kiểm chứng lại trên cụm: lỗi không còn xuất hiện trong `ceph health detail`."
    if attempted_command:
        text += f"\n💻 Lệnh đã chạy: {_compact(attempted_command, _MAX_FOLLOWUP_FIELD_CHARS)}"
    _send(
        bot_token if bot_token is not None else settings.telegram_incident_bot_token,
        chat_id if chat_id is not None else settings.telegram_incident_chat_id,
        enabled if enabled is not None else settings.telegram_incident_enabled,
        text,
        cluster_name,
        category="incident", severity="info",
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
        category="incident", severity="critical",
    )
