"""Safe lab-only synthetic incident injection.

Synthetic incidents exercise the normal Incident -> AI diagnosis pipeline
without issuing a Ceph command.  They are deliberately marked in the
incident evidence so the Watcher does not reconcile them against live health
and the Worker can fail closed before any executor is reached.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from shared.incident_actions import cancel_pending_actions
from shared.models import Cluster, Incident, IncidentStatus
from watcher import publisher

SYNTHETIC_EVIDENCE_KEY = "synthetic_injection"
SYNTHETIC_MODE = "shadow-only"


class SyntheticInjectionError(ValueError):
    """Raised when an injection request fails a safety invariant."""


@dataclass(frozen=True)
class Scenario:
    id: str
    ceph_code: str
    severity: str
    message: str


SCENARIOS: dict[str, Scenario] = {
    "osd_down": Scenario(
        id="osd_down", ceph_code="OSD_DOWN", severity="HEALTH_WARN",
        message="Synthetic lab fault: osd.0 is reported down.",
    ),
    "mon_clock_skew": Scenario(
        id="mon_clock_skew", ceph_code="MON_CLOCK_SKEW", severity="HEALTH_WARN",
        message="Synthetic lab fault: monitor clock skew is reported.",
    ),
    "pg_degraded": Scenario(
        id="pg_degraded", ceph_code="PG_DEGRADED", severity="HEALTH_WARN",
        message="Synthetic lab fault: placement groups are degraded.",
    ),
    "osd_nearfull": Scenario(
        id="osd_nearfull", ceph_code="OSD_NEARFULL", severity="HEALTH_WARN",
        message="Synthetic lab fault: an OSD is near full.",
    ),
}


def is_synthetic_evidence(raw: str | None) -> bool:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and value.get(SYNTHETIC_EVIDENCE_KEY) is True


def _cluster_nodes(cluster: Cluster) -> list[str]:
    return [node.strip() for node in (cluster.ceph_mon_nodes or "").split(",") if node.strip()]


def create(session, *, cluster: Cluster, scenario_id: str, actor: str) -> tuple[Incident, dict]:
    """Create one un-published synthetic Incident and its safe envelope.

    Only a cluster explicitly placed in the ``lab`` commissioning environment
    is accepted.  The returned envelope is marked shadow-only; callers may
    publish it to RabbitMQ, but no command is ever allowed to run for it.
    """
    scenario = SCENARIOS.get(scenario_id)
    if scenario is None:
        raise SyntheticInjectionError("Unknown synthetic scenario")
    if not cluster.is_active:
        raise SyntheticInjectionError("Cluster is not active")
    if cluster.autonomy_environment != "lab":
        raise SyntheticInjectionError("Synthetic injection chỉ được phép trên cluster có environment=lab")
    nodes = _cluster_nodes(cluster)
    if not nodes:
        raise SyntheticInjectionError("Cluster chưa có MON node để dựng evidence")

    run_id = str(uuid.uuid4())
    detected_at = datetime.utcnow()
    snapshot = {
        "status": "HEALTH_WARN",
        "checks": {
            scenario.ceph_code: {
                "severity": scenario.severity,
                "detail": [{"message": scenario.message}],
            },
        },
        SYNTHETIC_EVIDENCE_KEY: True,
        "scenario": scenario.id,
        "run_id": run_id,
        "mode": SYNTHETIC_MODE,
    }
    evidence = {
        SYNTHETIC_EVIDENCE_KEY: True,
        "scenario": scenario.id,
        "run_id": run_id,
        "mode": SYNTHETIC_MODE,
        "created_by": actor,
    }
    incident = Incident(
        cluster_id=cluster.id,
        ceph_code=scenario.ceph_code,
        status=IncidentStatus.NEW.value,
        severity=scenario.severity,
        log_excerpt=scenario.message,
        signal_evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        detected_at=detected_at,
    )
    session.add(incident)
    session.flush()
    envelope = publisher.build_envelope(
        incident_id=incident.id,
        ceph_code=scenario.ceph_code,
        detected_at=detected_at.isoformat(),
        nodes=nodes,
        log_excerpt=scenario.message,
        cluster_snapshot=snapshot,
        cluster_id=cluster.id,
        ssh_user=cluster.ssh_user or "",
        ssh_key_path=cluster.ssh_key_path or "",
        ceph_exec_mode=cluster.ceph_exec_mode or "",
        ceph_container_name=cluster.ceph_container_name or "",
    )
    envelope[SYNTHETIC_EVIDENCE_KEY] = True
    envelope["synthetic_scenario"] = scenario.id
    envelope["synthetic_run_id"] = run_id
    envelope["synthetic_mode"] = SYNTHETIC_MODE
    return incident, envelope


def cleanup(session, *, cluster_id: str, run_id: str | None = None) -> int:
    """Close only synthetic rows for a cluster, never real incidents."""
    rows = session.query(Incident).filter(Incident.cluster_id == cluster_id).all()
    changed = 0
    for incident in rows:
        try:
            evidence = json.loads(incident.signal_evidence_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(evidence, dict) or evidence.get(SYNTHETIC_EVIDENCE_KEY) is not True:
            continue
        if run_id is not None and evidence.get("run_id") != run_id:
            continue
        if incident.status in {
            IncidentStatus.NEW.value, IncidentStatus.DIAGNOSING.value,
            IncidentStatus.PENDING_APPROVAL.value, IncidentStatus.APPROVED.value,
            IncidentStatus.EXECUTING.value, IncidentStatus.VERIFYING.value,
            IncidentStatus.FAILED.value,
        }:
            incident.status = IncidentStatus.REJECTED.value
            cancel_pending_actions(session, incident.id)
            changed += 1
    return changed
