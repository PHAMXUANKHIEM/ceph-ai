"""Compatibility facade for the persisted AI telemetry summary."""

from __future__ import annotations

from datetime import datetime

from shared.ai_telemetry import summary as _summary


def summary(period_hours: int, *, now: datetime | None = None) -> dict:
    """Return content-free persisted usage for the requested period."""
    return _summary(period_hours, now=now)
