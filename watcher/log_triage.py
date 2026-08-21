"""Log Intelligence -- bước L1 / tầng T3 (Plan/log-intelligence-rca-plan.md).

Quyết định "mẫu log nào đáng nhìn" trong một cửa sổ thời gian, hoàn toàn
**tất định**: không gọi AI, không tốn token, chạy được kể cả khi tầng AI
(L2) đang tắt hoặc router AI chết.

Đây là chốt chặn chi phí của cả tính năng (plan, ràng buộc R4). L0 đã co
hàng triệu dòng log thành vài trăm mẫu; module này co tiếp vài trăm mẫu đó
xuống còn vài mẫu thực sự bất thường. Chỉ những mẫu được gắn cờ ở đây mới
bao giờ được đưa lên model ở L2.

Bốn lý do gắn cờ (một mẫu chỉ cần thoả MỘT):

- `NOTABLE`  -- operator đã tự đánh dấu "luôn báo cho tôi".
- `NOVEL`    -- mẫu chưa từng xuất hiện trước cửa sổ này, và lặp đủ nhiều
                để không phải nhiễu một lần.
- `SEVERE`   -- Ceph tự ghi ở mức lỗi, hoặc khớp từ khoá hạt nhân.
- `BURST`    -- tần suất cao bất thường so với chính nó ở cùng khung giờ
                những ngày trước.

Mẫu bị operator gắn `BENIGN` thì bị loại bỏ trước mọi kiểm tra khác -- đó
là cách "dạy" hệ thống im lặng mà không cần sửa code.

Module này KHÔNG ghi gì vào DB và không có bảng riêng: kết quả triage luôn
tính lại được từ `log_patterns` + `log_pattern_observations`, nên lưu thêm
một bảng nữa chỉ tạo ra thứ có thể lệch với nguồn sự thật. `log_intel.py`
gọi hàm ở đây sau mỗi lần quét chỉ để GHI SỐ ĐẾM vào `LogIngestRun` và log
cảnh báo cho vận hành.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config.settings import settings
from shared import db
from shared.models import LogPattern, LogPatternObservation, LogPatternTriageLabel

# Ceph ghi mức ưu tiên âm cho lỗi (`derr` = -1); 0 trở lên là info/debug.
SEVERE_PRIORITY_MAX = -1

# Từ khoá hạt nhân: cố ý HẸP và đặc trưng cho Ceph. Khớp trên template đã
# chuẩn hoá (đã bỏ số/địa chỉ/id) chứ không phải dòng thô.
#
# Không đưa vào những từ chung chung như "error"/"failed" -- chúng xuất
# hiện trong quá nhiều dòng lành tính và sẽ làm tầng này mất tác dụng lọc,
# đúng thứ nó sinh ra để làm. Dùng ranh giới từ cho những từ dễ khớp nhầm
# ("full" phải là từ riêng, không phải phần của chữ khác).
_SEVERE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(expr, re.IGNORECASE)
    for expr in (
        r"heartbeat_check",          # mất kết nối giữa các OSD
        r"slow request",             # op bị nghẽn
        r"slow ops",
        r"scrub error",              # phát hiện sai lệch dữ liệu
        r"\binconsistent\b",
        r"failed to authenticate",   # sự cố cephx
        r"caught signal",            # daemon crash
        r"assert_fail|\bassertion\b",
        r"\bcorrupt(?:ed|ion)?\b",
        r"\bfull\b",                 # osd/pool full
        r"\bdamaged\b",
        r"map gap",                  # OSD tụt lại quá xa khỏi osdmap
        r"\bwedged\b",
    )
)

# Một số subsystem ghi event vận hành ở priority âm dù nội dung không phải
# lỗi. Danh sách cố ý hẹp, chỉ khớp mẫu đã xác minh trên production; không
# dùng từ chung như "started"/"idle" để tránh che lỗi thật.
_KNOWN_BENIGN_SEVERE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(expr, re.IGNORECASE)
    for expr in (
        r"^rocksdb: EVENT_LOG_v1 .*\"event\": \"(?:flush|compaction)_(?:started|finished)\"",
        r"^rgw user sync thread: user is idle, not doing a full sync",
    )
)


class TriageReason(str, enum.Enum):
    NOTABLE = "NOTABLE"
    NOVEL = "NOVEL"
    SEVERE = "SEVERE"
    BURST = "BURST"


@dataclass
class TriageResult:
    """Một mẫu bị gắn cờ, kèm bằng chứng số học vì sao.

    `baseline_mean`/`burst_ratio` là None khi không đủ mẫu lịch sử để so
    sánh -- KHÔNG phải 0. Phân biệt "đã đo, bằng 0" với "chưa đo được" là
    yêu cầu evidence của roadmap mục 3.1, và ở L2 nó là thứ quyết định
    model được kết luận hay phải trả INSUFFICIENT_EVIDENCE.
    """

    pattern_id: str
    fingerprint: str
    template: str
    daemon_type: str
    severity: int | None
    sample_line: str | None
    window_count: int
    reasons: list[TriageReason] = field(default_factory=list)
    baseline_mean: float | None = None
    burst_ratio: float | None = None
    hosts: list[str] = field(default_factory=list)

    @property
    def is_flagged(self) -> bool:
        return bool(self.reasons)


def triage_window(
    cluster_id: str,
    window_start: datetime,
    window_end: datetime,
) -> list[TriageResult]:
    """Trả về các mẫu đáng chú ý trong [window_start, window_end].

    Sắp xếp theo mức đáng chú ý giảm dần: nhiều lý do trước, rồi đến tỉ lệ
    đột biến, rồi đến số lần xuất hiện -- để bên gọi (L2/L3/L4) có thể cắt
    top-N mà vẫn giữ đúng thứ quan trọng nhất.
    """
    bucket_start = window_start.replace(minute=0, second=0, microsecond=0)
    baseline_start = bucket_start - timedelta(days=max(1, settings.log_intel_baseline_days))

    with db.SessionLocal() as session:
        # Chỉ xét mẫu THỰC SỰ xuất hiện trong cửa sổ này. Một mẫu im lặng
        # từ tuần trước không phải việc của lần triage này.
        patterns = (
            session.query(LogPattern)
            .filter(LogPattern.cluster_id == cluster_id)
            .filter(LogPattern.last_seen_at >= window_start)
            .all()
        )
        candidates = [
            p for p in patterns
            if p.triage_label != LogPatternTriageLabel.BENIGN.value
        ]
        if not candidates:
            return []

        # MỘT truy vấn cho toàn bộ observation của mọi ứng viên trong cả
        # khoảng baseline + cửa sổ, rồi gộp trong bộ nhớ -- tránh N*2 truy
        # vấn khi có vài trăm mẫu.
        pattern_ids = [p.id for p in candidates]
        observations = (
            session.query(LogPatternObservation)
            .filter(LogPatternObservation.pattern_id.in_(pattern_ids))
            .filter(LogPatternObservation.bucket_hour >= baseline_start)
            .filter(LogPatternObservation.bucket_hour <= window_end)
            .all()
        )

        by_pattern: dict[str, list[LogPatternObservation]] = {}
        for observation in observations:
            by_pattern.setdefault(observation.pattern_id, []).append(observation)

        results = []
        for pattern in candidates:
            result = _evaluate(
                pattern, by_pattern.get(pattern.id, []), bucket_start, window_start, window_end
            )
            if result.is_flagged:
                results.append(result)

    results.sort(
        key=lambda r: (len(r.reasons), r.burst_ratio or 0.0, r.window_count),
        reverse=True,
    )
    return results


def _evaluate(
    pattern: LogPattern,
    observations: list[LogPatternObservation],
    bucket_start: datetime,
    window_start: datetime,
    window_end: datetime,
) -> TriageResult:
    in_window = [o for o in observations if bucket_start <= o.bucket_hour <= window_end]
    window_count = sum(o.count for o in in_window)
    hosts = sorted({o.host for o in in_window})

    result = TriageResult(
        pattern_id=pattern.id,
        fingerprint=pattern.fingerprint,
        template=pattern.template,
        daemon_type=pattern.daemon_type,
        severity=pattern.severity,
        sample_line=pattern.sample_line,
        window_count=window_count,
        hosts=hosts,
    )

    if pattern.triage_label == LogPatternTriageLabel.NOTABLE.value:
        result.reasons.append(TriageReason.NOTABLE)

    if (
        # Dùng đúng đầu cửa sổ, không dùng đầu giờ của observation bucket.
        # Nếu scan chạy 10:55 với cửa sổ từ 09:55, một pattern xuất hiện
        # 09:05 không còn là NOVEL. So với bucket_start=09:00 trước đây làm
        # nó bị báo lặp gần hai giờ, đặc biệt tệ sau khi onboarding Loki.
        pattern.first_seen_at >= window_start
        and window_count >= max(1, settings.log_intel_novelty_min_count)
    ):
        result.reasons.append(TriageReason.NOVEL)

    if _is_severe(pattern):
        result.reasons.append(TriageReason.SEVERE)

    baseline_mean, burst_ratio = _burst_check(observations, in_window, bucket_start)
    result.baseline_mean = baseline_mean
    result.burst_ratio = burst_ratio
    if burst_ratio is not None and burst_ratio >= settings.log_intel_burst_ratio:
        result.reasons.append(TriageReason.BURST)

    return result


def _is_severe(pattern: LogPattern) -> bool:
    if any(expr.search(pattern.template) for expr in _KNOWN_BENIGN_SEVERE_PATTERNS):
        return False
    if pattern.severity is not None and pattern.severity <= SEVERE_PRIORITY_MAX:
        return True
    return any(expr.search(pattern.template) for expr in _SEVERE_PATTERNS)


def _burst_check(
    observations: list[LogPatternObservation],
    in_window: list[LogPatternObservation],
    bucket_start: datetime,
) -> tuple[float | None, float | None]:
    """So sánh từng ô giờ trong cửa sổ với CÙNG KHUNG GIỜ những ngày trước.

    So theo giờ-trong-ngày chứ không phải trung bình phẳng toàn bộ lịch sử,
    vì cụm Ceph có nhịp ngày rõ rệt (scrub đêm, backup rạng sáng, giờ cao
    điểm). Một mẫu lúc nào cũng nhiều vào 3h sáng thì 3h sáng nay nhiều
    KHÔNG phải bất thường -- trung bình phẳng sẽ gắn cờ sai mỗi đêm.

    Trả về (baseline_mean, burst_ratio); cả hai là None khi lịch sử chưa đủ
    mẫu để kết luận -- không đủ mẫu thì im lặng, không đoán.
    """
    if not in_window:
        return (None, None)

    # Gộp theo ô giờ (một ô có thể trải trên nhiều host).
    def totals_by_bucket(rows):
        totals: dict[datetime, int] = {}
        for row in rows:
            totals[row.bucket_hour] = totals.get(row.bucket_hour, 0) + row.count
        return totals

    window_totals = totals_by_bucket(in_window)
    history_totals = totals_by_bucket(
        [o for o in observations if o.bucket_hour < bucket_start]
    )

    best_mean: float | None = None
    best_ratio: float | None = None
    min_samples = max(1, settings.log_intel_burst_min_baseline_samples)

    for bucket, current in window_totals.items():
        samples = [
            count
            for historical_bucket, count in history_totals.items()
            if historical_bucket.hour == bucket.hour
        ]
        if len(samples) < min_samples:
            continue
        mean = sum(samples) / len(samples)
        if mean <= 0:
            # Baseline toàn 0 mà giờ có log: đó là NOVEL/lần đầu ở khung
            # giờ này, không phải "đột biến" đo được -- để lý do NOVEL/
            # SEVERE bắt, không bịa ra một tỉ lệ chia cho 0.
            continue
        ratio = current / mean
        if best_ratio is None or ratio > best_ratio:
            best_ratio = ratio
            best_mean = mean

    return (best_mean, best_ratio)


def summarize(results: list[TriageResult]) -> str:
    """Một dòng tóm tắt cho log vận hành / thông báo sau này."""
    if not results:
        return "không có mẫu log bất thường"
    by_reason: dict[str, int] = {}
    for result in results:
        for reason in result.reasons:
            by_reason[reason.value] = by_reason.get(reason.value, 0) + 1
    detail = ", ".join(f"{name}={count}" for name, count in sorted(by_reason.items()))
    return f"{len(results)} mẫu log bất thường ({detail})"
