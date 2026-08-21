"""Log Intelligence L0 (Plan/log-intelligence-rca-plan.md).

Bao gồm cả nhóm test bảo mật bắt buộc của plan mục 9 (redaction) — nhóm
này KHÔNG được phép bỏ: log RGW mang chữ ký S3 và presigned URL ngay trên
dòng access log, và bước L2 sẽ đưa chính những dòng này vào prompt.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import settings
from shared import db as db_module
from shared.db import Base
from shared.models import (
    Cluster,
    LogFinding,
    LogFindingStatus,
    LogIngestRun,
    LogIngestStatus,
    LogPattern,
    LogPatternObservation,
)
from watcher import log_intel
from watcher.log_source.base import LogRecord, LogSourceResult


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
    yield engine


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "log_intel_enabled", True)
    monkeypatch.setattr(settings, "log_intel_source", "ssh")
    monkeypatch.setattr(settings, "log_intel_window_minutes", 60)


# --- normalize() / fingerprint_of() ----------------------------------------


def test_same_event_different_variables_collapses_to_one_template():
    """Cốt lõi của tầng T2: hai dòng cùng bản chất, khác số liệu, phải ra
    cùng một template — nếu không thì fingerprint vô dụng và AI vẫn phải
    đọc log thô."""
    a = log_intel.normalize(
        "osd.5 heartbeat_check: no reply from 10.0.0.7:6802 osd.7 ever on either front or back"
    )
    b = log_intel.normalize(
        "osd.12 heartbeat_check: no reply from 10.0.0.9:6803 osd.9 ever on either front or back"
    )
    assert a == b
    assert log_intel.fingerprint_of(a, "osd") == log_intel.fingerprint_of(b, "osd")


def test_genuinely_different_events_stay_separate():
    a = log_intel.normalize("osd.5 heartbeat_check: no reply from 10.0.0.7:6802")
    b = log_intel.normalize("osd.5 slow request osd_op(client.4136 ...) initiated")
    assert log_intel.fingerprint_of(a, "osd") != log_intel.fingerprint_of(b, "osd")


def test_same_text_from_different_daemon_types_is_not_merged():
    """Cùng câu chữ phát ra từ mon và từ osd là hai hiện tượng khác nhau với
    người điều tra — số đếm không được trộn."""
    template = log_intel.normalize("failed to authenticate client")
    assert log_intel.fingerprint_of(template, "mon") != log_intel.fingerprint_of(template, "osd")


def test_pathological_line_is_length_capped():
    """Một stack trace vài KB không được biến thành một hàng khổng lồ."""
    assert len(log_intel.normalize("x " * 5000)) <= log_intel.TEMPLATE_MAX_CHARS


# --- redaction (bắt buộc, plan mục 9) --------------------------------------


@pytest.mark.parametrize(
    "secret_line, secret_fragment",
    [
        ("mon.a auth: key AQBvcGRlbW9rZXlub3RyZWFsMTIzNDU2Nzg5MA== accepted", "AQBvcGRl"),
        ("beast: GET /b?X-Amz-Signature=deadbeefcafe1234 HTTP/1.1", "deadbeefcafe1234"),
        ("req authorization: AWS4-HMAC-SHA256 Credential=AKIAsecret", "AKIAsecret"),
        ("rgw: X-Amz-Credential=AKIAEXAMPLE/20260818/us-east-1", "AKIAEXAMPLE"),
        ("config secret_key=supersecretvalue loaded", "supersecretvalue"),
        ("auth bearer eyJhbGciOiJIUzI1NiJ9.payload", "eyJhbGciOiJIUzI1NiJ9"),
    ],
)
def test_secrets_never_survive_redaction(secret_line, secret_fragment):
    assert secret_fragment not in log_intel.redact(secret_line)


def test_redaction_keeps_addresses_because_they_are_the_evidence():
    """IP không phải bí mật trong mạng nội bộ cụm — và `no reply from <ip>`
    chính là thứ RCA cần. Che nó đi là tự làm hỏng mục đích."""
    assert "10.0.0.7" in log_intel.redact("osd.5 heartbeat_check: no reply from 10.0.0.7:6802")


def test_stored_sample_line_is_redacted_not_raw(isolated_db):
    record = log_intel.parse_log_line(
        "2026-08-18T10:23:45.123+0000 7f8b1c2d3700 -1 mon.a key "
        "AQBvcGRlbW9rZXlub3RyZWFsMTIzNDU2Nzg5MA== accepted",
        host="10.0.0.1",
        daemon_type="mon",
    )
    assert "AQBvcGRl" not in record.raw
    assert "AQBvcGRl" not in record.message


# --- parse_log_line() ------------------------------------------------------


def test_parses_ceph_daemon_line_fields():
    record = log_intel.parse_log_line(
        "2026-08-18T10:23:45.123456+0000 7f8b1c2d3700 -1 osd.5 1234 heartbeat_check: no reply",
        host="10.0.0.1",
        daemon_type="osd",
    )
    assert record.severity == -1
    assert record.ts == datetime(2026, 8, 18, 10, 23, 45, 123456)
    assert record.message.startswith("osd.5")


def test_timezone_offset_is_converted_to_utc():
    """Log cụm ở VN ghi +0700. Nếu không quy về UTC, mọi bản ghi sẽ rơi ra
    ngoài cửa sổ thời gian và bị bỏ im lặng."""
    record = log_intel.parse_log_line(
        "2026-08-18T10:23:45.000000+0700 7f8b1c2d3700 -1 osd.5 x",
        host="h", daemon_type="osd",
    )
    assert record.ts == datetime(2026, 8, 18, 3, 23, 45)


def test_journalctl_prefix_is_stripped():
    record = log_intel.parse_log_line(
        "Aug 18 10:23:45 node1 ceph-osd[1234]: "
        "2026-08-18T10:23:45.000000+0000 7f8b1c2d3700 -1 osd.5 slow request",
        host="10.0.0.1", daemon_type="osd",
    )
    assert record.severity == -1
    assert "ceph-osd[1234]" not in record.message


def test_unparseable_line_is_kept_not_dropped():
    """Dòng sai định dạng chính là thứ RCA quan tâm nhất — một parser cứng
    nhắc sẽ âm thầm đánh rơi đúng chúng."""
    record = log_intel.parse_log_line("*** Caught signal (Segmentation fault) **", "h", "osd")
    assert record is not None
    assert record.ts is None and record.severity is None


def test_blank_and_cephadm_separator_lines_are_skipped():
    assert log_intel.parse_log_line("   ", "h", "osd") is None
    assert log_intel.parse_log_line("--- osd.1 ---", "h", "osd") is None


# --- scan_and_store() ------------------------------------------------------


def _fake_source(monkeypatch, results_by_key, error_keys=()):
    class FakeSource:
        @staticmethod
        def fetch(host, daemon_type, window_start, window_end, cluster=None):
            key = (host, daemon_type)
            if key in error_keys:
                return LogSourceResult(records=[], error=f"{host}: unreachable")
            return LogSourceResult(records=results_by_key.get(key, []))

    monkeypatch.setattr(log_intel, "get_log_source", lambda name: FakeSource)


def _record(host, message, ts=None, daemon_type="osd", severity=-1):
    return LogRecord(
        ts=ts or datetime(2026, 8, 18, 10, 5, 0),
        host=host, daemon_type=daemon_type,
        message=message, raw=message, severity=severity,
    )


def _one_node(monkeypatch, roles=("osd",)):
    monkeypatch.setattr(
        log_intel, "configured_nodes", lambda cluster=None: [{"host": "10.0.0.1", "roles": list(roles)}]
    )


def test_disabled_flag_does_nothing(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "log_intel_enabled", False)
    assert log_intel.scan_and_store() is None
    with db_module.SessionLocal() as session:
        assert session.query(LogIngestRun).count() == 0


def test_scan_counts_patterns_and_marks_ok(isolated_db, enabled, monkeypatch):
    _one_node(monkeypatch)
    _fake_source(monkeypatch, {
        ("10.0.0.1", "osd"): [
            _record("10.0.0.1", "osd.5 heartbeat_check: no reply from 10.0.0.7:6802"),
            _record("10.0.0.1", "osd.9 heartbeat_check: no reply from 10.0.0.8:6803"),
            _record("10.0.0.1", "osd.5 slow request osd_op initiated"),
        ],
    })

    log_intel.scan_and_store()

    with db_module.SessionLocal() as session:
        run = session.query(LogIngestRun).one()
        assert run.status == LogIngestStatus.OK.value
        assert run.lines_scanned == 3
        # 3 dòng thô -> 2 template (hai dòng heartbeat gộp lại).
        assert run.patterns_seen == 2
        assert run.patterns_new == 2
        assert session.query(LogPattern).count() == 2
        heartbeat = (
            session.query(LogPattern).filter(LogPattern.template.like("%heartbeat%")).one()
        )
        assert heartbeat.total_count == 2


def test_unreachable_host_yields_partial_not_failure(isolated_db, enabled, monkeypatch):
    """Một node chết không được làm mất dữ liệu của node còn lại — nhưng
    phải được GHI NHẬN, vì bước L2 buộc phải trả INSUFFICIENT_EVIDENCE khi
    cửa sổ nó suy luận chỉ được thu thập một phần."""
    monkeypatch.setattr(log_intel, "configured_nodes", lambda cluster=None: [
        {"host": "10.0.0.1", "roles": ["osd"]},
        {"host": "10.0.0.2", "roles": ["osd"]},
    ])
    _fake_source(
        monkeypatch,
        {("10.0.0.1", "osd"): [_record("10.0.0.1", "osd.5 slow request")]},
        error_keys={("10.0.0.2", "osd")},
    )

    log_intel.scan_and_store()

    with db_module.SessionLocal() as session:
        run = session.query(LogIngestRun).one()
        assert run.status == LogIngestStatus.PARTIAL.value
        assert run.hosts_scanned == 2 and run.hosts_failed == 1
        assert "10.0.0.2" in run.error_message
        assert session.query(LogPattern).count() == 1  # node sống vẫn được lưu


def test_every_host_failing_is_failed_status(isolated_db, enabled, monkeypatch):
    _one_node(monkeypatch)
    _fake_source(monkeypatch, {}, error_keys={("10.0.0.1", "osd")})
    log_intel.scan_and_store()
    with db_module.SessionLocal() as session:
        assert session.query(LogIngestRun).one().status == LogIngestStatus.FAILED.value


def test_empty_loki_result_is_partial_not_false_ok(isolated_db, enabled, monkeypatch):
    monkeypatch.setattr(settings, "log_intel_source", "loki")
    _one_node(monkeypatch)
    _fake_source(monkeypatch, {})

    log_intel.scan_and_store()

    with db_module.SessionLocal() as session:
        run = session.query(LogIngestRun).one()
        assert run.status == LogIngestStatus.FAILED.value
        assert run.lines_scanned == 0
        assert "Loki trả 0 dòng" in run.error_message


def test_loki_host_with_no_stream_makes_mixed_scan_partial(isolated_db, enabled, monkeypatch):
    monkeypatch.setattr(settings, "log_intel_source", "loki")
    monkeypatch.setattr(log_intel, "configured_nodes", lambda cluster=None: [
        {"host": "10.0.0.1", "roles": ["osd"]},
        {"host": "10.0.0.2", "roles": ["osd"]},
    ])
    _fake_source(monkeypatch, {
        ("10.0.0.1", "osd"): [_record("10.0.0.1", "osd.1 slow request")],
    })

    log_intel.scan_and_store()

    with db_module.SessionLocal() as session:
        run = session.query(LogIngestRun).one()
        assert run.status == LogIngestStatus.PARTIAL.value
        assert run.hosts_scanned == 2
        assert run.hosts_failed == 1
        assert "10.0.0.2" in run.error_message


def test_cluster_id_resolves_real_cluster_for_source_selector(isolated_db, enabled, monkeypatch):
    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="CS-LAB", ceph_mon_nodes="10.0.0.1",
            ssh_user="root", ssh_key_path="/tmp/test-key",
        )
        session.add(cluster)
        session.commit()
        cluster_id = cluster.id

    seen = []

    class FakeSource:
        @staticmethod
        def fetch(host, daemon_type, window_start, window_end, cluster=None):
            seen.append(cluster.name if cluster else None)
            return LogSourceResult(records=[])

    monkeypatch.setattr(log_intel, "get_log_source", lambda _name: FakeSource)
    monkeypatch.setattr(
        log_intel, "configured_nodes",
        lambda cluster=None: [{"host": "10.0.0.1", "roles": ["mon"]}],
    )

    log_intel.scan_and_store(cluster_id)

    assert seen == ["CS-LAB"]


def test_repeat_scan_accumulates_instead_of_duplicating(isolated_db, enabled, monkeypatch):
    """Cửa sổ quét cố ý lớn hơn chu kỳ quét, nên hai tick liên tiếp chồng
    lấn nhau — phải cộng dồn vào cùng một pattern/ô giờ, không tạo bản sao."""
    _one_node(monkeypatch)
    _fake_source(monkeypatch, {
        ("10.0.0.1", "osd"): [_record("10.0.0.1", "osd.5 slow request osd_op initiated")],
    })

    log_intel.scan_and_store()
    log_intel.scan_and_store()

    with db_module.SessionLocal() as session:
        assert session.query(LogPattern).count() == 1
        assert session.query(LogPattern).one().total_count == 2
        assert session.query(LogPatternObservation).count() == 1
        assert session.query(LogPatternObservation).one().count == 2
        # Lần quét thứ hai không được tính lại là "pattern mới".
        assert [r.patterns_new for r in session.query(LogIngestRun).all()] == [1, 0]


def test_observations_are_split_per_host_and_hour(isolated_db, enabled, monkeypatch):
    monkeypatch.setattr(log_intel, "configured_nodes", lambda cluster=None: [
        {"host": "10.0.0.1", "roles": ["osd"]},
        {"host": "10.0.0.2", "roles": ["osd"]},
    ])
    message = "osd.5 slow request osd_op initiated"
    _fake_source(monkeypatch, {
        ("10.0.0.1", "osd"): [
            _record("10.0.0.1", message, ts=datetime(2026, 8, 18, 10, 5)),
            _record("10.0.0.1", message, ts=datetime(2026, 8, 18, 11, 5)),
        ],
        ("10.0.0.2", "osd"): [_record("10.0.0.2", message, ts=datetime(2026, 8, 18, 10, 30))],
    })

    log_intel.scan_and_store()

    with db_module.SessionLocal() as session:
        # 1 pattern, nhưng 3 ô: (h1,10h) (h1,11h) (h2,10h)
        assert session.query(LogPattern).count() == 1
        assert session.query(LogPatternObservation).count() == 3


def test_node_without_ceph_role_is_not_scanned(isolated_db, enabled, monkeypatch):
    """Chỉ quét đúng allowlist node đã cấu hình, đúng vai trò daemon —
    không tự dò, không tự đoán host (chống SSRF-qua-SSH)."""
    scanned = []

    class FakeSource:
        @staticmethod
        def fetch(host, daemon_type, window_start, window_end, cluster=None):
            scanned.append((host, daemon_type))
            return LogSourceResult(records=[])

    monkeypatch.setattr(log_intel, "get_log_source", lambda name: FakeSource)
    monkeypatch.setattr(log_intel, "configured_nodes", lambda cluster=None: [
        {"host": "10.0.0.1", "roles": ["mon", "osd"]},
        {"host": "10.0.0.9", "roles": ["something-else"]},
    ])

    log_intel.scan_and_store()

    assert sorted(scanned) == [("10.0.0.1", "mon"), ("10.0.0.1", "osd")]


def test_no_configured_nodes_is_recorded_as_failed(isolated_db, enabled, monkeypatch):
    monkeypatch.setattr(log_intel, "configured_nodes", lambda cluster=None: [])
    log_intel.scan_and_store()
    with db_module.SessionLocal() as session:
        run = session.query(LogIngestRun).one()
        assert run.status == LogIngestStatus.FAILED.value
        assert "Chưa cấu hình node" in run.error_message


def test_bad_source_name_fails_loudly_without_crashing(isolated_db, enabled, monkeypatch):
    monkeypatch.setattr(settings, "log_intel_source", "khong-ton-tai")
    log_intel.scan_and_store()
    with db_module.SessionLocal() as session:
        assert session.query(LogIngestRun).one().status == LogIngestStatus.FAILED.value


# --- prune_old_rows() ------------------------------------------------------


def test_prune_respects_two_different_retentions(isolated_db, monkeypatch):
    """Bảng observations phình theo KHỐI LƯỢNG log nên có hạn ngắn hơn hẳn —
    ràng buộc R1, và watcher/database_capacity_monitor.py là lý do."""
    monkeypatch.setattr(settings, "log_intel_observation_retention_days", 30)
    monkeypatch.setattr(settings, "log_intel_pattern_retention_days", 180)
    now = datetime(2026, 8, 18, 12, 0)

    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="default", ceph_mon_nodes="10.0.0.1",
            ssh_user="root", ssh_key_path="/root/.ssh/id_rsa",
        )
        session.add(cluster)
        session.flush()
        pattern = LogPattern(
            cluster_id=cluster.id, fingerprint="f1", template="t", daemon_type="osd",
            first_seen_at=now, last_seen_at=now, total_count=1,
        )
        session.add(pattern)
        session.flush()
        session.add_all([
            LogPatternObservation(
                pattern_id=pattern.id, bucket_hour=now - timedelta(days=40), host="h", count=1
            ),
            LogPatternObservation(
                pattern_id=pattern.id, bucket_hour=now - timedelta(days=10), host="h", count=1
            ),
        ])
        for age_days in (200, 10):
            session.add(LogIngestRun(
                cluster_id=cluster.id, source="ssh",
                window_start=now, window_end=now, status=LogIngestStatus.OK.value,
                created_at=now - timedelta(days=age_days),
            ))
        session.commit()

    observations_deleted, runs_deleted, _findings_deleted = log_intel.prune_old_rows(now=now)

    assert observations_deleted == 1 and runs_deleted == 1
    with db_module.SessionLocal() as session:
        assert session.query(LogPatternObservation).count() == 1
        assert session.query(LogIngestRun).count() == 1
        # Pattern (danh mục, nhỏ) không bị prune theo observation cutoff.
        assert session.query(LogPattern).count() == 1


# --- L1: nối triage vào lần quét -------------------------------------------


def test_scan_records_flagged_count_from_triage(isolated_db, enabled, monkeypatch):
    """L0 thu thập xong thì L1 triage chạy ngay trên chính dữ liệu vừa ghi,
    và số mẫu bị gắn cờ được lưu vào bảng provenance — để trả lời "cửa sổ đó
    có gì bất thường không" mà không phải tính lại."""
    _one_node(monkeypatch)
    _fake_source(monkeypatch, {
        ("10.0.0.1", "osd"): [
            _record(
                "10.0.0.1", "osd.5 heartbeat_check: no reply from 10.0.0.7:6802",
                ts=datetime.utcnow(),
            ),
        ],
    })

    log_intel.scan_and_store()

    with db_module.SessionLocal() as session:
        # Mẫu mới + khớp từ khoá hạt nhân -> bị gắn cờ.
        assert session.query(LogIngestRun).one().patterns_flagged == 1


def test_scan_records_zero_flagged_when_nothing_anomalous(isolated_db, enabled, monkeypatch):
    """0 ("đã triage, không có gì") phải phân biệt được với NULL ("lần quét
    chưa hề có tầng triage")."""
    _one_node(monkeypatch)
    _fake_source(monkeypatch, {
        ("10.0.0.1", "osd"): [
            _record("10.0.0.1", "osd.5 routine chatter", ts=datetime.utcnow(), severity=0),
        ],
    })

    log_intel.scan_and_store()

    with db_module.SessionLocal() as session:
        assert session.query(LogIngestRun).one().patterns_flagged == 0


def test_triage_failure_does_not_lose_collected_data(isolated_db, enabled, monkeypatch):
    """Triage là tầng PHÂN TÍCH — nó hỏng thì không được kéo theo kết quả
    THU THẬP đã hoàn tất (dữ liệu đã nằm trong DB rồi)."""
    _one_node(monkeypatch)
    _fake_source(monkeypatch, {
        ("10.0.0.1", "osd"): [
            _record("10.0.0.1", "osd.5 slow request osd_op initiated", ts=datetime.utcnow()),
        ],
    })
    monkeypatch.setattr(
        log_intel.log_triage, "triage_window",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    log_intel.scan_and_store()

    with db_module.SessionLocal() as session:
        run = session.query(LogIngestRun).one()
        assert run.status == LogIngestStatus.OK.value
        assert run.lines_scanned == 1
        assert run.patterns_flagged == 0  # triage hỏng, nhưng pattern vẫn được lưu
        assert session.query(LogPattern).count() == 1


def test_ai_circuit_breaker_skips_noisy_window(isolated_db, enabled, monkeypatch):
    """Backfill/onboarding không được biến thành một trận mưa AI alert."""
    _one_node(monkeypatch)
    _fake_source(monkeypatch, {
        ("10.0.0.1", "osd"): [
            _record("10.0.0.1", "osd.5 routine chatter", ts=datetime.utcnow(), severity=0),
        ],
    })
    noisy = [SimpleNamespace(reasons=[], template=f"noise-{i}") for i in range(3)]
    analyzed = []
    monkeypatch.setattr(log_intel.log_triage, "triage_window", lambda *a, **k: noisy)
    monkeypatch.setattr(log_intel.log_triage, "summarize", lambda _rows: "3 noisy patterns")
    monkeypatch.setattr(log_intel.log_analysis, "reconcile_overlapping_findings", lambda *_: 0)
    monkeypatch.setattr(
        log_intel.log_analysis, "analyze_window", lambda *a, **k: analyzed.append(1)
    )
    monkeypatch.setattr(settings, "log_intel_ai_enabled", True)
    monkeypatch.setattr(settings, "log_intel_ai_max_flagged_patterns", 2)

    log_intel.scan_and_store()

    assert analyzed == []
    with db_module.SessionLocal() as session:
        assert session.query(LogIngestRun).one().patterns_flagged == 3


def test_prune_never_orphans_a_finding_from_its_ingest_run(isolated_db, monkeypatch):
    """`log_findings.ingest_run_id` là FK NOT NULL vào `log_ingest_runs`.

    Bản đầu xoá thẳng log_ingest_runs theo cutoff nên trên Postgres (luôn
    cưỡng chế FK) nó ném IntegrityError — và vì lệnh xoá observations nằm
    CÙNG transaction, nó bị rollback theo, tức retention ngừng hoạt động
    HOÀN TOÀN, âm thầm. Đúng kiểu phình DB mà ràng buộc R1 sinh ra để tránh.

    sqlite mặc định TẮT cưỡng chế FK nên ca này không tự lộ ra — test dưới
    khẳng định bằng bất biến dữ liệu (không có finding nào trỏ vào một run
    đã biến mất) thay vì dựa vào việc DB có ném lỗi hay không.
    """
    monkeypatch.setattr(settings, "log_intel_pattern_retention_days", 180)
    monkeypatch.setattr(settings, "log_intel_finding_retention_days", 90)
    now = datetime(2026, 8, 19, 12, 0)

    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="default", ceph_mon_nodes="10.0.0.1",
            ssh_user="root", ssh_key_path="/root/.ssh/id_rsa",
        )
        session.add(cluster)
        session.flush()
        old_run = LogIngestRun(
            cluster_id=cluster.id, source="ssh",
            window_start=now, window_end=now, status=LogIngestStatus.OK.value,
            created_at=now - timedelta(days=200),   # quá hạn run
        )
        session.add(old_run)
        session.flush()
        session.add(LogFinding(
            cluster_id=cluster.id, ingest_run_id=old_run.id, verdict="FINDING",
            dedupe_key="k-open", status=LogFindingStatus.OPEN.value,
            created_at=now - timedelta(days=200),   # già, nhưng CÒN MỞ
        ))
        session.commit()

    log_intel.prune_old_rows(now=now)

    with db_module.SessionLocal() as session:
        finding = session.query(LogFinding).one()
        # Finding còn OPEN thì không bị xoá vì già — vẫn là việc chưa xong.
        assert finding.status == LogFindingStatus.OPEN.value
        # Và lần quét sinh ra nó phải còn nguyên: provenance không được gãy.
        assert session.query(LogIngestRun).filter(
            LogIngestRun.id == finding.ingest_run_id
        ).count() == 1


def test_prune_removes_resolved_findings_and_then_their_run(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "log_intel_pattern_retention_days", 180)
    monkeypatch.setattr(settings, "log_intel_finding_retention_days", 90)
    now = datetime(2026, 8, 19, 12, 0)

    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="default", ceph_mon_nodes="10.0.0.1",
            ssh_user="root", ssh_key_path="/root/.ssh/id_rsa",
        )
        session.add(cluster)
        session.flush()
        run = LogIngestRun(
            cluster_id=cluster.id, source="ssh",
            window_start=now, window_end=now, status=LogIngestStatus.OK.value,
            created_at=now - timedelta(days=200),
        )
        session.add(run)
        session.flush()
        session.add(LogFinding(
            cluster_id=cluster.id, ingest_run_id=run.id, verdict="FINDING",
            dedupe_key="k-done", status=LogFindingStatus.RESOLVED.value,
            created_at=now - timedelta(days=200),
        ))
        session.commit()

    _obs, runs_deleted, findings_deleted = log_intel.prune_old_rows(now=now)

    assert findings_deleted == 1
    assert runs_deleted == 1  # hết finding trỏ vào thì run mới được dọn
    with db_module.SessionLocal() as session:
        assert session.query(LogFinding).count() == 0
        assert session.query(LogIngestRun).count() == 0


def test_recent_resolved_finding_is_kept(isolated_db, monkeypatch):
    monkeypatch.setattr(settings, "log_intel_finding_retention_days", 90)
    now = datetime(2026, 8, 19, 12, 0)

    with db_module.SessionLocal() as session:
        cluster = Cluster(
            name="default", ceph_mon_nodes="10.0.0.1",
            ssh_user="root", ssh_key_path="/root/.ssh/id_rsa",
        )
        session.add(cluster)
        session.flush()
        run = LogIngestRun(
            cluster_id=cluster.id, source="ssh", window_start=now, window_end=now,
            status=LogIngestStatus.OK.value, created_at=now,
        )
        session.add(run)
        session.flush()
        session.add(LogFinding(
            cluster_id=cluster.id, ingest_run_id=run.id, verdict="FINDING",
            dedupe_key="k-new", status=LogFindingStatus.RESOLVED.value,
            created_at=now - timedelta(days=10),
        ))
        session.commit()

    _obs, _runs, findings_deleted = log_intel.prune_old_rows(now=now)

    assert findings_deleted == 0
