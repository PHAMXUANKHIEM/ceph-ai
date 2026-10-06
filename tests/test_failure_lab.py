"""Failure Lab replay campaigns (FL1): inject, read the Worker's outcome, score, clean up."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import failure_lab
from shared.db import Base
from shared.models import Action, Cluster, Incident, IncidentStatus
from shared.synthetic_incidents import SyntheticInjectionError


def _lab(environment="lab"):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        cluster = Cluster(name="CS-LAB", ceph_mon_nodes="10.0.0.1", ssh_user="root",
                          ssh_key_path="/tmp/readonly-id_ed25519", autonomy_environment=environment)
        session.add(cluster)
        session.commit()
        return factory, cluster.id


def _worker(factory, diagnosis, action_id="investigate_manually"):
    """Stands in for the real Worker: diagnose the published Incident."""
    def publish(envelope):
        with factory() as session:
            incident = session.get(Incident, envelope["incident_id"])
            incident.diagnosis_text = diagnosis
            incident.status = IncidentStatus.PENDING_APPROVAL.value
            session.add(Action(incident_id=incident.id, action_id=action_id, classification="SAFE",
                               status="PENDING_APPROVAL", target_nodes="[]"))
            session.commit()
    return publish


def test_a_campaign_scores_what_the_worker_concluded_and_cleans_up():
    factory, cluster_id = _lab()

    report = failure_lab.run_campaign(
        factory, cluster_id=cluster_id, scenario_ids=["osd_down"], campaign_id="c1",
        publish=_worker(factory, "osd.0 trên node 10.0.0.1 đang bị dừng"),
        wait_seconds=10, sleep=lambda seconds: None,
    )

    assert report["run_count"] == 1 and report["passed_count"] == 1
    assert report["results"][0]["stages"]["diagnosis"] is True
    assert report["runs"][0]["observed"]["action_id"] == "investigate_manually"
    with factory() as session:
        incident = session.get(Incident, report["runs"][0]["incident_id"])
        assert incident.status == IncidentStatus.REJECTED.value  # synthetic rows of the run closed


def test_a_wrong_diagnosis_or_action_fails_the_run():
    factory, cluster_id = _lab()

    report = failure_lab.run_campaign(
        factory, cluster_id=cluster_id, scenario_ids=["osd_down"], campaign_id="c2",
        publish=_worker(factory, "mạng chậm", action_id="restart_osd_daemon"),
        wait_seconds=10, sleep=lambda seconds: None,
    )

    stages = report["results"][0]["stages"]
    assert report["passed_count"] == 0 and stages["diagnosis"] is False and stages["proposal"] is False


def test_no_diagnosis_before_the_deadline_is_a_timed_out_failure():
    factory, cluster_id = _lab()
    now = {"t": 0.0}

    result = failure_lab.run_replay(
        factory, cluster_id=cluster_id, scenario_id="osd_down", publish=lambda envelope: None,
        wait_seconds=30, poll_seconds=5, sleep=lambda seconds: now.__setitem__("t", now["t"] + seconds),
        clock=lambda: now["t"],
    )

    assert result["observed"]["timed_out"] is True and result["observed"]["diagnosis_text"] == ""
    with factory() as session:
        assert session.get(Incident, result["incident_id"]).status == IncidentStatus.REJECTED.value


def test_a_production_cluster_is_refused():
    factory, cluster_id = _lab(environment="production")

    with pytest.raises(SyntheticInjectionError):
        failure_lab.run_replay(factory, cluster_id=cluster_id, scenario_id="osd_down",
                               publish=lambda envelope: pytest.fail("must not publish"), wait_seconds=1)
