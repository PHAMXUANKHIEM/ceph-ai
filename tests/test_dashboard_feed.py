from datetime import datetime, timedelta

from sqlalchemy.exc import OperationalError
import dashboard.routes.incidents as incidents_route

from shared import db as db_module
from shared.models import Action, AuditEntry, Incident, RemediationCase, WatcherHeartbeat
from watcher.incident_grouping import assign_incident_group


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def test_index_shows_incident_from_db(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(
            Incident(ceph_code="OSD_DOWN", status="NEW", detected_at=datetime.utcnow())
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "OSD_DOWN" in response.text
    assert "/timeline" in response.text


def test_incident_timeline_page_and_postmortem_generation(dashboard_client, monkeypatch):
    with db_module.SessionLocal() as session:
        session.add(Incident(
            id="timeline-inc", ceph_code="OSD_DOWN", status="RESOLVED",
            detected_at=datetime.utcnow(), signal_evidence_json='{"osd_id": 3}',
        ))
        session.commit()
    called = []
    async def fake_generate(incident_id):
        called.append(incident_id)
        return {}
    monkeypatch.setattr(incidents_route.incident_postmortem, "generate", fake_generate)
    _login(dashboard_client)
    page = dashboard_client.get("/incidents/timeline-inc/timeline")
    assert page.status_code == 200
    assert "Incident Timeline" in page.text
    assert "incident:timeline-inc:detected" in page.text
    response = dashboard_client.post("/incidents/timeline-inc/postmortem", follow_redirects=False)
    assert response.status_code == 303
    assert called == ["timeline-inc"]


def test_incident_timeline_shows_group_context(dashboard_client):
    detected_at = datetime.utcnow()
    root_detected_at = detected_at - timedelta(minutes=5)
    with db_module.SessionLocal() as session:
        root = Incident(
            id="group-root",
            ceph_code="OSD_DOWN",
            status="RESOLVED",
            detected_at=root_detected_at,
            diagnosis_text="Network heartbeat interrupted.",
        )
        child = Incident(
            id="group-child",
            ceph_code="OSD_DOWN",
            status="NEW",
            detected_at=detected_at,
        )
        session.add_all([root, child])
        session.flush()
        assign_incident_group(session, root)
        assign_incident_group(session, child)
        session.commit()

    _login(dashboard_client)
    page = dashboard_client.get("/incidents/group-child/timeline")

    assert page.status_code == 200
    assert "Incident Group" in page.text
    assert "group-root" in page.text
    assert "Network heartbeat interrupted." in page.text
    assert "/incidents/group-root/timeline" in page.text
    assert root_detected_at.strftime("%d/%m/%Y") in page.text
    assert root_detected_at.isoformat() not in page.text


def test_operator_can_set_case_verdict_from_incident_timeline(dashboard_client):
    with db_module.SessionLocal() as session:
        incident = Incident(
            id="verdict-inc", ceph_code="OSD_DOWN", status="RESOLVED",
            detected_at=datetime.utcnow(),
        )
        session.add(incident); session.flush()
        action = Action(
            incident_id=incident.id, action_id="restart_osd_daemon",
            classification="RISKY", status="EXECUTED",
        )
        session.add(action); session.flush()
        case = RemediationCase(
            incident_id=incident.id, action_id=action.id, fault_family="OSD_DOWN",
            evidence_fingerprint="a" * 64, prompt_version="v1", classification="RISKY",
            autonomy_decision="PENDING_APPROVAL", playbook_version="v1", outcome="VERIFIED_SUCCESS",
            shadow_decision="HOLD", shadow_reason="verified samples 0/20",
            shadow_trust_score=0.0, shadow_sample_count=0,
        )
        session.add(case); session.commit(); case_id = case.id

    _login(dashboard_client)
    page = dashboard_client.get("/incidents/verdict-inc/timeline")
    assert page.status_code == 200
    assert "Remediation Case Memory" in page.text
    assert "Chẩn đoán/xử lý đúng" in page.text
    assert "Shadow Autopilot" in page.text
    assert "MISSED_OPPORTUNITY" in page.text

    response = dashboard_client.post(
        f"/incidents/verdict-inc/cases/{case_id}/verdict",
        data={"verdict": "CORRECT", "note": "OSD ổn định sau kiểm chứng."},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with db_module.SessionLocal() as session:
        case = session.get(RemediationCase, case_id)
        assert case.operator_verdict == "CORRECT"
        assert case.operator_note == "OSD ổn định sau kiểm chứng."
        assert case.operator_verdict_by == "admin"
        assert case.operator_verdict_at is not None
        entry = session.query(AuditEntry).filter_by(action_id=case.action_id).one()
        assert entry.event_type == "remediation_case_verdict_updated"
        assert entry.actor == "admin"


def test_case_verdict_rejects_invalid_value(dashboard_client):
    with db_module.SessionLocal() as session:
        incident = Incident(
            id="bad-verdict-inc", ceph_code="OSD_DOWN", status="RESOLVED",
            detected_at=datetime.utcnow(),
        )
        session.add(incident); session.flush()
        action = Action(
            incident_id=incident.id, action_id="restart_osd_daemon",
            classification="RISKY", status="EXECUTED",
        )
        session.add(action); session.flush()
        case = RemediationCase(
            incident_id=incident.id, action_id=action.id, fault_family="OSD_DOWN",
            evidence_fingerprint="b" * 64, prompt_version="v1", classification="RISKY",
            autonomy_decision="PENDING_APPROVAL", playbook_version="v1", outcome="VERIFIED_SUCCESS",
        )
        session.add(case); session.commit(); case_id = case.id
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/incidents/bad-verdict-inc/cases/{case_id}/verdict",
        data={"verdict": "MAKE_AUTONOMOUS", "note": "no"},
    )
    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.get(RemediationCase, case_id).operator_verdict is None


def test_negative_case_verdict_requires_meaningful_note(dashboard_client):
    with db_module.SessionLocal() as session:
        incident = Incident(id="negative-note-inc", ceph_code="OSD_DOWN", status="RESOLVED", detected_at=datetime.utcnow())
        session.add(incident); session.flush()
        action = Action(incident_id=incident.id, action_id="restart_osd_daemon", classification="RISKY", status="EXECUTED")
        session.add(action); session.flush()
        case = RemediationCase(incident_id=incident.id, action_id=action.id, fault_family="OSD_DOWN",
            evidence_fingerprint="c" * 64, prompt_version="v1", classification="RISKY",
            autonomy_decision="PENDING_APPROVAL", outcome="VERIFIED_SUCCESS")
        session.add(case); session.commit(); case_id = case.id
    _login(dashboard_client)

    response = dashboard_client.post(
        f"/incidents/negative-note-inc/cases/{case_id}/verdict",
        data={"verdict": "UNSAFE", "note": "no"},
    )

    assert response.status_code == 400
    with db_module.SessionLocal() as session:
        assert session.get(RemediationCase, case_id).operator_verdict is None


def test_index_shows_diagnosis_text_as_the_error_reason(dashboard_client):
    # 2026-07-23: the Incident Feed was simplified to just "Mã lỗi" + "Lý do
    # lỗi" (Chat-with-AI now covers everything the removed status/severity/
    # approve-action columns used to show) — diagnosis_text is the "reason".
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                ceph_code="OSD_DOWN",
                status="NEW",
                detected_at=datetime.utcnow(),
                severity="HEALTH_ERR",
                diagnosis_text="OSD.3 bị crash do hết dung lượng đĩa.",
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "OSD.3 bị crash do hết dung lượng đĩa." in response.text


def test_index_shows_placeholder_when_incident_has_no_diagnosis_yet(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(Incident(ceph_code="OSD_DOWN", status="NEW", detected_at=datetime.utcnow()))
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Chưa có chẩn đoán." in response.text


def test_index_shows_empty_state_when_no_incidents(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Chưa có Incident" in response.text


def test_index_handles_db_error_gracefully(dashboard_client, monkeypatch):
    _login(dashboard_client)

    def _broken_session_local():
        raise OperationalError("SELECT 1", {}, Exception("db unreachable"))

    monkeypatch.setattr(db_module, "SessionLocal", _broken_session_local)
    response = dashboard_client.get("/")

    assert response.status_code == 503


def test_index_handles_non_db_error_gracefully_not_raw_500(dashboard_client, monkeypatch):
    # Review Story 5.2: compute_cluster_status()/is_heartbeat_stale() run
    # inside the same try as the DB fetch — a bug there (anything that
    # isn't a SQLAlchemyError) must still return a clean error response,
    # not leak a raw unhandled-exception 500/stack trace to the browser.
    import dashboard.routes.incidents as incidents_route

    def _broken_is_heartbeat_stale(_heartbeat):
        raise RuntimeError("boom")

    monkeypatch.setattr(incidents_route, "is_heartbeat_stale", _broken_is_heartbeat_stale)

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 500
    assert "boom" not in response.text


# --- Story 5.2: heartbeat display on the main page -------------------------


def test_index_shows_connection_lost_warning_when_never_polled(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Mất kết nối cụm Ceph" in response.text


def test_index_shows_connection_lost_warning_when_last_poll_failed(dashboard_client, default_cluster_id):
    with db_module.SessionLocal() as session:
        session.add(
            WatcherHeartbeat(
                cluster_id=default_cluster_id,
                success=False,
                mon_node=None,
                error_message="All MON nodes failed: timed out",
                polled_at=datetime.utcnow(),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Mất kết nối cụm Ceph" in response.text
    assert "All MON nodes failed: timed out" in response.text


def test_index_shows_connection_lost_warning_when_poll_too_old(dashboard_client, monkeypatch, default_cluster_id):
    from config.settings import settings

    monkeypatch.setattr(settings, "watcher_poll_interval_seconds", 15)
    with db_module.SessionLocal() as session:
        session.add(
            WatcherHeartbeat(
                cluster_id=default_cluster_id,
                success=True,
                mon_node="10.20.1.150",
                error_message=None,
                polled_at=datetime.utcnow() - timedelta(seconds=15 * 3 + 1),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Mất kết nối cụm Ceph" in response.text


def test_index_shows_healthy_connection_details_when_recent_and_successful(dashboard_client, monkeypatch, default_cluster_id):
    from config.settings import settings

    monkeypatch.setattr(settings, "watcher_poll_interval_seconds", 15)
    with db_module.SessionLocal() as session:
        session.add(
            WatcherHeartbeat(
                cluster_id=default_cluster_id,
                success=True,
                mon_node="10.20.1.150",
                error_message=None,
                polled_at=datetime.utcnow() - timedelta(seconds=2),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Mất kết nối cụm Ceph" not in response.text
    assert "10.20.1.150" in response.text


# --- Audit Trail on the Dashboard page (see test_dashboard_audit.py for
# filtering behavior) ---------------------------------------------------


def test_index_shows_audit_trail_section_with_entries(dashboard_client):
    with db_module.SessionLocal() as session:
        session.add(
            Incident(id="inc-1", ceph_code="OSD_DOWN", status="NEW", detected_at=datetime.utcnow())
        )
        session.add(
            AuditEntry(
                incident_id="inc-1",
                action_id=None,
                event_type="INCIDENT_DETECTED",
                actor="system",
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert '<h2>Audit Trail</h2>' in response.text
    assert 'id="audit-feed"' in response.text
    assert "INCIDENT_DETECTED" in response.text


def test_index_shows_empty_state_when_no_audit_entries(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Chưa có hoạt động nào." in response.text


def test_index_shows_backup_alert_banner_when_recent_failure_exists(dashboard_client):
    from shared.models import BackupJob

    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id="run-1",
                pool="vms",
                image="web01",
                job_type="full",
                status="FAILED",
                error_message="disk full trên đích",
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Backup thất bại" in response.text
    assert "vms/web01" in response.text
    assert "disk full trên đích" in response.text


def test_index_does_not_show_backup_alert_banner_when_no_recent_failure(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Backup thất bại" not in response.text


def test_index_does_not_show_backup_alert_banner_for_old_failure(dashboard_client):
    from shared.models import BackupJob

    with db_module.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id="run-old",
                pool="vms",
                image="web01",
                job_type="full",
                status="FAILED",
                error_message="old failure, already superseded",
                created_at=datetime.utcnow() - timedelta(hours=25),
            )
        )
        session.commit()

    _login(dashboard_client)
    response = dashboard_client.get("/")

    assert response.status_code == 200
    assert "Backup thất bại" not in response.text
