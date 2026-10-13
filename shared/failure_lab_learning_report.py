"""Read-only FL6.5 learning metrics from completed, operator-approved lab runs.

These numbers describe lab performance, not production accuracy or model
training. Failed or incomplete runs are visible but never counted as a pass.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STAGES = ("detection", "diagnosis", "proposal", "recovery", "side_effects", "cleanup")


def _started(record: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(record.get("run_started_at") or ""))
        return value if value.tzinfo is not None else None
    except ValueError:
        return None


def _records(directory: Path) -> list[dict]:
    records = []
    for path in sorted(directory.glob("repro-*.json")) if directory.exists() else []:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _scored(record: dict) -> bool:
    stages = record.get("stages")
    return (record.get("status") == "DONE" and isinstance(stages, dict)
            and all(type(stages.get(stage)) is bool for stage in STAGES)
            and type(record.get("passed")) is bool
            and record["passed"] is all(stages[stage] for stage in STAGES))


def summarize(directory: Path, *, family: str | None = None) -> dict[str, dict[str, Any]]:
    """Aggregate only finished, six-stage-scored proposals by fault family."""
    grouped: dict[str, list[dict]] = {}
    for record in _records(directory):
        name = record.get("family")
        if not isinstance(name, str) or not name or (family is not None and name != family):
            continue
        grouped.setdefault(name, []).append(record)
    result = {}
    for name, records in sorted(grouped.items()):
        completed = sorted((record for record in records if _scored(record)),
                           key=lambda record: (_started(record) is None,
                                               _started(record) or datetime.max.replace(tzinfo=timezone.utc)))
        stage_passes = {stage: sum(bool(record["stages"][stage]) for record in completed) for stage in STAGES}
        first_correct = next((index for index, record in enumerate(completed, 1)
                              if record["stages"]["diagnosis"]), None)
        first_time = _started(completed[0]) if completed else None
        correct_time = _started(completed[first_correct - 1]) if first_correct else None
        elapsed = max(0, int((correct_time - first_time).total_seconds())) if first_time and correct_time else None
        result[name] = {
            "family": name,
            "completed_runs": len(completed),
            "passed_runs": sum(record["passed"] for record in completed),
            "stage_passes": stage_passes,
            "failed_execution_runs": sum(record.get("status") == "FAILED" for record in records),
            "unscored_done_runs": sum(record.get("status") == "DONE" and not _scored(record) for record in records),
            "first_correct_attempt": first_correct,
            "seconds_from_first_run_to_first_correct": elapsed,
            "production_reference_effect": "not_measured",
        }
    return result


def telegram_summary(summary: dict[str, Any]) -> str:
    """A bounded, plain-text lab-only notice, with no production claim."""
    family = " ".join(str(summary["family"]).split())[:80]
    total = int(summary["completed_runs"])
    correct = int(summary["stage_passes"]["diagnosis"])
    recovered = int(summary["stage_passes"]["recovery"])
    first = summary["first_correct_attempt"]
    first_text = f"lượt {first}" if first is not None else "chưa có"
    return (f"🧠 Học từ Failure Lab — {family}\n"
            f"Lab đã chấm: {total} lượt; chẩn đoán đúng {correct}/{total}; hồi phục {recovered}/{total}; "
            f"đạt đủ 6 khâu {summary['passed_runs']}/{total}.\n"
            f"Lỗi chạy: {summary['failed_execution_runs']}; lượt chưa đủ dữ liệu chấm: "
            f"{summary['unscored_done_runs']}.\n"
            f"Lần đầu chẩn đoán đúng: {first_text}. "
            "Chưa đo tác động lên chẩn đoán production; không tự nâng quyền remediation.")
