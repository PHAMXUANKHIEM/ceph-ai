"""Operator requests are not health checks (shared/operator_requests.py)."""

import re
from datetime import datetime
from pathlib import Path

import pytest

import watcher.main as watcher_main
from shared import db as db_module
from shared.models import Action, Incident, IncidentStatus
from shared.operator_requests import OPERATOR_REQUEST_CODES, is_operator_request

ROOT = Path(__file__).resolve().parents[1]
_CONSTANT_RE = re.compile(r'^[A-Z_]*_CEPH_CODE\s*=\s*"([A-Z0-9_]+)"', re.M)


def test_every_request_code_in_the_code_base_is_declared():
    """A new Dashboard request with its own *_CEPH_CODE must be added to OPERATOR_REQUEST_CODES."""
    found = set()
    for folder in ("dashboard/routes", "worker", "shared"):
        for path in (ROOT / folder).rglob("*.py"):
            found |= set(_CONSTANT_RE.findall(path.read_text(encoding="utf-8")))
    assert found - OPERATOR_REQUEST_CODES == set()


@pytest.mark.parametrize("code", ["FILE_SERVICE_CHANGE:cephfs_create_volume", "OSD_REPLACE", "NODE_MAINTENANCE",
                                  "CLUSTER_NODE_ADD", "POOL_CREATE_REQUEST"])
def test_a_pending_request_survives_the_health_recovery_pass(code):
    """11/10/2026: four pending CS-STAGING service requests were closed as "recovered" and cancelled."""
    with db_module.SessionLocal() as session:
        incident = Incident(ceph_code=code, status=IncidentStatus.PENDING_APPROVAL.value, detected_at=datetime.utcnow())
        session.add(incident)
        session.flush()
        session.add(Action(incident_id=incident.id, action_id="cephfs_create_volume", classification="RISKY",
                           status="PENDING_APPROVAL", target_nodes="[]"))
        session.commit()
        incident_id = incident.id

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, incident_id).status == IncidentStatus.PENDING_APPROVAL.value
        assert session.query(Action).filter_by(incident_id=incident_id).one().status == "PENDING_APPROVAL"


def test_health_codes_are_not_requests():
    assert not is_operator_request("OSD_DOWN") and not is_operator_request("POOL_NEARFULL")
    assert not is_operator_request(None)
