"""Log Intelligence L3 — cảnh báo Telegram + vòng đời OPEN/RESOLVED.

Nhóm test quan trọng nhất ở đây là **chống spam**: một vấn đề kéo dài vài
ngày sẽ được quét lại mỗi 15 phút, nên nếu dedupe hỏng thì người trực nhận
vài trăm tin nhắn cho cùng một chuyện và sẽ tắt kênh — lúc đó cả tính năng
thành vô dụng.
"""

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import settings
from shared import db as db_module
from shared import telegram_alerts
from shared.db import Base
from shared.models import (
    Action,
    ActionStatus,
    Cluster,
    Incident,
    IncidentStatus,
    LogFinding,
    LogFindingStatus,
    LogIngestRun,
    LogIngestStatus,
    LogPattern,
)
from watcher import log_analysis
from watcher.log_triage import TriageReason, TriageResult

WINDOW_START = datetime(2026, 8, 19, 10, 0)
WINDOW_END = datetime(2026, 8, 19, 11, 0)


def test_real_rgw_recovery_telegram_formatters_route_and_render(monkeypatch):
    delivered = []
    monkeypatch.setattr(telegram_alerts, "_send", lambda *args: delivered.append(args))
    monkeypatch.setattr(settings, "telegram_rgw_bot_token", "rgw-token")
    monkeypatch.setattr(settings, "telegram_rgw_chat_id", "rgw-chat")
    monkeypatch.setattr(settings, "telegram_rgw_enabled", True)
    telegram_alerts.send_log_finding_resolved_alert(
        "Vault recovered", daemon_types=["rgw"],
        verification_summary="VAULT_RECOVERY_VERIFIED",
    )
    telegram_alerts.send_log_finding_recovery_pending_alert(
        "Vault pending", "token lookup failed",
        ("ceph_health=HEALTH_OK", "vault_probe[x]=TOKEN_LOOKUP_HTTP=403"),
    )
    assert delivered[0][:3] == ("rgw-token", "rgw-chat", True)
    assert "RGW ĐÃ XÁC NHẬN PHỤC HỒI" in delivered[0][3]
    assert delivered[1][:3] == ("rgw-token", "rgw-chat", True)
    assert "RGW CHƯA XÁC NHẬN PHỤC HỒI" in delivered[1][3]


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
        session.flush()
        run = LogIngestRun(
            cluster_id=cluster.id, source="ssh",
            window_start=WINDOW_START, window_end=WINDOW_END,
            status=LogIngestStatus.OK.value,
        )
        session.add(run)
        session.flush()
        session.add(LogPattern(
            id="pat-1", cluster_id=cluster.id, fingerprint="fp-1",
            template="osd.<ID> heartbeat_check: no reply from <ADDR>",
            daemon_type="osd", severity=-1,
            first_seen_at=WINDOW_START, last_seen_at=WINDOW_END - timedelta(minutes=5),
            total_count=42,
        ))
        session.commit()
        yield (cluster.id, run.id)


@pytest.fixture()
def ai_on(monkeypatch):
    monkeypatch.setattr(settings, "log_intel_ai_enabled", True)
    monkeypatch.setattr(settings, "router_model", "test-model")
    monkeypatch.setattr(
        log_analysis, "configured_nodes",
        lambda cluster=None: [{"host": "10.0.0.1", "roles": ["osd"]}],
    )
    monkeypatch.setattr(log_analysis, "_cluster_context", lambda cluster_id: "Ceph 18.2.2")


@pytest.fixture()
def sent(monkeypatch):
    """Bắt mọi lời gọi gửi Telegram thay vì thật sự gửi."""
    alerts = {"new": [], "resolved": [], "recovery_pending": []}
    monkeypatch.setattr(
        log_analysis.telegram_alerts, "send_log_finding_alert",
        lambda *a, **k: alerts["new"].append((a, k)),
    )
    monkeypatch.setattr(
        log_analysis.telegram_alerts, "send_log_finding_resolved_alert",
        lambda *a, **k: alerts["resolved"].append((a, k)),
    )
    monkeypatch.setattr(
        log_analysis.telegram_alerts, "send_log_finding_recovery_pending_alert",
        lambda *a, **k: alerts["recovery_pending"].append((a, k)),
    )
    return alerts


def _triage_result(pattern_id="pat-1"):
    return TriageResult(
        pattern_id=pattern_id, fingerprint="fp-1",
        template="osd.<ID> heartbeat_check: no reply from <ADDR>",
        daemon_type="osd", severity=-1,
        sample_line="osd.5 heartbeat_check: no reply from 10.0.0.7",
        window_count=42, reasons=[TriageReason.SEVERE],
        baseline_mean=4.0, burst_ratio=10.5, hosts=["10.0.0.1"],
    )


def _response(**overrides):
    payload = {
        "verdict": "FINDING", "severity": "WARNING", "confidence": "MEDIUM",
        "title": "OSD mất heartbeat", "summary": "osd.5 không phản hồi.",
        "root_cause_hypothesis": "Nghi đứt mạng cluster.",
        "evidence_pattern_ids": ["pat-1"],
        "affected_hosts": ["10.0.0.1"], "affected_daemons": ["osd.5"],
        "recommended_action_id": None, "recommended_manual_steps": [],
    }
    payload.update(overrides)
    return payload


def test_default_key_finding_gets_deterministic_best_recommendation():
    payload = _response(
        title="RGW failed to decode default encryption key",
        summary="rgw crypt default encryption key is AES256",
    )
    validated = log_analysis._validate(
        payload, {"pat-1"}, {"10.0.0.1"}, LogIngestStatus.OK.value,
    )
    steps = validated["recommended_manual_steps"]
    assert steps[0].startswith("Khuyến nghị tốt nhất")
    assert "Vault SSE-S3" in steps[0]
    assert "khóa đoán" in steps[0]
    assert any("Lựa chọn thay thế" in step for step in steps)


def test_finding_without_ai_suggestion_gets_safe_fallback():
    validated = log_analysis._validate(
        _response(), {"pat-1"}, {"10.0.0.1"}, LogIngestStatus.OK.value,
    )
    assert validated["recommended_manual_steps"][0].startswith("Khuyến nghị tốt nhất")
    assert "AI không đưa ra gợi ý" in validated["validation_notes"]


def test_resolving_duplicate_does_not_cancel_action_while_same_finding_is_open(isolated_db):
    cluster_id, run_id = isolated_db
    dedupe_key = "same-rgw-problem"
    with db_module.SessionLocal() as session:
        session.add(LogFinding(
            cluster_id=cluster_id, ingest_run_id=run_id, verdict="FINDING",
            severity="WARNING", confidence="HIGH", dedupe_key=dedupe_key,
            status=LogFindingStatus.OPEN.value,
        ))
        incident = Incident(
            cluster_id=cluster_id, ceph_code=log_analysis.ceph_code_for(dedupe_key),
            status=IncidentStatus.PENDING_APPROVAL.value, detected_at=WINDOW_START,
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id, action_id="remove_invalid_rgw_default_key",
            classification="RISKY", status=ActionStatus.PENDING_APPROVAL.value,
        )
        session.add(action)
        session.commit()
        log_analysis._resolve_incident_for(session, dedupe_key)
        session.commit()
        assert session.get(Incident, incident.id).status == IncidentStatus.PENDING_APPROVAL.value
        assert session.get(Action, action.id).status == ActionStatus.PENDING_APPROVAL.value


def test_rgw_approval_is_not_cancelled_by_concurrent_stale_resolver(isolated_db):
    cluster_id, _run_id = isolated_db
    dedupe_key = "rgw-race"
    with db_module.SessionLocal() as session:
        incident = Incident(
            cluster_id=cluster_id, ceph_code=log_analysis.ceph_code_for(dedupe_key),
            status=IncidentStatus.PENDING_APPROVAL.value, detected_at=WINDOW_START,
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id, action_id="remove_invalid_rgw_default_key",
            classification="RISKY", status=ActionStatus.PENDING_APPROVAL.value,
        )
        session.add(action)
        session.commit()
        log_analysis._resolve_incident_for(session, dedupe_key)
        session.commit()
        assert session.get(Incident, incident.id).status == IncidentStatus.PENDING_APPROVAL.value
        assert session.get(Action, action.id).status == ActionStatus.PENDING_APPROVAL.value


def _stub_router(monkeypatch, payload):
    async def fake(user_content, allowed_action_ids):
        return payload
    monkeypatch.setattr(log_analysis, "_call_router", fake)


def _analyze(isolated_db, ingest_status=LogIngestStatus.OK.value):
    cluster_id, run_id = isolated_db
    return log_analysis.analyze_window(
        cluster_id, run_id, WINDOW_START, WINDOW_END,
        [_triage_result()], ingest_status,
    )


# --- Chống spam (nhóm quan trọng nhất) ------------------------------------


def test_repeat_scan_of_same_problem_alerts_only_once(isolated_db, ai_on, sent, monkeypatch):
    """Một vấn đề kéo dài được quét lại mỗi 15 phút. Nếu dedupe hỏng, người
    trực nhận vài trăm tin nhắn cho cùng một chuyện rồi tắt kênh."""
    _stub_router(monkeypatch, _response())

    first = _analyze(isolated_db)
    second = _analyze(isolated_db)
    third = _analyze(isolated_db)

    assert first == second == third            # cùng trỏ về một bản ghi
    assert len(sent["new"]) == 1               # chỉ báo một lần
    with db_module.SessionLocal() as session:
        assert session.query(LogFinding).count() == 1  # không đẻ hàng trùng


def test_different_problem_gets_its_own_alert(isolated_db, ai_on, sent, monkeypatch):
    cluster_id, run_id = isolated_db
    with db_module.SessionLocal() as session:
        session.add(LogPattern(
            id="pat-2", cluster_id=cluster_id, fingerprint="fp-2",
            template="mon.<ID> slow ops", daemon_type="mon", severity=-1,
                first_seen_at=WINDOW_START,
                last_seen_at=WINDOW_END - timedelta(minutes=5), total_count=5,
        ))
        session.commit()

    _stub_router(monkeypatch, _response())
    _analyze(isolated_db)

    _stub_router(monkeypatch, _response(evidence_pattern_ids=["pat-2"], title="MON chậm"))
    log_analysis.analyze_window(
        cluster_id, run_id, WINDOW_START, WINDOW_END,
        [_triage_result(pattern_id="pat-2")], LogIngestStatus.OK.value,
    )

    assert len(sent["new"]) == 2


def test_reopened_after_resolution_alerts_again(isolated_db, ai_on, sent, monkeypatch):
    """Sau khi đã đóng, vấn đề quay lại thì PHẢI báo lại — dedupe chỉ được
    chặn trong lúc còn đang mở, không phải chặn vĩnh viễn."""
    _stub_router(monkeypatch, _response())
    _analyze(isolated_db)
    assert len(sent["new"]) == 1

    with db_module.SessionLocal() as session:
        session.query(LogFinding).update({LogFinding.status: LogFindingStatus.RESOLVED.value})
        session.commit()

    _analyze(isolated_db)

    assert len(sent["new"]) == 2


# --- Ngưỡng báo ------------------------------------------------------------


@pytest.mark.parametrize("severity, should_alert", [
    ("CRITICAL", True),
    ("WARNING", True),
    ("INFO", False),
])
def test_only_warning_and_above_reach_the_phone(
    isolated_db, ai_on, sent, monkeypatch, severity, should_alert
):
    """INFO vẫn được LƯU để xem trên Dashboard, nhưng không làm rung điện
    thoại — báo mọi thứ sẽ nhanh chóng dạy người trực bỏ qua kênh."""
    _stub_router(monkeypatch, _response(severity=severity))
    _analyze(isolated_db)

    assert bool(sent["new"]) is should_alert
    with db_module.SessionLocal() as session:
        assert session.query(LogFinding).count() == 1  # luôn lưu


def test_insufficient_evidence_is_stored_but_not_alerted(isolated_db, ai_on, sent, monkeypatch):
    _stub_router(monkeypatch, _response(
        verdict="INSUFFICIENT_EVIDENCE", severity="INFO", evidence_pattern_ids=["pat-1"],
    ))
    _analyze(isolated_db)

    assert sent["new"] == []
    with db_module.SessionLocal() as session:
        assert session.query(LogFinding).count() == 1


# --- Nội dung cảnh báo -----------------------------------------------------


def test_alert_carries_original_evidence_not_just_ai_conclusion(
    isolated_db, ai_on, sent, monkeypatch
):
    """Người trực phải tự đánh giá được, không phải tin lời model."""
    _stub_router(monkeypatch, _response())
    _analyze(isolated_db)

    args, _ = sent["new"][0]
    evidence_templates = args[5]
    assert evidence_templates == ["osd.<ID> heartbeat_check: no reply from <ADDR>"]


def test_alert_surfaces_that_server_had_to_correct_the_model(
    isolated_db, ai_on, sent, monkeypatch
):
    """Nếu server đã phải sửa/hạ cấp câu trả lời của AI, người đọc cần biết
    ngay trên điện thoại chứ không phải mở Dashboard mới thấy."""
    _stub_router(monkeypatch, _response(recommended_action_id="delete_pool"))
    _analyze(isolated_db)

    args, _ = sent["new"][0]
    validation_notes = args[7]
    assert validation_notes and "delete_pool" in validation_notes


def test_telegram_failure_does_not_lose_the_finding(isolated_db, ai_on, monkeypatch):
    """Gửi cảnh báo là best-effort — lỗi Telegram không được xoá sổ kết quả
    phân tích đã hoàn tất."""
    _stub_router(monkeypatch, _response())
    monkeypatch.setattr(
        log_analysis.telegram_alerts, "send_log_finding_alert",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("telegram down")),
    )

    finding_id = _analyze(isolated_db)

    assert finding_id is not None
    with db_module.SessionLocal() as session:
        assert session.query(LogFinding).count() == 1


# --- Vòng đời OPEN -> RESOLVED --------------------------------------------


def _make_open_finding(cluster_id, run_id, pattern_ids=("pat-1",), title="OSD mất heartbeat"):
    with db_module.SessionLocal() as session:
        finding = LogFinding(
            cluster_id=cluster_id, ingest_run_id=run_id, verdict="FINDING",
            severity="WARNING", confidence="MEDIUM", title=title,
            evidence_pattern_ids_json=json.dumps(list(pattern_ids)),
            dedupe_key="k-" + title, status=LogFindingStatus.OPEN.value,
        )
        session.add(finding)
        session.commit()
        return finding.id


def test_finding_resolves_when_its_patterns_stop_appearing(isolated_db, sent):
    cluster_id, run_id = isolated_db
    finding_id = _make_open_finding(cluster_id, run_id)

    # Cửa sổ mới, sau lần cuối mẫu xuất hiện.
    later = WINDOW_START + timedelta(days=1)
    resolved = log_analysis.resolve_stale_findings(cluster_id, later)

    assert resolved == 1
    assert len(sent["resolved"]) == 1
    with db_module.SessionLocal() as session:
        assert session.get(LogFinding, finding_id).status == LogFindingStatus.RESOLVED.value


def test_finding_stays_open_while_its_patterns_still_occur(isolated_db, sent):
    cluster_id, run_id = isolated_db
    finding_id = _make_open_finding(cluster_id, run_id)

    resolved = log_analysis.resolve_stale_findings(cluster_id, WINDOW_START)

    assert resolved == 0
    assert sent["resolved"] == []
    with db_module.SessionLocal() as session:
        assert session.get(LogFinding, finding_id).status == LogFindingStatus.OPEN.value


def test_rgw_vault_finding_only_resolves_after_live_recovery_gate(
    isolated_db, sent, monkeypatch,
):
    from watcher import ceph_finding_verifier
    cluster_id, run_id = isolated_db
    with db_module.SessionLocal() as session:
        pattern = session.get(LogPattern, "pat-1")
        pattern.daemon_type = "rgw"
        pattern.template = "failed to retrieve actual key from Vault"
        session.commit()
    finding_id = _make_open_finding(cluster_id, run_id, title="RGW Vault key lookup failed")
    blocked = ceph_finding_verifier.VerificationResult(
        "VAULT_RECOVERY_UNVERIFIED", "token lookup returned 403", (), False,
    )
    monkeypatch.setattr(ceph_finding_verifier, "verify_vault_recovery", lambda *args: blocked)
    later = WINDOW_START + timedelta(days=1)
    assert log_analysis.resolve_stale_findings(cluster_id, later) == 0
    with db_module.SessionLocal() as session:
        assert session.get(LogFinding, finding_id).status == LogFindingStatus.OPEN.value
    assert sent["resolved"] == []
    assert len(sent["recovery_pending"]) == 1

    # Operator requested a progress reminder every 10 minutes while still open.
    assert log_analysis.resolve_stale_findings(cluster_id, later + timedelta(minutes=15)) == 0
    assert len(sent["recovery_pending"]) == 2

    verified = ceph_finding_verifier.VerificationResult(
        "VAULT_RECOVERY_VERIFIED", "Vault token lookup thành công", (), True,
    )
    monkeypatch.setattr(ceph_finding_verifier, "verify_vault_recovery", lambda *args: verified)
    assert log_analysis.resolve_stale_findings(cluster_id, later) == 1
    assert sent["resolved"][0][1]["daemon_types"] == ["rgw"]
    assert "VAULT_RECOVERY_VERIFIED" in sent["resolved"][0][1]["verification_summary"]


def test_functional_rgw_recovery_can_resolve_before_log_window_ages_out(
    isolated_db, sent, monkeypatch,
):
    from watcher import ceph_finding_verifier
    cluster_id, run_id = isolated_db
    with db_module.SessionLocal() as session:
        pattern = session.get(LogPattern, "pat-1")
        pattern.daemon_type = "rgw"
        pattern.template = "failed to retrieve actual key from Vault"
        session.commit()
    finding_id = _make_open_finding(cluster_id, run_id, title="RGW Vault recovered")
    verified = ceph_finding_verifier.VerificationResult(
        "VAULT_RECOVERY_VERIFIED", "PUT SSE-S3 200 after latest error", (), True,
    )
    monkeypatch.setattr(ceph_finding_verifier, "verify_vault_recovery", lambda *args: verified)

    # Pattern is still inside this Loki window, but a later functional PUT
    # is stronger recovery evidence and must close immediately.
    assert log_analysis.resolve_stale_findings(cluster_id, WINDOW_START) == 1
    with db_module.SessionLocal() as session:
        assert session.get(LogFinding, finding_id).status == LogFindingStatus.RESOLVED.value
    assert len(sent["resolved"]) == 1


def test_partially_active_evidence_keeps_finding_open(isolated_db, sent):
    """Chỉ đóng khi MỌI mẫu đã ngừng — một mẫu còn chạy nghĩa là hiện tượng
    mới giảm đi chứ chưa hết."""
    cluster_id, run_id = isolated_db
    with db_module.SessionLocal() as session:
        session.add(LogPattern(
            id="pat-cu", cluster_id=cluster_id, fingerprint="fp-cu",
            template="cũ", daemon_type="osd", severity=-1,
            first_seen_at=WINDOW_START - timedelta(days=5),
            last_seen_at=WINDOW_START - timedelta(days=5), total_count=1,
        ))
        session.commit()
    _make_open_finding(cluster_id, run_id, pattern_ids=("pat-1", "pat-cu"))

    assert log_analysis.resolve_stale_findings(cluster_id, WINDOW_START) == 0


def test_finding_without_evidence_never_auto_resolves(isolated_db, sent):
    """INSUFFICIENT_EVIDENCE không trích dẫn mẫu nào — không có gì để đối
    chiếu, nên để operator tự xử lý thay vì âm thầm đóng hộ."""
    cluster_id, run_id = isolated_db
    _make_open_finding(cluster_id, run_id, pattern_ids=())

    later = WINDOW_START + timedelta(days=30)
    assert log_analysis.resolve_stale_findings(cluster_id, later) == 0


def test_reconcile_merges_findings_when_evidence_set_drifted(isolated_db):
    cluster_id, run_id = isolated_db
    canonical_id = _make_open_finding(
        cluster_id, run_id, pattern_ids=("p1", "p2", "p3", "p4", "p5"), title="first"
    )
    duplicate_id = _make_open_finding(
        cluster_id, run_id, pattern_ids=("p1", "p2", "p3", "p4", "p6", "p7"), title="second"
    )

    assert log_analysis.reconcile_overlapping_findings(cluster_id) == 1

    with db_module.SessionLocal() as session:
        assert session.get(LogFinding, canonical_id).status == LogFindingStatus.OPEN.value
        assert session.get(LogFinding, duplicate_id).status == LogFindingStatus.RESOLVED.value


def test_reconcile_does_not_merge_findings_with_only_generic_overlap(isolated_db):
    cluster_id, run_id = isolated_db
    first_id = _make_open_finding(
        cluster_id, run_id, pattern_ids=("generic", "a", "b"), title="first"
    )
    second_id = _make_open_finding(
        cluster_id, run_id, pattern_ids=("generic", "x", "y"), title="second"
    )

    assert log_analysis.reconcile_overlapping_findings(cluster_id) == 0
    with db_module.SessionLocal() as session:
        assert session.get(LogFinding, first_id).status == LogFindingStatus.OPEN.value
        assert session.get(LogFinding, second_id).status == LogFindingStatus.OPEN.value


def test_reconcile_merges_same_server_semantics_without_evidence_overlap(isolated_db):
    cluster_id, run_id = isolated_db
    first_id = _make_open_finding(cluster_id, run_id, pattern_ids=("old-heartbeat",), title="first")
    second_id = _make_open_finding(cluster_id, run_id, pattern_ids=("new-heartbeat",), title="second")
    with db_module.SessionLocal() as session:
        for finding_id in (first_id, second_id):
            finding = session.get(LogFinding, finding_id)
            finding.fault_family = "network_heartbeat"
            finding.semantic_entities_json = json.dumps(["host:10.0.0.1", "daemon:osd.5"])
        session.commit()

    assert log_analysis.reconcile_overlapping_findings(cluster_id) == 1
    with db_module.SessionLocal() as session:
        assert session.get(LogFinding, first_id).status == LogFindingStatus.OPEN.value
        assert session.get(LogFinding, second_id).status == LogFindingStatus.RESOLVED.value


def test_semantic_reconcile_does_not_merge_different_entities(isolated_db):
    cluster_id, run_id = isolated_db
    first_id = _make_open_finding(cluster_id, run_id, pattern_ids=("p-a",), title="first")
    second_id = _make_open_finding(cluster_id, run_id, pattern_ids=("p-b",), title="second")
    with db_module.SessionLocal() as session:
        first = session.get(LogFinding, first_id)
        second = session.get(LogFinding, second_id)
        first.fault_family = second.fault_family = "disk_io"
        first.semantic_entities_json = json.dumps(["host:node-a", "daemon:osd.1"])
        second.semantic_entities_json = json.dumps(["host:node-b", "daemon:osd.2"])
        session.commit()

    assert log_analysis.reconcile_overlapping_findings(cluster_id) == 0


def test_already_resolved_finding_is_not_re_alerted(isolated_db, sent):
    cluster_id, run_id = isolated_db
    _make_open_finding(cluster_id, run_id)
    later = WINDOW_START + timedelta(days=1)

    log_analysis.resolve_stale_findings(cluster_id, later)
    log_analysis.resolve_stale_findings(cluster_id, later)

    assert len(sent["resolved"]) == 1


def test_acknowledged_finding_still_auto_resolves(isolated_db, sent):
    cluster_id, run_id = isolated_db
    finding_id = _make_open_finding(cluster_id, run_id)
    with db_module.SessionLocal() as session:
        session.get(LogFinding, finding_id).status = LogFindingStatus.ACKNOWLEDGED.value
        session.commit()

    later = WINDOW_START + timedelta(days=1)
    assert log_analysis.resolve_stale_findings(cluster_id, later) == 1


def test_resolution_works_without_ai_enabled(isolated_db, sent, monkeypatch):
    """Vòng đời chỉ đọc LogPattern.last_seen_at mà L0 vẫn cập nhật đều —
    không được kẹt vì router AI chết hay AI bị tắt."""
    monkeypatch.setattr(settings, "log_intel_ai_enabled", False)
    cluster_id, run_id = isolated_db
    _make_open_finding(cluster_id, run_id)

    later = WINDOW_START + timedelta(days=1)
    assert log_analysis.resolve_stale_findings(cluster_id, later) == 1
