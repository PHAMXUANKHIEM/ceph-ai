"""Normalize provider quota payloads for Settings and the chat widget."""

from __future__ import annotations


def normalize_rate_limits(payload: dict | None) -> list[dict]:
    """Convert Codex/Claude-style quota windows to day/week remaining percentages."""
    if not payload:
        return []
    snapshot = payload.get("rateLimits") or payload.get("rate_limits") or payload
    windows = []
    for key, label in (("primary", "Ngày"), ("secondary", "Tuần")):
        item = snapshot.get(key) or {}
        used = item.get("usedPercent", item.get("used_percent"))
        if used is None:
            continue
        remaining = max(0, min(100, 100 - int(used)))
        windows.append({
            "period": key,
            "label": label,
            "remaining_percent": remaining,
            "used_percent": 100 - remaining,
            "resets_at": item.get("resetsAt", item.get("resets_at")),
        })
    return windows
