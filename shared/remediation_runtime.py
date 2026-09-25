"""Single runtime-decision boundary for remediation dispatch.

The evaluator is pure; the persistence helper is append-only.  This keeps
every executor caller explainable and makes split-brain/kill-switch decisions
visible without exposing credentials or command text.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timedelta

from config.settings import settings
from shared.autopilot_guardrails import GuardrailContext, GuardrailDecision, resolve_mode, evaluate_guardrails
from shared.models import Action, ActionPolicyOverride, Cluster, RemediationRuntimeDecision
from worker.policy.playbook_registry import get_contract
from worker.policy import gate


def _target_count(action: Action | None) -> int:
    if action is None:
        return 0
    try:
        targets = json.loads(action.target_nodes or "[]")
    except (TypeError, ValueError):
        return 0
    return len(targets) if isinstance(targets, list) else 0


def build_context(
    session,
    *,
    action: Action,
    cluster: Cluster | None,
    health_status: str,
    now: datetime,
    target_nodes: list[str] | None = None,
) -> GuardrailContext:
    cluster_enabled = bool(cluster and cluster.autopilot_enabled)
    configured_mode = str(getattr(cluster, "autopilot_mode", "LEGACY") or "LEGACY").upper()
    mode = configured_mode if configured_mode != "LEGACY" else resolve_mode(
        global_enabled=bool(settings.autopilot_enabled),
        cluster_enabled=cluster_enabled,
    ).value
    contract = get_contract(action.action_id)
    # Use the effective, persisted classification so an authenticated
    # operator override is represented in the same decision that gates the
    # executor.  The static SAFE_ACTION_IDS set alone would incorrectly
    # reject a verified, explicitly overridden action such as an exact OSD
    # restart.
    override = session.get(ActionPolicyOverride, action.action_id)
    if override is not None:
        try:
            effective_classification = gate.classify_action(action.action_id, session=session)
        except TypeError:
            # Keep unit-test and plugin shims that expose the historical
            # one-argument classifier compatible.
            effective_classification = gate.classify_action(action.action_id)
    else:
        # The persisted Action classification includes deterministic,
        # context-specific safety proof (for example an exact OSD-to-host
        # mapping). Reclassifying only from the static YAML here would erase
        # that proof and reject a valid SAFE action.
        effective_classification = action.classification
    effective_classification = getattr(effective_classification, "value", effective_classification)
    target_count = len(target_nodes) if isinstance(target_nodes, list) else _target_count(action)
    if contract is not None and contract.max_targets:
        max_targets = int(contract.max_targets)
    else:
        # max_targets=0 means "no fixed ceiling" for the bounded OSD
        # contract; the action's own target list remains the blast-radius
        # evidence used by the executor.
        max_targets = max(target_count, 1)
    since = now - timedelta(hours=1)
    actions_used = session.query(Action).filter(
        Action.status.in_(("AUTO_EXECUTED", "EXECUTED")),
        Action.executed_at >= since,
    ).count()
    return GuardrailContext(
        mode=mode,
        kill_switch=bool(settings.autopilot_enabled),
        cluster_enabled=cluster_enabled,
        action_id=action.action_id,
        classification=str(effective_classification),
        allowlisted=str(effective_classification).upper() == "SAFE" and contract is not None,
        target_count=target_count,
        max_targets=max_targets,
        actions_used=actions_used,
        action_budget=int(settings.autopilot_max_actions_per_hour),
        cooldown_active=False,
        health_status=health_status,
        maintenance_window_open=True,
    )


def persist_decision(
    session,
    *,
    action: Action | None,
    cluster: Cluster | None,
    incident_id: str | None,
    decision: GuardrailDecision,
    context: GuardrailContext,
    reason: str | None = None,
    now: datetime | None = None,
) -> RemediationRuntimeDecision:
    row = RemediationRuntimeDecision(
        cluster_id=cluster.id if cluster else None,
        incident_id=incident_id,
        action_id=action.id if action else None,
        worker_id=f"{socket.gethostname()}:{os.getpid()}",
        mode=decision.mode.value,
        classification=str(context.classification).upper(),
        decision="ALLOWED" if decision.allowed else "BLOCKED",
        reason=reason or decision.reason,
        controls_json=json.dumps({
            "kill_switch": context.kill_switch,
            "cluster_enabled": context.cluster_enabled,
            "allowlisted": context.allowlisted,
            "target_count": context.target_count,
            "max_targets": context.max_targets,
            "actions_used": context.actions_used,
            "action_budget": context.action_budget,
            "health_status": context.health_status,
            "maintenance_window_open": context.maintenance_window_open,
        }, sort_keys=True),
        created_at=now or datetime.utcnow(),
    )
    session.add(row)
    return row


def evaluate_and_persist(session, *, action: Action, cluster: Cluster | None,
                         incident_id: str | None, health_status: str,
                         target_nodes: list[str] | None = None) -> GuardrailDecision:
    context = build_context(session, action=action, cluster=cluster,
                            health_status=health_status, now=datetime.utcnow(),
                            target_nodes=target_nodes)
    decision = evaluate_guardrails(context)
    persist_decision(session, action=action, cluster=cluster, incident_id=incident_id,
                     decision=decision, context=context)
    return decision
