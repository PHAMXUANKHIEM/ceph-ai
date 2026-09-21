"""Bounded, evidence-preserving event timeline merger."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

MAX_EVENTS = 500


def _parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _safe_event(raw: Mapping[str, Any], source: str, fallback_id: str) -> dict[str, Any] | None:
    event_id = str(raw.get("id") or raw.get("event_id") or fallback_id)
    at = raw.get("at") or raw.get("created_at") or raw.get("event_at")
    parsed = _parse_time(at)
    if parsed is None:
        return None
    result = {
        "id": f"{source}:{event_id}",
        "at": parsed.isoformat(timespec="seconds") + "Z",
        "source": source,
        "kind": str(raw.get("kind") or raw.get("event_type") or raw.get("action") or "event"),
        "actor": str(raw.get("actor") or raw.get("requester") or "unknown"),
        "summary": str(raw.get("summary") or raw.get("event_type") or raw.get("action") or "event")[:500],
    }
    for key in ("incident_id", "action_id", "status", "target", "cluster_id"):
        if raw.get(key) is not None:
            result[key] = raw[key]
    return result


def merge_event_sources(
    sources: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    limit: int = MAX_EVENTS,
) -> dict[str, Any]:
    """Merge bounded event sources and report malformed evidence explicitly."""
    events: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []
    seen: set[str] = set()
    for source, rows in sources.items():
        count = 0
        for index, raw in enumerate(rows or []):
            count += 1
            if not isinstance(raw, Mapping):
                continue
            event = _safe_event(raw, source, str(index))
            if event is None:
                gaps.append({
                    "code": "EVENT_TIMESTAMP_INVALID",
                    "reason": f"{source} có event không có timestamp hợp lệ.",
                })
                continue
            if event["id"] in seen:
                continue
            seen.add(event["id"])
            events.append(event)
        if count == 0:
            gaps.append({
                "code": "EVENT_SOURCE_EMPTY",
                "reason": f"Không có event từ source {source}.",
            })

    events.sort(key=lambda event: (event["at"], event["id"]))
    truncated = len(events) > max(1, min(int(limit), MAX_EVENTS))
    events = events[-max(1, min(int(limit), MAX_EVENTS)):]
    return {
        "status": "observed" if events else "not_available",
        "events": events,
        "summary": {
            "event_count": len(events),
            "source_counts": dict(sorted(Counter(event["source"] for event in events).items())),
            "kind_counts": dict(sorted(Counter(event["kind"] for event in events).items())),
            "truncated": truncated,
        },
        "evidence_gaps": gaps,
        "read_only": True,
        "recommendation_mode": "EVIDENCE_ONLY",
        "action_id": None,
    }
