from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import autonomy_kpi
from shared.db import Base
from shared.models import Action, Incident, RemediationCase

NOW = datetime(2026, 9, 28, 12, 0)


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _incident(session, code, *, created, status="RESOLVED", resolved=None, cluster=None):
    row = Incident(ceph_code=code, status=status, detected_at=created, created_at=created,
                   updated_at=resolved or created, cluster_id=cluster)
    session.add(row)
    session.flush()
    return row


def test_fault_family_collapses_entity_suffixes():
    assert autonomy_kpi.fault_family("NODE_UNREACHABLE:10.0.0.1") == "NODE_UNREACHABLE"
    assert autonomy_kpi.fault_family("OSD_LATENCY_HIGH:3") == "OSD_LATENCY_HIGH"
    assert autonomy_kpi.fault_family("BLUESTORE_SLOW_OP_ALERT") == "BLUESTORE_SLOW_OP_ALERT"
    assert autonomy_kpi.fault_family(None) == "UNKNOWN"


def test_reopen_counts_only_the_same_code_within_the_window():
    session = _session()
    t0 = NOW - timedelta(days=1)
    _incident(session, "NODE_UNREACHABLE:h1", created=t0, resolved=t0 + timedelta(minutes=5))
    _incident(session, "NODE_UNREACHABLE:h1", created=t0 + timedelta(minutes=20))          # reopened
    _incident(session, "NODE_UNREACHABLE:h2", created=t0 + timedelta(minutes=21))          # other host
    _incident(session, "NODE_UNREACHABLE:h1", created=t0 + timedelta(hours=3))             # outside window
    report = autonomy_kpi.collect(session, days=30, now=NOW)
    assert report["incidents"]["total"] == 4
    assert report["incidents"]["reopened_within_30m"] == 1
    assert report["incidents"]["by_family"] == {"NODE_UNREACHABLE": 4}


def test_placeholder_rate_verdicts_and_cluster_filter():
    session = _session()
    t0 = NOW - timedelta(days=2)
    first = _incident(session, "OSD_DOWN", created=t0, cluster="c1")
    second = _incident(session, "OSD_DOWN", created=t0, cluster="c2")
    actions = []
    for incident, action_id in ((first, "investigate_manually"), (first, "restart_osd_daemon"),
                                (second, "investigate_manually")):
        action = Action(incident_id=incident.id, action_id=action_id, classification="RISKY",
                        status="REJECTED", created_at=t0)
        session.add(action)
        session.flush()
        actions.append(action)
    session.add(RemediationCase(
        incident_id=first.id, action_id=actions[1].id, cluster_id="c1", fault_family="OSD_DOWN",
        evidence_fingerprint="f" * 64, prompt_version="v1", classification="RISKY",
        autonomy_decision="APPROVAL_REQUIRED", outcome="VERIFIED_SUCCESS", created_at=t0,
        operator_verdict="CORRECT", operator_verdict_at=t0 + timedelta(hours=4),
    ))
    session.flush()

    overall = autonomy_kpi.collect(session, days=30, now=NOW)
    assert overall["actions"]["investigate_manually"] == 2
    assert overall["actions"]["investigate_manually_rate"] == round(2 / 3, 4)
    assert overall["verdicts"]["labelled"] == 1
    assert overall["verdicts"]["median_hours_to_verdict"] == 4.0
    assert overall["verdicts"]["verified_success"] == 1

    c2 = autonomy_kpi.collect(session, days=30, cluster_id="c2", now=NOW)
    assert c2["incidents"]["total"] == 1
    assert c2["actions"]["total"] == 1
    assert c2["verdicts"]["cases"] == 0


def test_empty_window_reports_nulls_not_zero_rates():
    report = autonomy_kpi.collect(_session(), days=7, now=NOW)
    assert report["incidents"]["reopen_rate"] is None
    assert report["actions"]["investigate_manually_rate"] is None
    assert report["verdicts"]["median_hours_to_verdict"] is None


def test_default_cluster_includes_legacy_unscoped_rows():
    session = _session()
    t0 = NOW - timedelta(days=1)
    _incident(session, "OSD_DOWN", created=t0, cluster=None)
    _incident(session, "OSD_DOWN", created=t0, cluster="default")
    _incident(session, "OSD_DOWN", created=t0, cluster="other")
    assert autonomy_kpi.collect(session, cluster_id="default", include_unscoped=True, now=NOW)["incidents"]["total"] == 2
    assert autonomy_kpi.collect(session, cluster_id="default", now=NOW)["incidents"]["total"] == 1


def test_api_requires_admin_and_returns_the_selected_cluster_report(dashboard_client):
    anonymous = dashboard_client.get("/api/ai-learning/autonomy-kpi", follow_redirects=False)
    assert anonymous.status_code in {303, 401}
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/api/ai-learning/autonomy-kpi?days=7")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "ceph-ai.autonomy-kpi.v1"
    assert body["window_days"] == 7
    assert dashboard_client.get("/api/ai-learning/autonomy-kpi?days=0").status_code == 400


def test_learning_page_loads_the_autonomy_kpi_card():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    page = (root / "dashboard/templates/ai_learning.html").read_text(encoding="utf-8")
    assert 'id="autonomy-kpi"' in page
    assert "/static/autonomy_kpi.js" in page
    assert (root / "dashboard/static/autonomy_kpi.js").is_file()
