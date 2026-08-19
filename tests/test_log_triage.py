"""Log Intelligence L1 — tầng triage tất định (watcher/log_triage.py).

Đây là chốt chặn chi phí của cả tính năng: chỉ mẫu được gắn cờ ở đây mới
bao giờ được đưa lên model ở L2. Nên nhóm test quan trọng nhất trong file
này không phải "có gắn cờ đúng cái xấu không", mà là **"có IM LẶNG đúng
lúc không"** — một tầng triage gắn cờ mọi thứ thì tương đương không có.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import settings
from shared import db as db_module
from shared.db import Base
from shared.models import Cluster, LogPattern, LogPatternObservation, LogPatternTriageLabel
from watcher import log_triage
from watcher.log_triage import TriageReason

WINDOW_START = datetime(2026, 8, 18, 10, 0)
WINDOW_END = datetime(2026, 8, 18, 11, 0)


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="default", ceph_mon_nodes="10.0.0.1",
            ssh_user="root", ssh_key_path="/root/.ssh/id_rsa",
        )
        session.add(cluster)
        session.commit()
        yield cluster.id


@pytest.fixture(autouse=True)
def stable_thresholds(monkeypatch):
    monkeypatch.setattr(settings, "log_intel_baseline_days", 7)
    monkeypatch.setattr(settings, "log_intel_novelty_min_count", 3)
    monkeypatch.setattr(settings, "log_intel_burst_ratio", 5.0)
    monkeypatch.setattr(settings, "log_intel_burst_min_baseline_samples", 3)


def _add_pattern(
    cluster_id,
    template="osd.<ID> some routine message <N>",
    *,
    daemon_type="osd",
    severity=0,
    first_seen=None,
    label=LogPatternTriageLabel.UNKNOWN,
    fingerprint=None,
):
    with db_module.SessionLocal() as session:
        pattern = LogPattern(
            cluster_id=cluster_id,
            fingerprint=fingerprint or template[:38],
            template=template,
            daemon_type=daemon_type,
            severity=severity,
            first_seen_at=first_seen or (WINDOW_START - timedelta(days=30)),
            last_seen_at=WINDOW_START + timedelta(minutes=5),
            total_count=1,
            triage_label=label.value,
        )
        session.add(pattern)
        session.commit()
        return pattern.id


def _observe(pattern_id, bucket, count, host="10.0.0.1"):
    with db_module.SessionLocal() as session:
        session.add(LogPatternObservation(
            pattern_id=pattern_id, bucket_hour=bucket, host=host, count=count
        ))
        session.commit()


def _triage(cluster_id):
    return log_triage.triage_window(cluster_id, WINDOW_START, WINDOW_END)


# --- Im lặng đúng lúc (nhóm quan trọng nhất) -------------------------------


def test_ordinary_pattern_at_normal_rate_is_silent(isolated_db):
    """Mẫu cũ, mức info, tần suất y hệt mọi ngày -> KHÔNG gắn cờ."""
    pattern_id = _add_pattern(isolated_db)
    for day in range(1, 8):
        _observe(pattern_id, WINDOW_START - timedelta(days=day), 10)
    _observe(pattern_id, WINDOW_START, 11)

    assert _triage(isolated_db) == []


def test_benign_label_silences_even_a_severe_pattern(isolated_db):
    """Nhãn BENIGN của operator thắng mọi lý do khác — đây là cách tắt nhiễu
    mà không phải sửa code."""
    pattern_id = _add_pattern(
        isolated_db,
        template="osd.<ID> heartbeat_check: no reply from <ADDR>",
        severity=-1,
        first_seen=WINDOW_START,
        label=LogPatternTriageLabel.BENIGN,
    )
    _observe(pattern_id, WINDOW_START, 500)

    assert _triage(isolated_db) == []


def test_seasonal_pattern_is_not_flagged_at_its_usual_peak_hour(isolated_db):
    """Điểm mấu chốt của thiết kế: so sánh theo CÙNG KHUNG GIỜ TRONG NGÀY.

    Một mẫu lúc nào cũng bùng lên lúc 10h sáng thì 10h sáng nay bùng lên
    KHÔNG phải bất thường. Nếu dùng trung bình phẳng cả ngày, cái này sẽ bị
    gắn cờ sai mỗi sáng — và tầng triage sẽ mất hết uy tín với vận hành.
    """
    pattern_id = _add_pattern(isolated_db)
    for day in range(1, 8):
        base = WINDOW_START - timedelta(days=day)
        _observe(pattern_id, base, 100)                      # 10h: luôn cao
        _observe(pattern_id, base + timedelta(hours=4), 2)   # 14h: luôn thấp
    _observe(pattern_id, WINDOW_START, 110)                  # 10h nay: vẫn cao

    assert _triage(isolated_db) == []


def test_burst_is_not_flagged_without_enough_baseline_samples(isolated_db):
    """Tuần đầu chạy, baseline còn rỗng — không đủ mẫu thì im lặng, không
    đoán. Đây là thứ giữ cho ngày đầu bật tính năng không thành mưa cảnh
    báo giả."""
    pattern_id = _add_pattern(isolated_db)
    _observe(pattern_id, WINDOW_START - timedelta(days=1), 1)  # chỉ 1 mẫu
    _observe(pattern_id, WINDOW_START, 900)

    results = _triage(isolated_db)

    assert results == []


def test_pattern_absent_from_window_is_not_considered(isolated_db):
    """Mẫu im lặng từ tuần trước không phải việc của lần triage này."""
    with db_module.SessionLocal() as session:
        session.add(LogPattern(
            cluster_id=isolated_db, fingerprint="old", template="osd.<ID> quiet",
            daemon_type="osd", severity=-1,
            first_seen_at=WINDOW_START - timedelta(days=30),
            last_seen_at=WINDOW_START - timedelta(days=10),  # trước cửa sổ
            total_count=5,
        ))
        session.commit()

    assert _triage(isolated_db) == []


# --- Gắn cờ đúng cái đáng gắn ---------------------------------------------


def test_novel_pattern_is_flagged(isolated_db):
    pattern_id = _add_pattern(isolated_db, first_seen=WINDOW_START + timedelta(minutes=1))
    _observe(pattern_id, WINDOW_START, 5)

    results = _triage(isolated_db)

    assert len(results) == 1
    assert TriageReason.NOVEL in results[0].reasons


def test_novel_pattern_below_min_count_is_ignored(isolated_db):
    """Một dòng log lạ xuất hiện đúng 1 lần thường là nhiễu."""
    pattern_id = _add_pattern(isolated_db, first_seen=WINDOW_START + timedelta(minutes=1))
    _observe(pattern_id, WINDOW_START, 1)

    assert _triage(isolated_db) == []


def test_error_priority_is_flagged_severe(isolated_db):
    pattern_id = _add_pattern(isolated_db, severity=-1)
    _observe(pattern_id, WINDOW_START, 1)

    results = _triage(isolated_db)

    assert TriageReason.SEVERE in results[0].reasons


@pytest.mark.parametrize("template", [
    "osd.<ID> heartbeat_check: no reply from <ADDR>",
    "<N> slow requests are blocked",
    "log_channel cluster scrub error on pg <PG>",
    "osd.<ID> pg <PG> is inconsistent",
    "auth: failed to authenticate client",
    "*** Caught signal (Segmentation fault) **",
    "osd.<ID> is full, stopping writes",
    "osd.<ID> map gap, too far behind",
])
def test_core_ceph_keywords_are_flagged_even_at_info_priority(isolated_db, template):
    """Từ khoá hạt nhân bắt được cả khi Ceph không ghi ở mức lỗi."""
    pattern_id = _add_pattern(isolated_db, template=template, severity=0, fingerprint=template[:38])
    _observe(pattern_id, WINDOW_START, 1)

    results = _triage(isolated_db)

    assert results and TriageReason.SEVERE in results[0].reasons


@pytest.mark.parametrize("benign_template", [
    "osd.<ID> successfully started",
    "mgr.<ID> respawning after config change",
    "mon.<ID> calling monitor election",
])
def test_generic_words_do_not_trigger_severe(isolated_db, benign_template):
    """Không đưa từ chung chung như error/failed vào danh sách hạt nhân —
    nếu không tầng lọc mất hết tác dụng. 'successfully' không được khớp
    'full'."""
    pattern_id = _add_pattern(
        isolated_db, template=benign_template, severity=0, fingerprint=benign_template[:38]
    )
    for day in range(1, 8):
        _observe(pattern_id, WINDOW_START - timedelta(days=day), 10)
    _observe(pattern_id, WINDOW_START, 10)

    assert _triage(isolated_db) == []


def test_burst_against_same_hour_baseline_is_flagged(isolated_db):
    pattern_id = _add_pattern(isolated_db)
    for day in range(1, 8):
        _observe(pattern_id, WINDOW_START - timedelta(days=day), 10)
    _observe(pattern_id, WINDOW_START, 80)  # 8x baseline

    results = _triage(isolated_db)

    assert len(results) == 1
    assert TriageReason.BURST in results[0].reasons
    assert results[0].baseline_mean == pytest.approx(10.0)
    assert results[0].burst_ratio == pytest.approx(8.0)


def test_notable_label_always_surfaces(isolated_db):
    pattern_id = _add_pattern(isolated_db, label=LogPatternTriageLabel.NOTABLE)
    for day in range(1, 8):
        _observe(pattern_id, WINDOW_START - timedelta(days=day), 10)
    _observe(pattern_id, WINDOW_START, 10)  # hoàn toàn bình thường

    results = _triage(isolated_db)

    assert results[0].reasons == [TriageReason.NOTABLE]


# --- Evidence và thứ tự ----------------------------------------------------


def test_baseline_is_none_not_zero_when_unmeasurable(isolated_db):
    """Phân biệt 'chưa đo được' với 'đo ra 0' — yêu cầu evidence của roadmap
    mục 3.1, và ở L2 nó quyết định model được kết luận hay phải trả
    INSUFFICIENT_EVIDENCE."""
    pattern_id = _add_pattern(isolated_db, severity=-1)
    _observe(pattern_id, WINDOW_START, 3)

    result = _triage(isolated_db)[0]

    assert result.baseline_mean is None
    assert result.burst_ratio is None


def test_window_count_and_hosts_are_aggregated(isolated_db):
    pattern_id = _add_pattern(isolated_db, severity=-1)
    _observe(pattern_id, WINDOW_START, 4, host="10.0.0.1")
    _observe(pattern_id, WINDOW_START, 6, host="10.0.0.2")

    result = _triage(isolated_db)[0]

    assert result.window_count == 10
    assert result.hosts == ["10.0.0.1", "10.0.0.2"]


def test_results_sorted_most_notable_first(isolated_db):
    """Bên gọi (L2/L3) cắt top-N, nên thứ tự phải đưa thứ đáng chú ý nhất
    lên trước."""
    mild = _add_pattern(isolated_db, template="osd.<ID> mild thing", severity=-1, fingerprint="mild")
    _observe(mild, WINDOW_START, 1)

    severe = _add_pattern(
        isolated_db, template="osd.<ID> heartbeat_check: no reply from <ADDR>",
        severity=-1, first_seen=WINDOW_START, fingerprint="severe",
    )
    _observe(severe, WINDOW_START, 50)

    results = _triage(isolated_db)

    assert results[0].fingerprint == "severe"
    assert len(results[0].reasons) > len(results[1].reasons)


def test_summarize_counts_by_reason(isolated_db):
    pattern_id = _add_pattern(
        isolated_db, template="osd.<ID> heartbeat_check: no reply",
        severity=-1, first_seen=WINDOW_START,
    )
    _observe(pattern_id, WINDOW_START, 10)

    text = log_triage.summarize(_triage(isolated_db))

    assert "1 mẫu log bất thường" in text
    assert "NOVEL" in text and "SEVERE" in text


def test_summarize_when_nothing_flagged():
    assert log_triage.summarize([]) == "không có mẫu log bất thường"
