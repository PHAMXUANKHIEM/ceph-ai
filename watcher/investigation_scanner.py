"""Collect read-only evidence for newly opened incidents (autonomy plan WP3.3).

Runs as a Watcher auxiliary scan in its own thread: each tick picks at most
``investigation_incidents_per_scan`` open incidents younger than
``investigation_max_age_minutes`` that have no evidence yet, runs their
runbook (worker/policy/investigation_runbooks.yaml) through the bounded
``EvidenceRunner`` and stores the results.  Incident creation never waits
for this.  One fault family per cluster is investigated at most once per
runner cooldown, so a burst of similar incidents costs one run.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import exists, or_

from shared import incident_evidence, investigation_runbooks
from shared.autonomy_kpi import fault_family
from shared.evidence_collectors import EvidenceRunner, SshTransport
from shared.models import Incident, IncidentEvidence
from shared.synthetic_incidents import SYNTHETIC_EVIDENCE_KEY
from shared.time import utc_now

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = ("AUTO_FIXED", "RESOLVED", "REJECTED")
_runners: dict[str, EvidenceRunner] = {}
_runners_lock = threading.Lock()


def runner_for(key: str, factory: Callable[[], EvidenceRunner]) -> EvidenceRunner:
    """One runner per cluster, so breaker and cooldown state survive ticks."""
    with _runners_lock:
        if key not in _runners:
            _runners[key] = factory()
        return _runners[key]


def signal_evidence(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def pending(session, *, cluster_id: str | None, include_unscoped: bool, now: datetime,
            max_age: timedelta, limit: int) -> list[Any]:
    has_evidence = exists().where(IncidentEvidence.incident_id == Incident.id)
    query = session.query(Incident.id, Incident.ceph_code, Incident.signal_evidence_json).filter(
        Incident.created_at >= now - max_age,
        Incident.status.notin_(TERMINAL_STATUSES),
        ~has_evidence,
        # Failure Lab incidents describe an imagined fault; the real cluster's
        # state would contradict the scenario and spoil its score.
        or_(Incident.signal_evidence_json.is_(None),
            ~Incident.signal_evidence_json.contains(f'"{SYNTHETIC_EVIDENCE_KEY}"')),
    )
    if cluster_id:
        scope = Incident.cluster_id == cluster_id
        query = query.filter(or_(scope, Incident.cluster_id.is_(None)) if include_unscoped else scope)
    return query.order_by(Incident.created_at).limit(limit).all()


def investigate(session_factory, transport, runner: EvidenceRunner, *, cluster_id: str | None,
                include_unscoped: bool = False, now: datetime | None = None, max_age: timedelta,
                limit: int) -> list[dict]:
    now = now or utc_now()
    with session_factory() as session:
        # Read a few extra: flapping repeats and cooled-down families are skipped.
        rows = pending(session, cluster_id=cluster_id, include_unscoped=include_unscoped, now=now,
                       max_age=max_age, limit=limit * 5)
    done: list[dict] = []
    for row in rows:
        if len(done) >= limit:
            break
        signal = signal_evidence(row.signal_evidence_json)
        if signal.get("flapping"):
            with session_factory() as session:
                incident_evidence.mark_flapping(session, row.id)
                session.commit()
            continue
        if not runner.claim(f"{cluster_id or 'default'}:{fault_family(row.ceph_code)}", now):
            continue
        plan = investigation_runbooks.plan(
            row.ceph_code, investigation_runbooks.context_for(row.ceph_code, signal),
            mon_host=transport.mon_nodes[0] if transport.mon_nodes else None,
        )
        results = runner.run(plan.requests)
        with session_factory() as session:
            summary = incident_evidence.store(session, row.id, plan, results)
            triage = incident_evidence.record_triage(session, row.id, row.ceph_code)
            session.commit()
        done.append({"incident_id": row.id, **summary, "triage": triage.conclusion})
    return done


def scan_default_cluster(cluster_id: str | None) -> list[dict]:
    """Watcher entry point for the default cluster (settings-configured)."""
    from config.settings import settings
    from shared import db

    if not incident_evidence.investigation_allowed(cluster_id):
        return []
    transport = SshTransport(None)
    if not transport.mon_nodes:
        return []
    runner = runner_for(cluster_id or "default", lambda: EvidenceRunner(transport))
    done = investigate(
        db.SessionLocal, transport, runner, cluster_id=cluster_id, include_unscoped=True,
        max_age=timedelta(minutes=settings.investigation_max_age_minutes),
        limit=settings.investigation_incidents_per_scan,
    )
    for item in done:
        logger.info("investigation: %s", item)
    return done
