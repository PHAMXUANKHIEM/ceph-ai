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

    production = _cluster("production")
    production.is_active = True
    with pytest.raises(SyntheticInjectionError, match="environment=lab"):
        create(session, cluster=production, scenario_id="osd_down", actor="tester")


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
