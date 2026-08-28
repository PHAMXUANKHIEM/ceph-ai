import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.models import Incident, IncidentStatus
from watcher.incident_grouping import (
    assign_incident_group,
    build_group_context,
    incidents_are_related,
)


def _session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _incident(session, incident_id, code, minutes, *, cluster_id=None, osd_id=None):
    incident = Incident(
        id=incident_id,
        cluster_id=cluster_id,
        ceph_code=code,
        status=IncidentStatus.NEW.value,
        detected_at=datetime(2026, 8, 28, 10, 0) + timedelta(minutes=minutes),
        log_excerpt=f"{code} osd.{osd_id} host=node-a" if osd_id is not None else code,
        signal_evidence_json=json.dumps({"osd_id": osd_id}) if osd_id is not None else None,
    )
    session.add(incident)
    session.flush()
    return incident


def test_same_code_incidents_share_root():
    session = _session()
    root = _incident(session, "root", "OSD_DOWN", 0, osd_id=3)
    child = _incident(session, "child", "OSD_DOWN", 5, osd_id=4)

    assert assign_incident_group(session, root) == "root"
    assert assign_incident_group(session, child) == "root"
    assert root.group_root_incident_id == "root"
    assert child.group_root_incident_id == "root"


def test_same_family_requires_shared_entity():
    left = Incident(
        id="left", ceph_code="BLUESTORE_SLOW_OP_ALERT", status="NEW",
        detected_at=datetime(2026, 8, 28, 10, 0),
        signal_evidence_json='{"osd_id": 5}',
    )
    right = Incident(
        id="right", ceph_code="OSD_LATENCY_HIGH:osd.5", status="NEW",
        detected_at=datetime(2026, 8, 28, 10, 1),
        log_excerpt="slow operation on osd.5",
    )
    unrelated = Incident(
        id="unrelated", ceph_code="OSD_LATENCY_HIGH:osd.6", status="NEW",
        detected_at=datetime(2026, 8, 28, 10, 1),
        log_excerpt="slow operation on osd.6",
    )

    assert incidents_are_related(left, right)
    assert not incidents_are_related(left, unrelated)


def test_different_clusters_never_share_root():
    session = _session()
    left = _incident(session, "cluster-a", "OSD_DOWN", 0, cluster_id="a", osd_id=3)
    right = _incident(session, "cluster-b", "OSD_DOWN", 1, cluster_id="b", osd_id=3)

    assign_incident_group(session, left)
    assign_incident_group(session, right)

    assert left.group_root_incident_id == "cluster-a"
    assert right.group_root_incident_id == "cluster-b"


def test_group_context_returns_root_and_related_incidents():
    session = _session()
    root = _incident(session, "root", "OSD_DOWN", 0, osd_id=3)
    child = _incident(session, "child", "OSD_DOWN", 5, osd_id=4)
    assign_incident_group(session, root)
    assign_incident_group(session, child)

    context = build_group_context(session, "child")

    assert context["root_incident_id"] == "root"
    assert [row["incident_id"] for row in context["related_incidents"]] == ["root"]


def test_router_prompt_includes_group_context():
    from worker.llm.router_client import _build_user_content

    content = _build_user_content({
        "ceph_code": "OSD_DOWN",
        "detected_at": "2026-08-28T10:05:00",
        "nodes": [],
        "cluster_snapshot": {},
        "log_excerpt": "osd.4 down",
        "incident_group": {
            "root_incident_id": "root",
            "related_incidents": [{
                "incident_id": "root", "ceph_code": "OSD_LATENCY_HIGH:osd.3",
                "status": "RESOLVED", "severity": "HEALTH_WARN",
                "detected_at": "2026-08-28T10:00:00",
                "diagnosis_text": "Disk latency tăng",
            }],
        },
    })

    assert "Deterministic incident group root: root" in content
    assert "OSD_LATENCY_HIGH:osd.3" in content
    assert "Disk latency tăng" in content
