"""Content-free process metrics for the natural-language rollout.

Provider latency/tokens/cost remain persisted in ``AIInvocation``. These
bounded counters cover NL-specific decisions that do not belong in that
table, such as clarification and evidence-validation rejection. Labels are
allowlisted and never contain prompt text, resource ids, or secrets.
"""

from __future__ import annotations

from collections import Counter
from threading import Lock


_lock = Lock()
_counts: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
_ALLOWED_LABELS = frozenset({"intent", "status", "provider", "cache", "source"})


def record_natural_language_metric(event: str, **labels: object) -> None:
    event = str(event or "unknown").strip()[:64] or "unknown"
    safe_labels = tuple(sorted(
        (key, str(value)[:64])
        for key, value in labels.items()
        if key in _ALLOWED_LABELS and value is not None and str(value).strip()
    ))
    with _lock:
        _counts[(event, safe_labels)] += 1


def get_natural_language_metrics() -> dict[str, object]:
    with _lock:
        rows = [
            {"event": event, "labels": dict(labels), "count": count}
            for (event, labels), count in sorted(_counts.items())
        ]
    return {"events": rows}


def reset_natural_language_metrics() -> None:
    with _lock:
        _counts.clear()
