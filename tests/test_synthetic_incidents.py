import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.models import Action, Cluster, Incident, IncidentStatus
from shared.synthetic_incidents import (
    SyntheticInjectionError,
    cleanup,
    create,
    is_synthetic_evidence,
    load_scenario_catalog,
    score_replay,
    score_replay_control,
    score_replay_report,
)


def _session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _cluster(environment="lab"):
    return Cluster(
        name="lab", ceph_mon_nodes="10.0.0.1,10.0.0.2", ssh_user="root",
        ssh_key_path="/root/.ssh/id_ed25519", autonomy_environment=environment,
    )


def test_create_is_lab_only_and_marks_envelope():
    session = _session()
    cluster = _cluster()
    session.add(cluster)
    session.flush()
    incident, envelope = create(session, cluster=cluster, scenario_id="osd_down", actor="tester")
    session.commit()

    assert incident.status == IncidentStatus.NEW.value
    assert envelope["synthetic_injection"] is True
    assert envelope["synthetic_mode"] == "shadow-only"
    assert is_synthetic_evidence(incident.signal_evidence_json)
    assert json.loads(incident.signal_evidence_json)["scenario"] == "osd_down"
    assert envelope["failure_lab_provenance"] == "synthetic_fixture"
    assert "fixture=synthetic" in envelope["log_excerpt"]
    assert envelope["cluster_snapshot"]["failure_lab_metrics"]["osd_up"] == 2

    production = _cluster("production")
    production.is_active = True
    with pytest.raises(SyntheticInjectionError, match="environment=lab"):
        create(session, cluster=production, scenario_id="osd_down", actor="tester")



def test_failure_lab_replay_catalog_is_validated_and_not_mislabelled_as_golden():
    scenarios = load_scenario_catalog()
    assert {
        "osd_down", "slow_heartbeat", "node_unreachable", "osd_latency_high",
        "crush_skew", "large_omap", "mon_clock_skew", "osd_nearfull",
    }.issubset(scenarios)
    assert all(not scenario.verified_real_incident for scenario in scenarios.values())



def test_catalog_validator_rejects_unsupported_schema(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text("schema_version: 99\nscenarios: []\n", encoding="utf-8")
    with pytest.raises(SyntheticInjectionError, match="schema_version must be 1"):
        load_scenario_catalog(path)


def test_catalog_validator_prevents_synthetic_fixture_becoming_verified_by_flag(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text(
        """schema_version: 1
scenarios:
  - id: fake
    mode: replay
    ceph_code: FAKE
    severity: HEALTH_WARN
    message: fixture
    replay:
      log_excerpt: fixture
      metrics: {}
    expect:
      diagnosis_markers: [fixture]
      acceptable_action_ids: [investigate_manually]
      detection_timeout_seconds: 30
    provenance:
      source: synthetic_fixture
      verified_real_incident: true
      golden_set_approved: false
""",
        encoding="utf-8",
    )
    with pytest.raises(SyntheticInjectionError, match="must agree"):
        load_scenario_catalog(path)

def test_replay_score_marks_only_complete_correct_observations_as_passed():
    result = score_replay("osd_down", {
        "detected_ceph_code": "OSD_DOWN",
        "detection_seconds": 12,
        "diagnosis_text": "The root cause is osd.0 down.",
        "action_id": "investigate_manually",
        "recovered": True,
        "unexpected_health_codes": [],
        "cleanup_verified": True,
    })
    assert result["passed"] is True
    assert result["golden_set_eligible"] is False
    assert all(result["stages"].values())


def test_replay_score_rejects_late_detection_wrong_diagnosis_and_action():
    result = score_replay("osd_down", {
        "detected_ceph_code": "OSD_DOWN",
        "detection_seconds": 61,
        "diagnosis_text": "network issue",
        "action_id": "restart_osd",
        "recovered": False,
        "unexpected_health_codes": [],
        "cleanup_verified": True,
    })
    assert result["passed"] is False
    assert result["stages"]["detection"] is False
    assert result["stages"]["diagnosis"] is False
    assert result["stages"]["proposal"] is False


def test_golden_set_approval_is_rejected_from_scenario_catalog(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text(
        """schema_version: 1
scenarios:
  - id: reviewed
    mode: replay
    ceph_code: REVIEWED
    severity: HEALTH_WARN
    message: reviewed fixture
    replay: {log_excerpt: fixture, metrics: {}}
    expect:
      diagnosis_markers: [fixture]
      acceptable_action_ids: [investigate_manually]
      detection_timeout_seconds: 30
    provenance:
      source: anonymized_incident
      verified_real_incident: true
      golden_set_approved: true
      source_incident_ref: incident-123
      reviewed_by: operator@example.test
      reviewed_at: '2026-10-06T10:00:00Z'
""",
        encoding="utf-8",
    )
    with pytest.raises(SyntheticInjectionError, match="cannot be granted by the scenario catalog"):
        load_scenario_catalog(path)

    text = path.read_text(encoding="utf-8").replace("golden_set_approved: true", "golden_set_approved: false")
    path.write_text(text, encoding="utf-8")
    scenario = load_scenario_catalog(path)["reviewed"]
    assert scenario.verified_real_incident is True


def test_replay_report_scores_runs_and_summarizes_campaign():
    report = score_replay_report({
        "schema_version": 1,
        "campaign_id": "campaign-1",
        "runs": [{
            "run_id": "run-1",
            "scenario_id": "osd_down",
            "observed": {
                "detected_ceph_code": "OSD_DOWN",
                "detection_seconds": 12,
                "diagnosis_text": "osd.0 is down",
                "action_id": "investigate_manually",
                "recovered": True,
                "unexpected_health_codes": [],
                "cleanup_verified": True,
            },
        }, {
            "run_id": "control-1",
            "kind": "control",
            "observed": {
                "incident_created": False,
                "detected_ceph_code": None,
                "action_id": None,
                "unexpected_health_codes": [],
            },
        }],
    })
    assert report["campaign_id"] == "campaign-1"
    assert report["run_count"] == report["passed_count"] == 2
    assert report["failed_count"] == report["golden_set_eligible_count"] == 0
    assert report["control_run_count"] == 1
    assert report["control_false_positive_count"] == 0
    assert report["results"][0]["passed"] is True


def test_no_fault_control_detects_false_positive_incident_or_action():
    result = score_replay_control({
        "incident_created": True,
        "detected_ceph_code": "OSD_DOWN",
        "action_id": "restart_osd",
        "unexpected_health_codes": [],
    })
    assert result["passed"] is False
    assert result["false_positive"] is True
    assert result["stages"]["no_incident"] is False
    assert result["stages"]["no_action"] is False


def test_replay_report_rejects_unknown_schema_and_empty_campaign():
    with pytest.raises(SyntheticInjectionError, match="schema_version"):
        score_replay_report({"schema_version": 2})
    with pytest.raises(SyntheticInjectionError, match="non-empty runs"):
        score_replay_report({"schema_version": 1, "campaign_id": "empty", "runs": []})

def test_cleanup_only_closes_synthetic_rows():
    session = _session()
    cluster = _cluster()
    session.add(cluster)
    session.flush()
    synthetic, envelope = create(session, cluster=cluster, scenario_id="mon_clock_skew", actor="tester")
    session.add(Action(
        incident_id=synthetic.id, action_id="investigate_manually", classification="RISKY",
        status="PENDING_APPROVAL", target_nodes='["10.0.0.1"]',
    ))
    real = Incident(
        cluster_id=cluster.id, ceph_code="OSD_DOWN", status=IncidentStatus.NEW.value,
        detected_at=synthetic.detected_at,
    )
    session.add(real)
    session.commit()

    assert cleanup(session, cluster_id=cluster.id, run_id=envelope["synthetic_run_id"]) == 1
    session.commit()
    assert session.get(Incident, synthetic.id).status == IncidentStatus.REJECTED.value
    assert session.query(Action).filter_by(incident_id=synthetic.id).one().status == "REJECTED"
    assert session.get(Incident, real.id).status == IncidentStatus.NEW.value


def test_admin_dashboard_page_is_registered(dashboard_client):
    response = dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    assert response.status_code in {200, 302, 303}
    page = dashboard_client.get("/synthetic-incidents")
    assert page.status_code == 200
    assert "Synthetic Incident Tests" in page.text
