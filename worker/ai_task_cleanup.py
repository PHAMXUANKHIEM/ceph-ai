"""Remove old completed user AI task records without touching active work."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import settings


TASK_ROOT = Path("/var/lib/ceph-ai/ai-tasks")
TERMINAL_STATUSES = {
    "PUSHED", "COMMITTED", "STAGING_VERIFIED", "PROMOTED",
    "FAILED", "FAILED_STALE", "SKIPPED_DUPLICATE",
}
TASK_ID_RE = re.compile(r"[0-9a-f-]{36}")
TASK_MAX_RUNTIME_SECONDS = 2 * 60 * 60 + 5 * 60


def _metadata(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _created_at(value: dict, fallback: float) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value.get("created_at")))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.fromtimestamp(fallback, timezone.utc)


def cleanup(*, root: Path = TASK_ROOT, retention_days: int | None = None, max_records: int | None = None) -> list[str]:
    """Delete only validated, terminal task directories past retention limits."""
    if not root.is_dir():
        return []
    age_limit = datetime.now(timezone.utc) - timedelta(days=retention_days or settings.ai_task_retention_days)
    limit = max_records or settings.ai_task_max_records
    terminal: list[tuple[datetime, Path]] = []
    for directory in root.iterdir():
        if not directory.is_dir() or directory.is_symlink() or not TASK_ID_RE.fullmatch(directory.name):
            continue
        metadata_path = directory / "task.json"
        if not metadata_path.is_file():
            continue
        metadata = _metadata(metadata_path)
        terminal_status = metadata.get("status") in TERMINAL_STATUSES
        if not terminal_status:
            state = _metadata(directory / "state.json")
            attempts = state.get("attempts") if isinstance(state.get("attempts"), dict) else {}
            latest = max(
                (item for item in attempts.values() if isinstance(item, dict)),
                key=lambda item: str(item.get("finished_at") or ""),
                default={},
            )
            terminal_status = latest.get("status") in TERMINAL_STATUSES
            if not terminal_status:
                started_at = latest.get("started_at") or metadata.get("created_at")
                try:
                    started = datetime.fromisoformat(str(started_at))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    terminal_status = (
                        datetime.now(timezone.utc) - started
                    ).total_seconds() > TASK_MAX_RUNTIME_SECONDS
                except (TypeError, ValueError):
                    terminal_status = False
            if not terminal_status:
                continue
        terminal.append((_created_at(metadata, directory.stat().st_mtime), directory))

    terminal.sort(key=lambda item: item[0], reverse=True)
    candidates = [item for item in terminal if item[0] < age_limit]
    candidates.extend(terminal[limit:])
    removed: list[str] = []
    for _created, directory in sorted({(created, path) for created, path in candidates}, key=lambda item: item[0]):
        if directory.parent != root or not TASK_ID_RE.fullmatch(directory.name):
            continue
        shutil.rmtree(directory)
        removed.append(directory.name)
    return removed


if __name__ == "__main__":
    for task_id in cleanup():
        print(task_id)
