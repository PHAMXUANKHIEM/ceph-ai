"""Acceptance-window gate for choosing a drift detector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class DetectorWindowEvidence:
    window_id: str
    false_positive_rate: float
    detection_delay: float
    delay_unit: str
    cpu_ms: float


@dataclass(frozen=True)
class DetectorSelection:
    status: str
    selected: str | None
    failed_checks: tuple[str, ...]
    reason: str


def choose_detector(
    baseline: Iterable[DetectorWindowEvidence],
    candidate: Iterable[DetectorWindowEvidence], *,
    minimum_windows: int = 3, max_cpu_ratio: float = 2.0,
) -> DetectorSelection:
    base = {item.window_id: item for item in baseline}
    trial = {item.window_id: item for item in candidate}
    failed: list[str] = []
    if len(base) < minimum_windows or len(trial) < minimum_windows:
        failed.append("acceptance_window")
    if set(base) != set(trial):
        failed.append("window_alignment")
    pairs = [(base[key], trial[key]) for key in sorted(set(base) & set(trial))]
    if any(left.delay_unit != right.delay_unit for left, right in pairs):
        failed.append("delay_unit")
    if any(right.false_positive_rate > left.false_positive_rate for left, right in pairs):
        failed.append("false_positive_rate")
    if any(right.cpu_ms > left.cpu_ms * max_cpu_ratio for left, right in pairs):
        failed.append("cpu_budget")
    if not failed and not any(right.detection_delay < left.detection_delay for left, right in pairs):
        failed.append("no_improvement")
    if failed:
        return DetectorSelection("HOLD", None, tuple(dict.fromkeys(failed)), "detector selection gate failed")
    return DetectorSelection("SELECTED", "candidate", (), "candidate is better within the acceptance window")
