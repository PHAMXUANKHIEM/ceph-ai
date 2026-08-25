"""Log Intelligence L4 — trang Dashboard + đề xuất hành động advisory."""

import json
from datetime import datetime, timedelta

import pytest

from shared import db, log_learning
from shared.models import (
    Action,
    ActionStatus,
    Incident,
    IncidentStatus,
    LogFinding,
    LogFindingStatus,
    LogLearningAudit,
    LogLearningSample,
    LogIngestRun,
    LogIngestStatus,
    LogPattern,
    LogPatternTriageLabel,
)
from watcher import log_analysis

NOW = datetime(2026, 8, 19, 10, 0)


def _login(client, username="admin", password="admin"):
    client.post("/login", data={"username": username, "password": password})


def _cluster_id(session):
    from shared.clusters import ensure_default_cluster

    return ensure_default_cluster(session).id


@pytest.fixture()
def seeded(dashboard_client):
    """Một lần quét + một mẫu log + một phát hiện đang mở.

    Phụ thuộc `dashboard_client` KHÔNG phải để gọi HTTP mà để lấy DB
    sqlite in-memory đã được monkeypatch — thiếu nó thì `db.SessionLocal()`
    trỏ thẳng vào Postgres dev thật trong .env."""
    with db.SessionLocal() as session:
        cluster_id = _cluster_id(session)
        run = LogIngestRun(
            cluster_id=cluster_id, source="ssh",
            window_start=NOW, window_end=NOW + timedelta(hours=1),
            status=LogIngestStatus.PARTIAL.value,
            hosts_scanned=3, hosts_failed=1, lines_scanned=1200,
            patterns_seen=8, patterns_new=2, patterns_flagged=1,
            error_message="10.0.0.9: unreachable",
        )
        session.add(run)
        session.flush()
        pattern = LogPattern(
            cluster_id=cluster_id, fingerprint="fp-1",
            template="osd.<ID> heartbeat_check: no reply from <ADDR>",
            daemon_type="osd", severity=-1,
            first_seen_at=NOW, last_seen_at=NOW, total_count=42,
        )
        session.add(pattern)
        session.flush()
        finding = LogFinding(
            cluster_id=cluster_id, ingest_run_id=run.id,
            verdict="FINDING", severity="WARNING", confidence="MEDIUM",
            title="OSD mất heartbeat", summary="osd.5 không phản hồi.",
            root_cause_hypothesis="Nghi đứt mạng cluster.",
            evidence_pattern_ids_json=json.dumps([pattern.id]),
            affected_hosts_json=json.dumps(["10.0.0.1"]),
            affected_daemons_json=json.dumps(["osd.5"]),
            recommended_manual_steps_json=json.dumps([]),
            dedupe_key="dk-abc123456789", status=LogFindingStatus.OPEN.value,
            model_name="test-model", prompt_version="v1",
        )
        session.add(finding)
        session.commit()
        return {"finding_id": finding.id, "pattern_id": pattern.id, "cluster_id": cluster_id}


# --- Trang ----------------------------------------------------------------


def test_unauthenticated_redirects_to_login(dashboard_client):
    response = dashboard_client.get("/log-intelligence", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_empty_state_renders(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/log-intelligence")
    assert response.status_code == 200
    assert "Chưa có lần quét nào" in response.text


def test_page_shows_collection_completeness_first(dashboard_client, seeded):
    """PARTIAL phải hiện rõ: mọi kết luận bên dưới chỉ đáng tin bằng đúng độ
    đầy đủ của dữ liệu sinh ra nó."""
    _login(dashboard_client)
    response = dashboard_client.get("/log-intelligence")
    assert "PARTIAL" in response.text
    assert "2/3" in response.text          # 3 node, 1 hụt
    assert "10.0.0.9" in response.text     # lý do hụt


def test_finding_is_shown_with_original_evidence(dashboard_client, seeded):
    """Kết luận AI luôn phải đi kèm bằng chứng gốc — người đọc tự đánh giá,
    không phải tin lời model."""
    _login(dashboard_client)
    response = dashboard_client.get("/log-intelligence")
    assert "OSD mất heartbeat" in response.text
    assert "heartbeat_check: no reply" in response.text     # mẫu log thật
    assert "Nghi đứt mạng cluster" in response.text
    assert "test-model" in response.text                    # truy vết model


def test_page_states_findings_are_hypotheses_not_measurements(dashboard_client, seeded):
    _login(dashboard_client)
    response = dashboard_client.get("/log-intelligence")
    assert "giả thuyết" in response.text.lower()


def test_rgw_finding_is_highlighted_in_dedicated_ai_alert_section(dashboard_client, seeded):
    with db.SessionLocal() as session:
        finding = session.get(LogFinding, seeded["finding_id"])
        finding.title = "RGW trả nhiều HTTP 503"
        finding.affected_daemons_json = json.dumps(["rgw"])
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/log-intelligence")

    assert "Cảnh báo RGW — AI phân tích" in response.text
    assert "RGW trả nhiều HTTP 503" in response.text


def test_page_shows_server_correlated_health_incident(dashboard_client, seeded):
    with db.SessionLocal() as session:
        incident = Incident(
            cluster_id=seeded["cluster_id"], ceph_code="OSD_DOWN",
            status=IncidentStatus.NEW.value, detected_at=NOW,
        )
        session.add(incident)
        session.flush()
        finding = session.get(LogFinding, seeded["finding_id"])
        finding.fault_family = "network_heartbeat"
        finding.correlated_incident_id = incident.id
        finding.correlation_reason = "server:network_heartbeat:ceph_code=OSD_DOWN"
        finding.correlated_at = NOW
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/log-intelligence")
    assert "Incident tương quan" in response.text
    assert "OSD_DOWN" in response.text
    assert "do server đối chiếu" in response.text


def test_validation_notes_are_surfaced(dashboard_client, seeded):
    """Nếu server đã phải sửa câu trả lời của model, người đọc phải thấy."""
    with db.SessionLocal() as session:
        finding = session.get(LogFinding, seeded["finding_id"])
        finding.validation_notes = "action_id 'delete_pool' không có trong allowlist — bỏ"
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/log-intelligence")
    assert "delete_pool" in response.text


# --- Ghi nhận / gắn nhãn ---------------------------------------------------


def test_acknowledge_moves_open_to_acknowledged(dashboard_client, seeded):
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/log-intelligence/findings/{seeded['finding_id']}/acknowledge",
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db.SessionLocal() as session:
        assert session.get(LogFinding, seeded["finding_id"]).status == (
            LogFindingStatus.ACKNOWLEDGED.value
        )


def test_acknowledge_never_marks_resolved(dashboard_client, seeded):
    """Một phát hiện chỉ được coi là hết khi mẫu log của nó thật sự ngừng —
    đo bằng dữ liệu, không phải bằng một cú bấm nút."""
    _login(dashboard_client)
    dashboard_client.post(f"/log-intelligence/findings/{seeded['finding_id']}/acknowledge")
    with db.SessionLocal() as session:
        assert session.get(LogFinding, seeded["finding_id"]).status != (
            LogFindingStatus.RESOLVED.value
        )


def test_label_pattern_benign(dashboard_client, seeded):
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/log-intelligence/patterns/{seeded['pattern_id']}/label",
        data={"label": "BENIGN"}, follow_redirects=False,
    )
    assert response.status_code == 303
    with db.SessionLocal() as session:
        assert session.get(LogPattern, seeded["pattern_id"]).triage_label == (
            LogPatternTriageLabel.BENIGN.value
        )


def test_label_rejects_unknown_value(dashboard_client, seeded):
    _login(dashboard_client)
    dashboard_client.post(
        f"/log-intelligence/patterns/{seeded['pattern_id']}/label",
        data={"label": "KHONG-HOP-LE"},
    )
    with db.SessionLocal() as session:
        assert session.get(LogPattern, seeded["pattern_id"]).triage_label == (
            LogPatternTriageLabel.UNKNOWN.value
        )


def test_acknowledge_unknown_finding_is_404(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post("/log-intelligence/findings/khong-co/acknowledge")
    assert response.status_code == 404


def test_learning_dashboard_shows_state_and_audit_only_gate(dashboard_client, seeded):
    with db.SessionLocal() as session:
        finding = session.get(LogFinding, seeded["finding_id"])
        sample = log_learning.record_finding_sample(session, finding, now=NOW)
        log_learning.recompute_fault_stats(session, now=NOW)
        sample_id = sample.id

    _login(dashboard_client)
    response = dashboard_client.get("/log-intelligence")
    assert response.status_code == 200
    assert "AI Learning từ log daemon" in response.text
    assert "INSUFFICIENT_EVIDENCE" in response.text
    assert "audit-only" in response.text
    assert sample_id in response.text


def test_admin_can_record_audited_negative_learning_verdict(dashboard_client, seeded):
    with db.SessionLocal() as session:
        finding = session.get(LogFinding, seeded["finding_id"])
        sample_id = log_learning.record_finding_sample(session, finding, now=NOW).id
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.post(
        f"/log-intelligence/learning/{sample_id}/verdict",
        data={"verdict": "FALSE_POSITIVE", "note": "Log kiểm thử có chủ đích"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db.SessionLocal() as session:
        sample = session.get(LogLearningSample, sample_id)
        # PARTIAL coverage remains the stronger fail-closed gate.
        assert sample.operator_verdict == "FALSE_POSITIVE"
        assert sample.eligible_for_learning is False
        audit = session.query(LogLearningAudit).one()
        assert audit.actor == "admin"
        assert audit.event_type == "OPERATOR_VERDICT_UPDATED"


# --- Đề xuất hành động (advisory) -----------------------------------------


def _payload(**overrides):
    payload = {
        "title": "OSD mất heartbeat", "severity": "WARNING", "confidence": "MEDIUM",
        "summary": "osd.5 không phản hồi.", "root_cause": "Nghi đứt mạng.",
        "recommended_action_id": None, "validation_notes": None,
    }
    payload.update(overrides)
    return payload


def test_proposed_action_always_waits_for_approval(seeded):
    """Ràng buộc R5 của plan: không có gì tự chạy ra cụm. Không đường nào
    trong codebase tự phê duyệt một hàng PENDING_APPROVAL."""
    log_analysis._maybe_propose_action(
        seeded["cluster_id"], _payload(), "dk-xyz", ["osd.<ID> heartbeat_check"]
    )
    with db.SessionLocal() as session:
        action = session.query(Action).one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        incident = session.get(Incident, action.incident_id)
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert incident.ceph_code.startswith(log_analysis.LOG_ANOMALY_PREFIX)


def test_proposal_falls_back_to_investigate_manually(seeded):
    log_analysis._maybe_propose_action(seeded["cluster_id"], _payload(), "dk-xyz", [])
    with db.SessionLocal() as session:
        assert session.query(Action).one().action_id == "investigate_manually"


def test_proposal_uses_recommended_action_when_present(seeded):
    log_analysis._maybe_propose_action(
        seeded["cluster_id"], _payload(recommended_action_id="restart_osd_daemon"), "dk-xyz", []
    )
    with db.SessionLocal() as session:
        assert session.query(Action).one().action_id == "restart_osd_daemon"


def test_rationale_marks_it_as_an_ai_hypothesis(seeded):
    """Người duyệt phải biết mình đang duyệt dựa trên suy luận, không phải
    một phép đo — khác hẳn OSD_LATENCY_HIGH vốn từ `ceph osd perf`."""
    log_analysis._maybe_propose_action(
        seeded["cluster_id"], _payload(), "dk-xyz", ["osd.<ID> heartbeat_check: no reply"]
    )
    with db.SessionLocal() as session:
        rationale = session.query(Action).one().rationale
    assert "Giả thuyết từ AI" in rationale
    assert "MEDIUM" in rationale
    assert "osd.<ID> heartbeat_check: no reply" in rationale  # bằng chứng gốc


def test_info_severity_creates_no_proposal(seeded):
    """Một phát hiện INFO không đáng chiếm một dòng trong hàng chờ duyệt."""
    log_analysis._maybe_propose_action(
        seeded["cluster_id"], _payload(severity="INFO"), "dk-xyz", []
    )
    with db.SessionLocal() as session:
        assert session.query(Action).count() == 0


def test_repeat_proposal_does_not_duplicate_incident(seeded):
    for _ in range(3):
        log_analysis._maybe_propose_action(seeded["cluster_id"], _payload(), "dk-xyz", [])
    with db.SessionLocal() as session:
        assert session.query(Incident).filter(
            Incident.ceph_code.like(f"{log_analysis.LOG_ANOMALY_PREFIX}%")
        ).count() == 1


def test_resolving_finding_also_closes_its_pending_incident(seeded):
    """Vấn đề tự hết thì hàng chờ duyệt cũng phải tự sạch."""
    cluster_id = seeded["cluster_id"]
    dedupe_key = "dk-abc123456789"  # trùng với finding trong fixture
    log_analysis._maybe_propose_action(cluster_id, _payload(), dedupe_key, [])

    later = NOW + timedelta(days=1)
    log_analysis.resolve_stale_findings(cluster_id, later)

    with db.SessionLocal() as session:
        incident = session.query(Incident).filter(
            Incident.ceph_code == log_analysis.ceph_code_for(dedupe_key)
        ).one()
        assert incident.status == IncidentStatus.RESOLVED.value


def test_log_anomaly_never_flips_the_cluster_health_badge(seeded):
    """Badge hứa phản ánh "tình trạng CỦA CLUSTER (HEALTH_WARN/ERR thật)".
    Để một suy luận của model bôi đỏ nó sẽ bào mòn niềm tin vào badge —
    phát hiện vẫn hiện đầy đủ trong danh sách Incident, chỉ không đổi màu."""
    from dashboard.routes.incidents import compute_cluster_status

    incident = Incident(
        cluster_id=seeded["cluster_id"],
        ceph_code=log_analysis.ceph_code_for("dk-xyz"),
        status=IncidentStatus.PENDING_APPROVAL.value,
        severity="HEALTH_ERR",
        detected_at=NOW,
    )

    assert compute_cluster_status([incident], heartbeat_stale=False) == "OK"
