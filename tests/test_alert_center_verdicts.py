from datetime import datetime, timedelta

from shared import db as db_module
from shared.models import Action, Incident, RemediationCase


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _incident_with_case(incident_id, code, *, verdict=None, detected=None, fingerprint="a"):
    with db_module.SessionLocal() as session:
        incident = Incident(id=incident_id, ceph_code=code, status="RESOLVED",
                            detected_at=detected or datetime.utcnow())
        session.add(incident)
        session.flush()
        action = Action(incident_id=incident.id, action_id="restart_osd_daemon",
                        classification="RISKY", status="REJECTED")
        session.add(action)
        session.flush()
        case = RemediationCase(
            incident_id=incident.id, action_id=action.id, fault_family=code,
            evidence_fingerprint=fingerprint * 64, prompt_version="v1", classification="RISKY",
            autonomy_decision="PENDING_APPROVAL", outcome="PROPOSED", operator_verdict=verdict,
        )
        session.add(case)
        session.commit()
        return case.id


def test_alert_center_shows_verdict_column_and_filters_unlabelled(dashboard_client):
    pending = _incident_with_case("inc-pending", "OSD_DOWN", fingerprint="a")
    _incident_with_case("inc-labelled", "MON_DOWN", verdict="FALSE_POSITIVE", fingerprint="b")
    with db_module.SessionLocal() as session:
        session.add(Incident(id="inc-nocase", ceph_code="PG_DEGRADED", status="RESOLVED",
                             detected_at=datetime.utcnow()))
        session.commit()
    _login(dashboard_client)

    page = dashboard_client.get("/alerts")
    assert page.status_code == 200
    assert "<th>Verdict</th>" in page.text
    assert f"/incidents/inc-pending/cases/{pending}/verdict" in page.text
    assert "FALSE_POSITIVE" in page.text
    assert "Không có case" in page.text

    unlabelled = dashboard_client.get("/alerts?verdict=unlabelled")
    assert "<code>OSD_DOWN</code>" in unlabelled.text
    assert "<code>MON_DOWN</code>" not in unlabelled.text
    assert "<code>PG_DEGRADED</code>" not in unlabelled.text

    labelled = dashboard_client.get("/alerts?verdict=labelled")
    assert "<code>MON_DOWN</code>" in labelled.text and "<code>OSD_DOWN</code>" not in labelled.text


def test_group_uses_the_latest_case_across_repeated_incidents(dashboard_client):
    now = datetime.utcnow()
    _incident_with_case("inc-old", "OSD_DOWN", verdict="CORRECT", detected=now - timedelta(hours=2), fingerprint="c")
    newest = _incident_with_case("inc-new", "OSD_DOWN", detected=now, fingerprint="d")
    _login(dashboard_client)
    page = dashboard_client.get("/alerts?verdict=unlabelled")
    assert f"/cases/{newest}/verdict" in page.text


def test_quick_verdict_returns_to_the_alert_center(dashboard_client):
    case_id = _incident_with_case("inc-quick", "OSD_DOWN", fingerprint="e")
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/incidents/inc-quick/cases/{case_id}/verdict",
        data={"verdict": "CORRECT", "next": "/alerts?page=1&verdict=unlabelled"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/alerts?page=1&verdict=unlabelled"
    with db_module.SessionLocal() as session:
        assert session.get(RemediationCase, case_id).operator_verdict == "CORRECT"


def test_verdict_return_target_cannot_leave_the_alert_center(dashboard_client):
    case_id = _incident_with_case("inc-redirect", "OSD_DOWN", fingerprint="f")
    _login(dashboard_client)
    for target in ("https://evil.example/alerts", "//evil.example", "/alerts?x=//evil", "/settings"):
        response = dashboard_client.post(
            f"/incidents/inc-redirect/cases/{case_id}/verdict",
            data={"verdict": "INCONCLUSIVE", "next": target}, follow_redirects=False,
        )
        assert response.headers["location"] == "/incidents/inc-redirect/timeline"


def test_negative_quick_verdict_still_requires_a_note(dashboard_client):
    case_id = _incident_with_case("inc-note", "OSD_DOWN", fingerprint="g")
    _login(dashboard_client)
    response = dashboard_client.post(
        f"/incidents/inc-note/cases/{case_id}/verdict",
        data={"verdict": "FALSE_POSITIVE", "note": "", "next": "/alerts"},
    )
    assert response.status_code == 400
