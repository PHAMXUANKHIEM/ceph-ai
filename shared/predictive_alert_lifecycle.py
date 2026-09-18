"""Explicit state machine for predictive-alert lifecycle decisions."""

from __future__ import annotations

from enum import Enum


class AlertLifecycleState(str, Enum):
    NORMAL = "NORMAL"
    CANDIDATE = "CANDIDATE"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    RECOVERING = "RECOVERING"
    RECOVERED = "RECOVERED"
    DATA_QUALITY = "DATA_QUALITY"
    SUPPRESSED = "SUPPRESSED"


class NotificationState(str, Enum):
    IDLE = "IDLE"
    SENT = "SENT"
    COOLDOWN = "COOLDOWN"
    FAILED = "FAILED"
    SUPPRESSED = "SUPPRESSED"


def next_lifecycle_state(
    previous: str | None,
    *,
    quality_ok: bool,
    candidate: bool = False,
    warning: bool = False,
    critical: bool = False,
    suppressed: bool = False,
) -> str:
    """Return the next state; notification delivery is intentionally separate."""

    if suppressed:
        return AlertLifecycleState.SUPPRESSED.value
    if not quality_ok:
        return AlertLifecycleState.DATA_QUALITY.value
    if critical:
        return AlertLifecycleState.CRITICAL.value
    if warning:
        return AlertLifecycleState.WARNING.value
    if candidate:
        return AlertLifecycleState.CANDIDATE.value
    if previous in {
        AlertLifecycleState.WARNING.value,
        AlertLifecycleState.CRITICAL.value,
    }:
        return AlertLifecycleState.RECOVERING.value
    if previous == AlertLifecycleState.RECOVERING.value:
        return AlertLifecycleState.RECOVERED.value
    if previous == AlertLifecycleState.RECOVERED.value:
        return AlertLifecycleState.NORMAL.value
    return AlertLifecycleState.NORMAL.value
