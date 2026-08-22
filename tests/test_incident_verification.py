"""Xác minh sau khắc phục (watcher/verify.py).

Điều đang được bảo vệ ở đây: "lệnh SSH chạy xong exit 0" KHÔNG được tự động
trở thành "sự cố đã hết". Trước 2026-08-20 nó chính là như vậy, nên một
lệnh chạy trót lọt mà chẳng sửa được gì vẫn khép Incident lại và Dashboard
vẫn báo đã xong.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.verify as verify
from shared import db as db_module, remediation_cases
from shared.db import Base
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    AuditEntry,
    Incident,
    IncidentStatus,
    RemediationCase,
)


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


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    """Mọi test ở đây chặn Telegram thật và ghi lại lời gọi."""
    sent = {"verified": [], "exhausted": []}
    monkeypatch.setattr(
        verify.telegram_alerts,
        "send_incident_verified_alert",
        lambda ceph_code, **kw: sent["verified"].append((ceph_code, kw)),
    )
    monkeypatch.setattr(
        verify.telegram_alerts,
        "send_incident_verify_exhausted_alert",
        lambda ceph_code, attempts, **kw: sent["exhausted"].append((ceph_code, attempts)),
    )
    return sent


@pytest.fixture(autouse=True)
def no_rabbitmq(monkeypatch):
    """Bắt envelope chẩn đoán lại thay vì thật sự publish."""
    published = []

    async def fake_publish(envelope):
        published.append(envelope)

    monkeypatch.setattr(verify.publisher, "publish_incident", fake_publish)
    return published


def _seed(ceph_code="OSD_DOWN", *, verify_after_minutes_ago=10, attempts=0, command="systemctl restart ceph-osd@3"):
    """Một Incident đang VERIFYING kèm Action đã EXECUTED — đúng trạng thái
    mà worker để lại sau khi lệnh chạy xong exit 0."""
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code=ceph_code,
            status=IncidentStatus.VERIFYING.value,
            detected_at=datetime.utcnow(),
            log_excerpt="log",
            verify_after=datetime.utcnow() - timedelta(minutes=verify_after_minutes_ago),
            verify_attempts=attempts,
        )
        session.add(incident)
        session.flush()
        session.add(
            Action(
                incident_id=incident.id,
                action_id="restart_osd_daemon",
                classification=ActionClassification.RISKY.value,
                status=ActionStatus.EXECUTED.value,
                proposed_command=command,
                target_nodes='["10.20.1.50"]',
                executed_at=datetime.utcnow(),
            )
        )
        session.commit()
        return incident.id


def _attach_case(
    incident_id: str, *, malformed_snapshot: bool = False, legacy_without_snapshot: bool = False,
):
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        action = session.query(Action).filter_by(incident_id=incident_id).one()
        case = remediation_cases.create_for_action(
            session, incident=incident, action=action,
            redacted_envelope={"nodes": ["10.20.1.50"], "cluster_snapshot": {}},
            diagnosis="test", model_provider="test",
        )
        case.outcome = "EXECUTED_PENDING_VERIFY"
        if malformed_snapshot:
            case.preflight_snapshot_json = '{"registry":{"action_id":"different_action"}}'
        elif legacy_without_snapshot:
            case.preflight_snapshot_json = None
        session.commit()
        return case.id


# --- lỗi đã hết thật ---------------------------------------------------------


def test_code_gone_from_health_resolves_and_reports_ok_on_telegram(isolated_db, no_telegram):
    incident_id = _seed()

    counts = verify.verify_pending_incidents({"MON_CLOCK_SKEW"})  # OSD_DOWN đã biến mất

    assert counts["verified"] == 1
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        assert incident.status == IncidentStatus.RESOLVED.value
        assert incident.verify_after is None
    assert len(no_telegram["verified"]) == 1
    assert no_telegram["verified"][0][0] == "OSD_DOWN"
    # Lệnh đã chạy phải có trong thông báo — operator cần biết cái gì đã cứu cụm.
    assert "ceph-osd@3" in no_telegram["verified"][0][1]["attempted_command"]


def test_verified_resolution_is_written_to_the_audit_trail(isolated_db):
    incident_id = _seed()

    verify.verify_pending_incidents(set())

    with db_module.SessionLocal() as session:
        events = [e.event_type for e in session.query(AuditEntry).filter_by(incident_id=incident_id)]
    assert "incident_fix_verified" in events


def test_case_postcheck_strategy_resolves_from_frozen_contract(isolated_db):
    incident_id = _seed()
    case_id = _attach_case(incident_id)

    counts = verify.verify_pending_incidents(set(), health={"checks": {}})

    assert counts["verified"] == 1
    with db_module.SessionLocal() as session:
        assert session.get(Incident, incident_id).status == IncidentStatus.RESOLVED.value
        assert session.get(RemediationCase, case_id).outcome == "VERIFIED_SUCCESS"


def test_corrupt_case_contract_makes_postcheck_inconclusive_without_retry(isolated_db):
    incident_id = _seed()
    case_id = _attach_case(incident_id, malformed_snapshot=True)

    counts = verify.verify_pending_incidents(set(), health={"checks": {}})

    assert counts["exhausted"] == 1
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        case = session.get(RemediationCase, case_id)
        events = [e.event_type for e in session.query(AuditEntry).filter_by(incident_id=incident_id)]
        assert incident.status == IncidentStatus.FAILED.value
        assert case.outcome == "INCONCLUSIVE"
        assert "playbook_postcheck_inconclusive" in events


def test_legacy_case_without_contract_snapshot_keeps_compatibility_verification(isolated_db):
    incident_id = _seed()
    case_id = _attach_case(incident_id, legacy_without_snapshot=True)

    counts = verify.verify_pending_incidents(set(), health={"checks": {}})

    assert counts["verified"] == 1
    with db_module.SessionLocal() as session:
        assert session.get(Incident, incident_id).status == IncidentStatus.RESOLVED.value
        assert session.get(RemediationCase, case_id).outcome == "VERIFIED_SUCCESS"


# --- lỗi vẫn còn -------------------------------------------------------------


def test_code_still_present_sends_it_back_to_ai_with_what_was_already_tried(
    isolated_db, no_telegram, no_rabbitmq
):
    incident_id = _seed()

    counts = verify.verify_pending_incidents({"OSD_DOWN"})  # vẫn còn nguyên

    assert counts["retried"] == 1
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        assert incident.status == IncidentStatus.DIAGNOSING.value
        assert incident.verify_attempts == 1
    # Chưa báo gì lên Telegram — chưa có kết luận nào để báo.
    assert no_telegram["verified"] == [] and no_telegram["exhausted"] == []
    # Envelope chẩn đoán lại phải mang theo lệnh đã thử, nếu không vòng hai
    # sẽ đề xuất lại đúng cái lệnh vừa thất bại.
    assert len(no_rabbitmq) == 1
    attempts = no_rabbitmq[0]["previous_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["command"] == "systemctl restart ceph-osd@3"
    assert no_rabbitmq[0]["incident_id"] == incident_id


def test_gives_up_after_max_attempts_and_says_so_on_telegram(isolated_db, no_telegram, monkeypatch):
    monkeypatch.setattr(verify.settings, "incident_verify_max_attempts", 2, raising=False)
    incident_id = _seed(attempts=1)  # đã dùng 1 vòng

    counts = verify.verify_pending_incidents({"OSD_DOWN"})

    assert counts["exhausted"] == 1
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        assert incident.status == IncidentStatus.FAILED.value
    assert no_telegram["exhausted"] == [("OSD_DOWN", 2)]


# --- những thứ TUYỆT ĐỐI không được xảy ra -----------------------------------


def test_does_not_verify_before_the_wait_has_elapsed(isolated_db, no_telegram):
    """Kiểm ngay sau khi lệnh chạy sẽ ra 'chưa hết' một cách giả tạo với mọi
    lỗi cần thời gian phục hồi (PG backfill, OSD vào lại quorum)."""
    incident_id = _seed(verify_after_minutes_ago=-10)  # verify_after còn ở tương lai

    counts = verify.verify_pending_incidents(set())

    assert counts == {"verified": 0, "retried": 0, "exhausted": 0}
    with db_module.SessionLocal() as session:
        assert session.get(Incident, incident_id).status == IncidentStatus.VERIFYING.value
    assert no_telegram["verified"] == []


def test_monitor_owned_ceph_codes_are_left_alone(isolated_db, no_telegram):
    """CRUSH_SKEW_PG không bao giờ xuất hiện trong `ceph health detail`, nên
    'không thấy nó' KHÔNG có nghĩa là đã hết — module monitor sở hữu nó mới
    biết. Đối chiếu ở đây sẽ báo khắc phục cho một vấn đề còn nguyên."""
    incident_id = _seed(ceph_code="CRUSH_SKEW_PG:1")

    counts = verify.verify_pending_incidents(set())

    assert counts["verified"] == 0
    with db_module.SessionLocal() as session:
        assert session.get(Incident, incident_id).status == IncidentStatus.VERIFYING.value
    assert no_telegram["verified"] == []


def test_incident_not_in_verifying_state_is_untouched(isolated_db, no_telegram):
    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code="OSD_DOWN",
            status=IncidentStatus.PENDING_APPROVAL.value,
            detected_at=datetime.utcnow(),
            verify_after=datetime.utcnow() - timedelta(minutes=10),
        )
        session.add(incident)
        session.commit()
        incident_id = incident.id

    verify.verify_pending_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, incident_id).status == IncidentStatus.PENDING_APPROVAL.value


# --- phía Worker: exit 0 không còn tự động là RESOLVED -----------------------


def test_successful_command_now_lands_in_verifying_not_resolved(isolated_db, monkeypatch):
    """Đây là chỗ lỗi cũ nằm: `succeeded=True` từng ghi thẳng RESOLVED."""
    from worker.llm import router_client

    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code="OSD_DOWN",
            status=IncidentStatus.EXECUTING.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="restart_osd_daemon",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.APPROVED.value,
        )
        session.add(action)
        session.commit()
        incident_id, action_pk = incident.id, action.id

    router_client._record_approved_execution_result(action_pk, "systemctl restart ceph-osd@3", True)

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, incident_id)
        assert incident.status == IncidentStatus.VERIFYING.value
        assert incident.verify_after is not None
        assert session.get(Action, action_pk).status == ActionStatus.EXECUTED.value


def test_failed_command_still_goes_straight_to_failed(isolated_db):
    from worker.llm import router_client

    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code="OSD_DOWN",
            status=IncidentStatus.EXECUTING.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="restart_osd_daemon",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.APPROVED.value,
        )
        session.add(action)
        session.commit()
        incident_id, action_pk = incident.id, action.id

    router_client._record_approved_execution_result(action_pk, "cmd", False)

    with db_module.SessionLocal() as session:
        assert session.get(Incident, incident_id).status == IncidentStatus.FAILED.value


def test_monitor_owned_code_keeps_resolving_immediately(isolated_db):
    """CRUSH_SKEW_PG không thể xác minh qua `ceph health detail` — giữ nguyên
    hành vi cũ cho nhóm này thay vì treo chúng ở VERIFYING mãi mãi."""
    from worker.llm import router_client

    with db_module.SessionLocal() as session:
        incident = Incident(
            ceph_code="CRUSH_SKEW_PG:1",
            status=IncidentStatus.EXECUTING.value,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="investigate_manually",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.APPROVED.value,
        )
        session.add(action)
        session.commit()
        incident_id, action_pk = incident.id, action.id

    router_client._record_approved_execution_result(action_pk, None, True)

    with db_module.SessionLocal() as session:
        assert session.get(Incident, incident_id).status == IncidentStatus.RESOLVED.value
