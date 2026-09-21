#!/usr/bin/env python3
"""Print content-free Natural Language rollout metrics for a time window."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

# Make direct ``python scripts/report_*.py`` execution use this checkout's
# source tree rather than an older globally installed package in a container.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import settings
from shared import audit, db
from shared.ai_cost import summary as ai_cost_summary
from shared.models import AIInvocation, AuditEntry, ChatMessage


_APPROVAL_EVENTS = frozenset({
    audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
    audit.EVENT_RISKY_ACTION_APPROVED,
    audit.EVENT_RISKY_ACTION_REJECTED,
    audit.EVENT_RISKY_ACTION_EXECUTED,
    audit.EVENT_RISKY_ACTION_FAILED,
    audit.EVENT_RISKY_ACTION_APPROVAL_EXPIRED,
    audit.EVENT_RISKY_ACTION_ACKNOWLEDGED_NO_COMMAND,
})


def _context(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _feature_flags() -> dict[str, bool]:
    return {
        "query_planner": bool(settings.ai_natural_language_query_planner_enabled),
        "snapshot_runner": bool(settings.ai_natural_language_snapshot_runner_enabled),
        "rag": bool(settings.ai_natural_language_rag_enabled),
        "fast_path": bool(settings.ai_natural_language_fast_path_enabled),
        "structured_output": bool(settings.ai_natural_language_structured_output_enabled),
        "snapshot_refresh": bool(settings.ai_natural_language_snapshot_refresh_enabled),
        "mcp": bool(settings.ai_natural_language_mcp_enabled),
    }


def _latency_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)
    p95_index = max(0, min(len(ordered) - 1, int((len(ordered) * 0.95) + 0.999999) - 1))
    return {
        "count": len(ordered),
        "p50_ms": round(median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "max_ms": round(max(ordered), 3),
    }


def _monitoring_window(*, now: datetime, state_path: str | Path) -> dict:
    path = Path(state_path)
    started_at = None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        started_at = datetime.fromisoformat(str(state.get("started_at", "")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        started_at = None
    if started_at is None:
        started_at = now
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"started_at": started_at.isoformat()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            # Reporting remains useful in read-only/test environments; the
            # next invocation will retry establishing the durable start time.
            pass
    elapsed_hours = max(0.0, (now - started_at).total_seconds() / 3600)
    return {
        "started_at": started_at.isoformat() + "Z",
        "elapsed_hours": round(elapsed_hours, 3),
        "minimum_hours": 24,
        "ready_for_close": elapsed_hours >= 24,
    }


def build_report(
    *, hours: int = 24, cluster_id: str | None = None, now=None,
    state_path: str | Path = "/var/lib/ceph-ai/nl-rollout-monitoring-start.json",
) -> dict:
    hours = max(1, min(int(hours), 168))
    now = now or datetime.utcnow()
    cutoff = now - timedelta(hours=hours)
    with db.SessionLocal() as session:
        message_query = session.query(ChatMessage).filter(
            ChatMessage.created_at >= cutoff,
            ChatMessage.nl_context_json.is_not(None),
        )
        if cluster_id:
            message_query = message_query.filter(ChatMessage.cluster_id == cluster_id)
        messages = message_query.all()

        chat_requests = session.query(AuditEntry).filter(
            AuditEntry.created_at >= cutoff,
            AuditEntry.event_type == audit.EVENT_CHAT_ACTION_REQUESTED,
        ).all()
        action_ids = {row.action_id for row in chat_requests if row.action_id}
        lifecycle = session.query(AuditEntry).filter(
            AuditEntry.created_at >= cutoff,
            AuditEntry.event_type.in_(_APPROVAL_EVENTS),
            AuditEntry.action_id.in_(action_ids) if action_ids else False,
        ).all()
        invocations = session.query(AIInvocation).filter(
            AIInvocation.created_at >= cutoff,
        ).all()

    contexts = [_context(row.nl_context_json) for row in messages]
    scopes = Counter(
        context.get("rollout", {}).get("scope", "unknown")
        for context in contexts
        if isinstance(context.get("rollout"), dict)
    )
    intents = Counter(context.get("intent", "unknown") for context in contexts)
    evidence_count = sum(bool(context.get("evidence_refs")) for context in contexts)
    intent_latencies = [
        float(context["telemetry"]["intent_latency_ms"])
        for context in contexts
        if isinstance(context.get("telemetry"), dict)
        and context["telemetry"].get("intent_latency_ms") is not None
    ]
    query_latencies = [
        float(context["telemetry"]["query_latency_ms"])
        for context in contexts
        if isinstance(context.get("telemetry"), dict)
        and context["telemetry"].get("query_latency_ms") is not None
    ]
    validation_rejections = sum(
        bool(context.get("telemetry", {}).get("validation_rejected"))
        for context in contexts
        if isinstance(context.get("telemetry"), dict)
    )
    events = Counter(row.event_type for row in lifecycle)
    errors = sum(row.status == "ERROR" for row in invocations)

    return {
        "schema_version": "nl-rollout-report-v1",
        "observed_at": now.isoformat() + "Z",
        "window_hours": hours,
        "cluster_id": cluster_id,
        "monitoring_window": _monitoring_window(now=now, state_path=state_path),
        "rollout": {
            "admin_only": bool(settings.ai_natural_language_admin_only),
            "feature_flags": _feature_flags(),
        },
        "contexts": {
            "count": len(contexts),
            "by_scope": dict(sorted(scopes.items())),
            "by_intent": dict(sorted(intents.items())),
            "with_evidence": evidence_count,
            "validation_rejections": validation_rejections,
            "latency_ms": {
                "intent": _latency_summary(intent_latencies),
                "query": _latency_summary(query_latencies),
            },
        },
        "provider_telemetry": {
            "scope": "all_ai_invocations_in_window",
            "calls": len(invocations),
            "errors": errors,
            "error_rate": round(errors / len(invocations), 4) if invocations else 0.0,
            "cost": ai_cost_summary(hours=hours, now=now),
        },
        "chat_action_approval": {
            "chat_action_requests": len(chat_requests),
            "lifecycle_events": dict(sorted(events.items())),
            "approval_events": sum(
                events[event] for event in (
                    audit.EVENT_RISKY_ACTION_APPROVED,
                    audit.EVENT_RISKY_ACTION_ACKNOWLEDGED_NO_COMMAND,
                )
            ),
            "rejection_events": events[audit.EVENT_RISKY_ACTION_REJECTED],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--cluster-id", default=None)
    parser.add_argument(
        "--state-file",
        default="/var/lib/ceph-ai/nl-rollout-monitoring-start.json",
    )
    args = parser.parse_args()
    print(json.dumps(
        build_report(
            hours=args.hours,
            cluster_id=args.cluster_id,
            state_path=args.state_file,
        ),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
