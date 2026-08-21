"""Log Intelligence -- bước L0 (Plan/log-intelligence-rca-plan.md).

Tầng T2 của thiết kế: đọc log mon/mgr/osd/rgw qua một adapter
(`watcher/log_source/`), chuẩn hoá mỗi dòng thành một "template" đã bỏ hết
biến số, rồi đếm template đó theo từng giờ.

Ba tính chất quan trọng của module này:

1. **Hoàn toàn tất định, không gọi AI.** Đây là điều làm cho bước phân tích
   AI (L2) trả nổi tiền: một cửa sổ quét chứa hàng triệu dòng thường co lại
   còn vài trăm template. AI sau này chỉ nhìn template + số đếm, không bao
   giờ nhìn log thô (plan, ràng buộc R4).

2. **Không lưu log thô.** Chỉ template, số đếm, và MỘT dòng mẫu đã được
   redact + cắt ngắn cho người đọc. Database của chính app này là tài nguyên
   có giám sát và có cảnh báo (`watcher/database_capacity_monitor.py`), nên
   log thô ở lại nguồn (plan, ràng buộc R1).

3. **Pure collector.** Không tạo Incident, không gửi Telegram, không import
   `shared.audit` -- đúng vai trò như `watcher/capability_inventory.py` và
   `watcher/crush_structure_monitor.py`. Cảnh báo và đề xuất là việc của
   bước L3/L4, dựng trên dữ liệu bảng này.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta

from config.settings import settings
from shared import db
from shared.cluster_nodes import configured_nodes
from shared.clusters import ensure_default_cluster
from shared.models import (
    Cluster,
    LogIngestRun,
    LogIngestStatus,
    LogFinding,
    LogFindingStatus,
    LogPattern,
    LogPatternObservation,
)
from watcher import log_analysis, log_triage
from watcher.log_source import get_log_source
from watcher.log_source.base import DAEMON_TYPES, LogRecord

logger = logging.getLogger(__name__)

# Một dòng mẫu giữ lại cho người đọc -- cắt ngắn để không biến bảng
# log_patterns thành nơi chứa log thô (ràng buộc R1).
SAMPLE_LINE_MAX_CHARS = 500
# Chặn trần độ dài template: một dòng log bệnh lý (stack trace nhiều KB,
# dump JSON) không được phép trở thành một hàng khổng lồ trong DB.
TEMPLATE_MAX_CHARS = 500

# --- Redaction (ràng buộc R6) ------------------------------------------
#
# Chạy TRƯỚC khi bất cứ thứ gì được lưu hoặc (ở L2) gửi tới model. Trọng
# tâm là log RGW: chữ ký S3 và presigned URL nằm ngay trên dòng access log,
# không phải thứ phải đào mới thấy.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # cephx key: "AQBxyz...==" -- luôn bắt đầu bằng AQ, base64, dài.
    (re.compile(r"\bAQ[A-Za-z0-9+/=]{20,}"), "<CEPHX_KEY>"),
    # Che tới HẾT DÒNG, không chỉ token đầu: một header Authorization thật có
    # dạng "AWS4-HMAC-SHA256 Credential=AKIA... SignedHeaders=... Signature=..."
    # -- chỉ che "AWS4-HMAC-SHA256" sẽ để lọt nguyên phần credential phía sau
    # (test_secrets_never_survive_redaction bắt được đúng lỗi này). Chấp nhận
    # mất phần đuôi dòng: với header xác thực, an toàn thắng evidence.
    (re.compile(r"(?i)\bauthorization:\s*.*$"), "authorization: <REDACTED>"),
    (re.compile(r"(?i)\bcredential=[^\s,&]+"), "Credential=<REDACTED>"),
    (re.compile(r"(?i)\bsignature=[A-Za-z0-9%/+=]+"), "Signature=<REDACTED>"),
    (re.compile(r"(?i)\bX-Amz-Signature=[A-Za-z0-9%]+"), "X-Amz-Signature=<REDACTED>"),
    (re.compile(r"(?i)\bX-Amz-Credential=[^&\s]+"), "X-Amz-Credential=<REDACTED>"),
    (re.compile(r"(?i)\bx-amz-security-token=[^&\s]+"), "x-amz-security-token=<REDACTED>"),
    (re.compile(r"(?i)\b(access_key|secret_key|secret|password|passwd|token)"
                r"\s*[=:]\s*\S+"), r"\1=<REDACTED>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "bearer <REDACTED>"),
)


def redact(text: str) -> str:
    """Bỏ mọi bí mật đã biết khỏi một dòng log.

    Cố ý KHÔNG che IP/hostname: đó chính là evidence mà RCA cần
    (`heartbeat_check: no reply from 10.0.0.5`), và chúng không phải bí mật
    trong mạng nội bộ của cụm.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# --- Chuẩn hoá thành template ------------------------------------------
#
# Thứ tự CÓ Ý NGHĨA: mẫu cụ thể phải chạy trước mẫu tổng quát, nếu không
# `<N>` sẽ nuốt mất phần số bên trong địa chỉ/uuid/pg id trước khi những
# mẫu đó kịp khớp.
_NORMALIZATIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # ISO timestamp có/không offset múi giờ.
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{4}|Z)?"), "<TS>"),
    # Timestamp kiểu syslog: "Aug 18 10:23:45".
    (re.compile(r"\b[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b"), "<TS>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b"), "<UUID>"),
    # IPv4 kèm cổng và/hoặc hậu tố nonce của Ceph ("10.0.0.5:6802/12345").
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/\d+)?"), "<ADDR>"),
    # Định danh daemon: osd.12, mon.a, mgr.x, client.admin.
    (re.compile(r"\b(osd|mon|mgr|mds|client)\.[A-Za-z0-9_-]+"), r"\1.<ID>"),
    # PG id: "2.1f4" / "2.1f4s0".
    (re.compile(r"\b\d+\.[0-9a-f]{1,5}(?:s\d+)?\b"), "<PG>"),
    # Thread id dạng hex dài của Ceph ("7f8b1c2d3700").
    (re.compile(r"\b[0-9a-f]{12,16}\b"), "<TID>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<HEX>"),
    (re.compile(r"/(?:[\w.\-]+/)+[\w.\-]*"), "<PATH>"),
    # Số cuối cùng, sau khi mọi thứ chứa số đã được thay.
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<N>"),
    # Gom khoảng trắng để hai dòng chỉ khác nhau ở căn lề không tạo ra hai
    # template khác nhau.
    (re.compile(r"\s+"), " "),
)

# Ceph daemon log: "<ts> <thread-hex> <prio> <phần còn lại>".
_CEPH_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{4}|Z)?)\s+"
    r"(?P<thread>[0-9a-f]{6,16})\s+"
    r"(?P<prio>-?\d+)\s+"
    r"(?P<message>.*)$"
)

# journalctl bọc dòng trên bằng tiền tố của nó:
# "Aug 18 10:23:45 host ceph-osd[123]: <dòng ceph thật>".
_JOURNAL_PREFIX_RE = re.compile(
    r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+[\w.\-]+\[\d+\]:\s*"
)


def normalize(message: str) -> str:
    """Biến một dòng log thành template đã bỏ biến số."""
    for pattern, replacement in _NORMALIZATIONS:
        message = pattern.sub(replacement, message)
    return message.strip()[:TEMPLATE_MAX_CHARS]


def fingerprint_of(template: str, daemon_type: str) -> str:
    """sha1(template + daemon_type).

    Gộp cả `daemon_type` vào: cùng một câu chữ phát ra từ mon và từ osd là
    hai hiện tượng khác nhau với người điều tra, không nên gộp số đếm.
    """
    return hashlib.sha1(f"{daemon_type}\x00{template}".encode()).hexdigest()


def parse_log_line(line: str, host: str, daemon_type: str) -> LogRecord | None:
    """Một dòng thô -> `LogRecord`, hoặc None nếu dòng rỗng/không dùng được.

    Dòng không khớp định dạng Ceph KHÔNG bị bỏ: nó vẫn được giữ với
    `ts=None`, `severity=None`. Đây là chủ ý -- những dòng lạ, sai định
    dạng, hoặc do một daemon bất thường sinh ra chính là thứ RCA quan tâm
    nhất, và cũng là thứ một parser cứng nhắc sẽ âm thầm đánh rơi.
    """
    line = line.rstrip("\n")
    if not line.strip():
        return None
    # Bỏ tiêu đề phân tách của cephadm khi một host có nhiều daemon cùng loại.
    if line.startswith("--- ") and line.endswith(" ---"):
        return None

    raw = line
    line = _JOURNAL_PREFIX_RE.sub("", line)

    ts: datetime | None = None
    severity: int | None = None
    message = line

    match = _CEPH_LINE_RE.match(line)
    if match:
        ts = _parse_ceph_timestamp(match.group("ts"))
        try:
            severity = int(match.group("prio"))
        except (TypeError, ValueError):
            severity = None
        message = match.group("message")

    return LogRecord(
        ts=ts,
        host=host,
        daemon_type=daemon_type,
        message=redact(message),
        raw=redact(raw)[:SAMPLE_LINE_MAX_CHARS],
        severity=severity,
    )


def parse_log_lines(raw_output: str, host: str, daemon_type: str) -> list[LogRecord]:
    records = []
    for line in raw_output.splitlines():
        record = parse_log_line(line, host=host, daemon_type=daemon_type)
        if record is not None:
            records.append(record)
    return records


def _parse_ceph_timestamp(value: str) -> datetime | None:
    """Ceph ghi offset múi giờ dạng '+0700' (không có dấu hai chấm).

    Trả về UTC **naive** -- toàn bộ codebase này dùng `datetime.utcnow()`
    naive, nên trả về tz-aware sẽ vỡ ngay ở phép so sánh cửa sổ thời gian
    ("can't compare offset-naive and offset-aware datetimes"). Một dòng log
    ghi 10:23 +0700 phải thành 03:23 UTC, nếu không mọi bản ghi từ cụm ở
    VN sẽ rơi ra ngoài cửa sổ và bị bỏ im lặng.
    """
    text = value.replace("T", " ").replace("Z", "+0000")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return _to_utc_naive(parsed)
    return None


def _to_utc_naive(parsed: datetime) -> datetime:
    """tz-aware -> UTC naive; naive giữ nguyên (coi như đã là giờ máy chủ)."""
    offset = parsed.utcoffset()
    if offset is None:
        return parsed
    return (parsed - offset).replace(tzinfo=None)


# --- Quét và lưu -------------------------------------------------------


def scan_and_store(cluster_id: str | None = None, cluster: Cluster | None = None) -> str | None:
    """Một chu kỳ quét -- gọi từ vòng lặp `watcher/main.py` theo cadence
    riêng `log_intel_scan_interval_seconds`.

    Trả về id của `LogIngestRun` vừa ghi (None nếu tính năng đang tắt).
    Không bao giờ raise: một node chết chỉ làm lần quét thành PARTIAL, đúng
    nếp best-effort của mọi collector khác trong Watcher.
    """
    if not settings.log_intel_enabled:
        return None

    # The default-cluster loop historically passed only cluster_id.  That
    # was enough for DB scoping, but not for source adapters: Loki then saw
    # cluster=None and queried the literal label cluster="default" instead
    # of the real Cluster.name (for example "CS-LAB").  HTTP 200 + an empty
    # result made this fail silently forever.  Resolve the row once here so
    # source selection, node inventory and AI host validation all share the
    # same real cluster context.
    if cluster is None and cluster_id is not None:
        with db.SessionLocal() as session:
            cluster = session.get(Cluster, cluster_id)
            if cluster is not None:
                session.expunge(cluster)

    window_end = datetime.utcnow()
    window_start = window_end - timedelta(minutes=max(1, settings.log_intel_window_minutes))

    try:
        source = get_log_source(settings.log_intel_source)
    except Exception as exc:
        logger.warning("log_intel.scan_and_store: nguồn log không hợp lệ: %s", exc)
        return _store_run(
            cluster_id, settings.log_intel_source, window_start, window_end,
            status=LogIngestStatus.FAILED, error_message=str(exc),
        )

    hosts = _hosts_by_daemon_type(cluster)
    if not hosts:
        return _store_run(
            cluster_id, settings.log_intel_source, window_start, window_end,
            status=LogIngestStatus.FAILED,
            error_message="Chưa cấu hình node nào cho cụm này",
        )

    records: list[LogRecord] = []
    errors: list[str] = []
    hosts_scanned = 0
    hosts_failed = 0

    for host, daemon_types in sorted(hosts.items()):
        host_had_error = False
        for daemon_type in sorted(daemon_types):
            result = source.fetch(host, daemon_type, window_start, window_end, cluster)
            records.extend(result.records)
            if result.error:
                errors.append(result.error)
                host_had_error = True
        hosts_scanned += 1
        if host_had_error:
            hosts_failed += 1

    if hosts_failed == hosts_scanned:
        status = LogIngestStatus.FAILED
    elif errors:
        status = LogIngestStatus.PARTIAL
    else:
        status = LogIngestStatus.OK

    # A reachable Loki returning zero lines is not proof that collection is
    # healthy.  In production this masked both a wrong label selector and a
    # stopped shipper for more than 100 scans.  Keep the run (useful
    # provenance), but mark it incomplete and explain what to verify.
    if settings.log_intel_source == "loki" and not records and status is LogIngestStatus.OK:
        status = LogIngestStatus.PARTIAL
        errors.append(
            "Loki trả 0 dòng log; kiểm tra label cluster/host/daemon_type và trạng thái log shipper"
        )

    cluster_id, seen, new = _persist_patterns(records, cluster_id, window_end)

    # L1: triage chạy NGAY SAU khi mẫu của cửa sổ này đã được lưu -- nó đọc
    # lại chính những hàng vừa ghi, nên thứ tự này là bắt buộc. Bọc riêng
    # try/except: triage là tầng phân tích, một lỗi ở đó không được phép
    # làm mất kết quả THU THẬP đã hoàn tất (dữ liệu đã nằm trong DB rồi).
    flagged: list = []
    try:
        flagged = log_triage.triage_window(cluster_id, window_start, window_end)
    except Exception:
        logger.exception("log_intel.scan_and_store: triage thất bại (dữ liệu thu thập vẫn được giữ)")

    if flagged:
        # WARNING chứ không phải INFO: trước khi L3 (Telegram) và L4
        # (Dashboard) có mặt, log của Watcher LÀ kênh duy nhất vận hành
        # nhìn thấy kết quả triage.
        logger.warning(
            "log_intel: %s | %s",
            log_triage.summarize(flagged),
            "; ".join(f"[{'+'.join(r.value for r in f.reasons)}] {f.template[:120]}"
                      for f in flagged[:5]),
        )

    run_id = _store_run(
        cluster_id, settings.log_intel_source, window_start, window_end,
        status=status,
        hosts_scanned=hosts_scanned,
        hosts_failed=hosts_failed,
        lines_scanned=len(records),
        patterns_seen=seen,
        patterns_new=new,
        patterns_flagged=len(flagged),
        # Chặn trần: 50 node chết không được biến một cột thành trang lỗi.
        error_message="\n".join(errors[:20]) or None,
    )

    # L2: phân tích AI chạy CUỐI CÙNG, sau khi lần quét đã được ghi -- nó
    # cần `run_id` làm provenance, và quan trọng hơn: nếu nó hỏng (router
    # chết, câu trả lời rác) thì mọi thứ L0/L1 thu thập được đã nằm an toàn
    # trong DB rồi. analyze_window() tự nuốt lỗi, nhưng bọc thêm một lớp ở
    # đây để kể cả một lỗi ngoài dự tính cũng không thoát ra vòng lặp
    # Watcher. Tự no-op khi settings.log_intel_ai_enabled tắt (mặc định).
    try:
        log_analysis.analyze_window(
            cluster_id, run_id, window_start, window_end, flagged, status.value, cluster,
        )
    except Exception:
        logger.exception("log_intel: phân tích AI thất bại (dữ liệu thu thập vẫn được giữ)")

    # L3: đóng những phát hiện mà mẫu log của nó đã ngừng xuất hiện. Chạy
    # KỂ CẢ khi phân tích AI ở trên bị tắt hoặc lỗi -- nó chỉ đọc
    # `LogPattern.last_seen_at` mà L0 vẫn cập nhật đều, nên vòng đời
    # OPEN -> RESOLVED không bao giờ bị kẹt vì router AI chết. Chỉ bỏ qua
    # khi lần quét FAILED: lúc đó không đọc được log của node nào cả, nên
    # "mẫu không còn xuất hiện" là kết luận sai -- sẽ đóng nhầm hàng loạt
    # phát hiện đang còn nguyên giá trị.
    if status is not LogIngestStatus.FAILED:
        try:
            log_analysis.resolve_stale_findings(cluster_id, window_start, cluster)
        except Exception:
            logger.exception("log_intel: đóng phát hiện cũ thất bại")

    return run_id


def _hosts_by_daemon_type(cluster: Cluster | None) -> dict[str, set[str]]:
    """Node nào chạy daemon nào, lấy từ đúng allowlist mà mọi đường SSH khác
    trong codebase này đã dùng (`shared/cluster_nodes.py`) -- không tự dò,
    không tự đoán host."""
    mapping: dict[str, set[str]] = {}
    for node in configured_nodes(cluster):
        for role in node.get("roles") or []:
            # 2026-08-20 — LỖI CÓ THẬT: `configured_nodes()` trả role VIẾT
            # HOA ("MON"/"MGR"/"OSD"/"RGW", xem shared/cluster_nodes.py),
            # còn DAEMON_TYPES là chữ thường. So sánh thẳng thì KHÔNG BAO
            # GIỜ khớp, hàm này luôn trả về {} và toàn bộ Log Intelligence
            # không thu nổi một dòng log nào — mà vẫn báo "quét xong" chứ
            # không lỗi. Chuẩn hoá về chữ thường, vì DAEMON_TYPES cũng là
            # giá trị đi vào nhãn `daemon_type` của Loki (chữ thường).
            daemon_type = role.lower()
            if daemon_type in DAEMON_TYPES:
                mapping.setdefault(node["host"], set()).add(daemon_type)
    return mapping


def _persist_patterns(
    records: list[LogRecord], cluster_id: str | None, fallback_ts: datetime
) -> tuple[str, int, int]:
    """Gộp record thành template, upsert `LogPattern`, cộng dồn
    `LogPatternObservation`.

    Trả về (cluster_id đã resolve, số template thấy, số template mới) --
    cluster_id được trả ra vì L1's triage cần nó và đây là nơi duy nhất
    `ensure_default_cluster` đã chạy, không nên mở thêm một session nữa chỉ
    để hỏi lại điều đã biết."""
    if not records:
        with db.SessionLocal() as session:
            return (cluster_id or ensure_default_cluster(session).id, 0, 0)

    # Gộp trong bộ nhớ trước, để mỗi template chỉ chạm DB một lần dù nó xuất
    # hiện 100k lần trong cửa sổ.
    aggregated: dict[tuple[str, str], dict] = {}
    for record in records:
        template = normalize(record.message)
        if not template:
            continue
        fingerprint = fingerprint_of(template, record.daemon_type)
        ts = record.ts or fallback_ts
        bucket = ts.replace(minute=0, second=0, microsecond=0)
        key = (fingerprint, record.host)
        entry = aggregated.get(key)
        if entry is None:
            aggregated[key] = {
                "fingerprint": fingerprint,
                "template": template,
                "daemon_type": record.daemon_type,
                "severity": record.severity,
                "sample_line": record.raw,
                "host": record.host,
                "buckets": {bucket: 1},
                "first_ts": ts,
                "last_ts": ts,
            }
        else:
            entry["buckets"][bucket] = entry["buckets"].get(bucket, 0) + 1
            entry["first_ts"] = min(entry["first_ts"], ts)
            entry["last_ts"] = max(entry["last_ts"], ts)
            # Ưu tiên giữ dòng mẫu của bản ghi nghiêm trọng nhất.
            if record.severity is not None and (
                entry["severity"] is None or record.severity < entry["severity"]
            ):
                entry["severity"] = record.severity
                entry["sample_line"] = record.raw

    new_patterns = 0
    with db.SessionLocal() as session:
        if cluster_id is None:
            cluster_id = ensure_default_cluster(session).id

        for entry in aggregated.values():
            pattern = (
                session.query(LogPattern)
                .filter(LogPattern.cluster_id == cluster_id)
                .filter(LogPattern.fingerprint == entry["fingerprint"])
                .one_or_none()
            )
            occurrences = sum(entry["buckets"].values())
            if pattern is None:
                pattern = LogPattern(
                    cluster_id=cluster_id,
                    fingerprint=entry["fingerprint"],
                    template=entry["template"],
                    daemon_type=entry["daemon_type"],
                    severity=entry["severity"],
                    sample_line=entry["sample_line"],
                    first_seen_at=entry["first_ts"],
                    last_seen_at=entry["last_ts"],
                    total_count=occurrences,
                )
                session.add(pattern)
                session.flush()  # cần pattern.id cho observation bên dưới
                new_patterns += 1
            else:
                pattern.last_seen_at = max(pattern.last_seen_at, entry["last_ts"])
                pattern.first_seen_at = min(pattern.first_seen_at, entry["first_ts"])
                pattern.total_count = (pattern.total_count or 0) + occurrences
                if pattern.sample_line is None:
                    pattern.sample_line = entry["sample_line"]

            for bucket, count in entry["buckets"].items():
                _upsert_observation(session, pattern.id, bucket, entry["host"], count)

        session.commit()

    return (cluster_id, len({key[0] for key in aggregated}), new_patterns)


def _upsert_observation(session, pattern_id: str, bucket: datetime, host: str, count: int) -> None:
    """Cộng dồn vào ô (pattern, giờ, host).

    CỘNG DỒN chứ không ghi đè, vì `log_intel_window_minutes` cố ý lớn hơn
    `log_intel_scan_interval_seconds` (để một tick chậm không tạo lỗ hổng
    dữ liệu). Đánh đổi đã biết và chấp nhận: phần chồng lấn bị đếm hai lần,
    nên số đếm là ƯỚC LƯỢNG TRÊN của tần suất thật. Với mục đích của L1
    (so sánh tương đối giữa giờ này và baseline cùng khung giờ) thì sai số
    đều nhau ở mọi ô, không làm lệch kết luận. Đổi thành ghi đè sẽ tệ hơn
    nhiều: một tick chậm sẽ ÂM THẦM XOÁ mất số đếm của giờ trước đó.
    """
    observation = (
        session.query(LogPatternObservation)
        .filter(LogPatternObservation.pattern_id == pattern_id)
        .filter(LogPatternObservation.bucket_hour == bucket)
        .filter(LogPatternObservation.host == host)
        .one_or_none()
    )
    if observation is None:
        session.add(
            LogPatternObservation(
                pattern_id=pattern_id, bucket_hour=bucket, host=host, count=count
            )
        )
    else:
        observation.count = (observation.count or 0) + count


def _store_run(
    cluster_id: str | None,
    source: str,
    window_start: datetime,
    window_end: datetime,
    *,
    status: LogIngestStatus,
    hosts_scanned: int = 0,
    hosts_failed: int = 0,
    lines_scanned: int = 0,
    patterns_seen: int = 0,
    patterns_new: int = 0,
    patterns_flagged: int | None = None,
    error_message: str | None = None,
) -> str:
    with db.SessionLocal() as session:
        if cluster_id is None:
            cluster_id = ensure_default_cluster(session).id
        run = LogIngestRun(
            cluster_id=cluster_id,
            source=source,
            window_start=window_start,
            window_end=window_end,
            status=status.value,
            hosts_scanned=hosts_scanned,
            hosts_failed=hosts_failed,
            lines_scanned=lines_scanned,
            patterns_seen=patterns_seen,
            patterns_new=patterns_new,
            patterns_flagged=patterns_flagged,
            error_message=error_message,
        )
        session.add(run)
        session.commit()
        return run.id


def prune_old_rows(now: datetime | None = None) -> tuple[int, int, int]:
    """Xoá dữ liệu quá hạn -- cùng khuôn cutoff như
    `watcher/vitastor_monitor.py`. Trả về
    (observations, runs, findings) đã xoá.

    `log_pattern_observations` có hạn NGẮN hơn hẳn (mặc định 30 ngày so với
    180): nó là bảng duy nhất ở đây phình theo KHỐI LƯỢNG log chứ không phải
    theo SỐ LOẠI log. Xem ràng buộc R1 và
    `watcher/database_capacity_monitor.py`.

    **THỨ TỰ XOÁ LÀ BẮT BUỘC** (sửa 2026-08-19, L6): `log_findings` có khoá
    ngoại NOT NULL trỏ vào `log_ingest_runs`. Bản đầu xoá thẳng
    `log_ingest_runs` theo cutoff nên trên Postgres (luôn cưỡng chế FK) nó
    ném IntegrityError ngay khi có một finding còn trỏ vào một lần quét quá
    hạn. Tệ hơn cả việc ném lỗi: lệnh xoá observations nằm CÙNG transaction
    nên bị rollback theo -- tức retention ngừng hoạt động HOÀN TOÀN, âm
    thầm, đúng kiểu phình DB mà ràng buộc R1 sinh ra để tránh (sqlite mặc
    định TẮT cưỡng chế FK nên test sqlite không tự lộ ra; ca này được kiểm
    riêng với `PRAGMA foreign_keys=ON`).

    Nên thứ tự đúng là:

    1. Xoá finding quá hạn -- **chỉ những cái đã RESOLVED**. Một finding còn
       OPEN/ACKNOWLEDGED không bao giờ bị xoá vì già: nó vẫn là việc chưa
       xong của người trực.
    2. Xoá observations quá hạn.
    3. Xoá lần quét quá hạn **và không còn finding nào trỏ vào** -- giữ
       nguyên provenance cho mọi finding còn được lưu.

    `log_patterns` cố ý KHÔNG bị xoá: nó là danh mục, phình theo SỐ LOẠI log
    (hàng trăm) chứ không theo khối lượng, và nó là nơi mọi finding neo
    evidence vào -- xoá nó sẽ làm rỗng bằng chứng của những kết luận còn
    hiệu lực.
    """
    now = now or datetime.utcnow()
    obs_cutoff = now - timedelta(days=max(1, settings.log_intel_observation_retention_days))
    run_cutoff = now - timedelta(days=max(1, settings.log_intel_pattern_retention_days))
    finding_cutoff = now - timedelta(days=max(1, settings.log_intel_finding_retention_days))

    with db.SessionLocal() as session:
        findings_deleted = (
            session.query(LogFinding)
            .filter(LogFinding.created_at < finding_cutoff)
            .filter(LogFinding.status == LogFindingStatus.RESOLVED.value)
            .delete(synchronize_session=False)
        )
        # Phải flush trước khi tính danh sách run còn được tham chiếu bên
        # dưới, nếu không truy vấn con vẫn thấy các finding vừa xoá.
        session.flush()

        observations_deleted = (
            session.query(LogPatternObservation)
            .filter(LogPatternObservation.bucket_hour < obs_cutoff)
            .delete(synchronize_session=False)
        )

        still_referenced = session.query(LogFinding.ingest_run_id).distinct().subquery()
        runs_deleted = (
            session.query(LogIngestRun)
            .filter(LogIngestRun.created_at < run_cutoff)
            .filter(~LogIngestRun.id.in_(session.query(still_referenced.c.ingest_run_id)))
            .delete(synchronize_session=False)
        )
        session.commit()
    return (observations_deleted, runs_deleted, findings_deleted)
