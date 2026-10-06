""""Luồng AI" tab of the Stream page (shared/ai_flow.py)."""

import json
import os
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import ai_flow
from shared.db import Base
from shared.models import Action, AuditEntry, Incident, IncidentEvidence

NOW = datetime(2026, 10, 6, 12, 0, 0)


def _factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _incident(session, code, *, diagnosis=None, status="PENDING_APPROVAL", age=timedelta(hours=1)):
    incident = Incident(ceph_code=code, status=status, diagnosis_text=diagnosis,
                        detected_at=NOW - age, created_at=NOW - age)
    session.add(incident)
    session.flush()
    return incident


def _node(flow, node_id):
    return next(node for node in flow["nodes"] if node["id"] == node_id)


def test_every_stage_is_drawn_and_connected(tmp_path):
    flow = ai_flow.build(now=NOW, session_factory=_factory(), failure_lab_dir=tmp_path)

    ids = {node["id"] for node in flow["nodes"]}
    assert {"detect_health", "incidents", "evidence", "diagnosis", "policy", "approval", "autopilot", "execution",
            "verification", "case_memory", "trust", "online_learning", "failure_lab"} <= ids
    assert all({edge["from"], edge["to"]} <= ids for edge in flow["edges"])
    assert _node(flow, "failure_lab")["status"] == "unknown"


def test_counts_sources_and_flags_missing_evidence_and_failed_execution(tmp_path):
    factory = _factory()
    with factory() as session:
        log = _incident(session, "LOG_ANOMALY:abc", diagnosis="mẫu log lạ")
        _incident(session, "OSD_LATENCY_HIGH:3")
        _incident(session, "OSD_DOWN", diagnosis="osd.1 down")
        _incident(session, "OSD_DOWN", status="RESOLVED", age=timedelta(days=3))  # outside the window
        session.add_all([
            Action(incident_id=log.id, action_id="restart_osd_daemon", classification="RISKY", status="FAILED",
                   target_nodes="[]", created_at=NOW - timedelta(hours=1)),
            AuditEntry(incident_id=log.id, event_type="proposal_blocked_by_preflight", actor="system",
                       created_at=NOW - timedelta(hours=1)),
        ])
        session.commit()

    flow = ai_flow.build(now=NOW, session_factory=factory, failure_lab_dir=tmp_path)

    assert flow["summary"]["incidents"] == 3 and flow["summary"]["diagnosed"] == 2
    assert _node(flow, "detect_log")["subtitle"].startswith("1 incident")
    assert _node(flow, "detect_telemetry")["subtitle"].startswith("1 incident")
    assert _node(flow, "detect_health")["subtitle"].startswith("1 incident")
    assert _node(flow, "evidence")["status"] == "warn"  # incidents but no evidence collected
    assert _node(flow, "execution")["status"] == "error"  # every execution failed
    assert "Preflight chặn: 1" in _node(flow, "policy")["facts"]


def test_evidence_collected_clears_the_warning(tmp_path):
    factory = _factory()
    with factory() as session:
        incident = _incident(session, "OSD_DOWN")
        session.add(IncidentEvidence(incident_id=incident.id, runbook="OSD_DOWN", collector_id="ceph_osd_tree",
                                     target="mon", status="ok", command="ceph osd tree", output_redacted="",
                                     truncated=False, duration_ms=10, created_at=NOW - timedelta(minutes=5)))
        session.commit()

    flow = ai_flow.build(now=NOW, session_factory=factory, failure_lab_dir=tmp_path)

    assert _node(flow, "evidence")["status"] == "ok"


def test_failure_lab_shows_the_newest_campaign(tmp_path):
    (tmp_path / "old.json").write_text(json.dumps({"campaign_id": "old", "passed_count": 1, "run_count": 9}))
    newest = tmp_path / "new.json"
    newest.write_text(json.dumps({"campaign_id": "new", "passed_count": 8, "run_count": 8, "skipped": []}))
    later = newest.stat().st_mtime + 10
    os.utime(newest, (later, later))

    flow = ai_flow.build(now=NOW, session_factory=_factory(), failure_lab_dir=tmp_path)

    lab = _node(flow, "failure_lab")
    assert lab["subtitle"] == "lượt gần nhất 8/8 đạt" and lab["status"] == "ok"


def test_api_and_page_are_admin_only(dashboard_client, monkeypatch):
    from dashboard.routes import installation_stream

    monkeypatch.setattr(installation_stream.ai_flow, "build", lambda: {"schema": ai_flow.SCHEMA, "nodes": []})
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    assert dashboard_client.get("/api/stream/ai-flow").json()["schema"] == ai_flow.SCHEMA
    assert 'id="ai-flow-bootstrap"' in dashboard_client.get("/stream").text

    monkeypatch.setattr(installation_stream.auth, "is_admin_user", lambda _user: False)
    assert dashboard_client.get("/api/stream/ai-flow").status_code == 403


def test_a_broken_count_does_not_take_the_stream_page_down(dashboard_client, monkeypatch):
    from dashboard.routes import installation_stream

    def broken():
        raise RuntimeError("db down")

    monkeypatch.setattr(installation_stream.ai_flow, "build", broken)
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})

    page = dashboard_client.get("/stream")
    assert page.status_code == 200 and '<script id="ai-flow-bootstrap" type="application/json">null</script>' in page.text
    assert dashboard_client.get("/api/stream/ai-flow").status_code == 503
