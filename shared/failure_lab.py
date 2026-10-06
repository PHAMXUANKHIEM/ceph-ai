"""Failure Lab replay campaigns (Plan/in-progress/failure-lab-plan-2026-10-06.md, FL1).

A replay run injects one synthetic Incident from the reviewed catalog
(shared/synthetic_incidents.py), lets the real Worker diagnose it — the
Worker blocks every execution for synthetic Incidents — then reads back what
the Worker concluded, scores it against the scenario's expectations and
closes the synthetic rows of that run. Nothing on the Ceph cluster changes.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from shared.models import Action, Cluster, Incident, IncidentStatus
from shared.synthetic_incidents import cleanup, create, score_replay_report

# The Worker has finished with an Incident once it left these states.
_DIAGNOSING_STATES = {IncidentStatus.NEW.value, IncidentStatus.DIAGNOSING.value}


def diagnosis_ready(incident: Incident | None) -> bool:
    return incident is not None and (
        bool((incident.diagnosis_text or "").strip()) or incident.status not in _DIAGNOSING_STATES
    )


def observed_outcome(incident: Incident, action: Action | None) -> dict[str, Any]:
    """What the Worker produced, in the shape score_replay() expects.

    Replay creates the Incident directly, so detection is immediate and the
    cluster is never touched (no unexpected health codes by construction).
    """
    return {
        "detected_ceph_code": incident.ceph_code,
        "detection_seconds": 0,
        "diagnosis_text": incident.diagnosis_text or "",
        "action_id": action.action_id if action is not None else None,
        "unexpected_health_codes": [],
        "incident_status": incident.status,
    }


def _latest_action(session, incident_id: str) -> Action | None:
    return (
        session.query(Action).filter(Action.incident_id == incident_id)
        .order_by(Action.created_at.desc()).first()
    )


def run_replay(
    session_factory: Callable[[], Any],
    *,
    cluster_id: str,
    scenario_id: str,
    publish: Callable[[dict], None],
    wait_seconds: float = 300,
    poll_seconds: float = 5,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Inject, wait for the Worker, read the outcome, clean up. One campaign run."""
    with session_factory() as session:
        cluster = session.get(Cluster, cluster_id)
        incident, envelope = create(session, cluster=cluster, scenario_id=scenario_id, actor="failure-lab")
        session.commit()
        incident_id, run_id = incident.id, envelope["synthetic_run_id"]
    publish(envelope)

    deadline = clock() + wait_seconds
    outcome: dict[str, Any] | None = None
    while outcome is None:
        with session_factory() as session:
            incident = session.get(Incident, incident_id)
            if diagnosis_ready(incident):
                outcome = observed_outcome(incident, _latest_action(session, incident_id))
            elif clock() >= deadline:
                outcome = {"detected_ceph_code": incident.ceph_code if incident else None, "detection_seconds": 0,
                           "diagnosis_text": "", "action_id": None, "unexpected_health_codes": [],
                           "timed_out": True}
        if outcome is None:
            sleep(poll_seconds)

    with session_factory() as session:
        cleanup(session, cluster_id=cluster_id, run_id=run_id)
        session.commit()
    return {"run_id": run_id, "incident_id": incident_id, "scenario_id": scenario_id, "observed": outcome}


def run_campaign(session_factory: Callable[[], Any], *, cluster_id: str, scenario_ids: list[str],
                 campaign_id: str, publish: Callable[[dict], None], **wait: Any) -> dict[str, Any]:
    """Run scenarios one after another and score them as one campaign report."""
    runs = [run_replay(session_factory, cluster_id=cluster_id, scenario_id=scenario_id, publish=publish, **wait)
            for scenario_id in scenario_ids]
    report = score_replay_report({"schema_version": 1, "campaign_id": campaign_id, "runs": runs})
    report["runs"] = runs
    return report


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str)
