"""Conservative backup capacity estimates and target preflight data."""

from __future__ import annotations

import re
import shutil
import tempfile
from datetime import timedelta

from shared import db
from shared.models import BackupJob
from shared.time import utc_now
from worker.backup.cluster_scope import resolve_targets

_SAFE_ERROR = re.compile(r"(?i)(password|token|secret|access[_ -]?key|private[_ -]?key)\s*[:=]\s*\S+")


def _safe_error(exc: Exception) -> str:
    return _SAFE_ERROR.sub(lambda match: match.group(1) + "=[redacted]", str(exc))[:300]


def _scope(column, cluster):
    if cluster is None:
        return column.is_(None)
    return column.is_(None) if cluster.is_default else column == cluster.id


def _round_bytes(value: int | float | None) -> int | None:
    return None if value is None else max(0, int(value))


def overview(cluster=None, *, now=None, window_days: int = 30) -> dict:
    """Return target capacity, observed growth and conservative forecasts.

    S3 commonly cannot expose bucket capacity, so unknown capacity is a valid
    result and never becomes a false healthy/failed signal.
    """
    now = now or utc_now()
    window_days = max(1, min(int(window_days), 365))
    cutoff = now - timedelta(days=window_days)
    try:
        bindings = resolve_targets(cluster)
    except Exception as exc:
        return {"window_days": window_days, "temp": _temp_capacity(), "targets": [], "error": _safe_error(exc)}

    rows = []
    with db.SessionLocal() as session:
        for slot, backend in bindings:
            successful = (
                session.query(BackupJob)
                .filter(
                    BackupJob.backup_target_slot == slot,
                    BackupJob.job_type.in_(("full", "incremental")),
                    BackupJob.status == "SUCCESS",
                    BackupJob.created_at >= cutoff,
                    _scope(BackupJob.cluster_id, cluster),
                )
                .all()
            )
            all_successful = (
                session.query(BackupJob.size_bytes)
                .filter(
                    BackupJob.backup_target_slot == slot,
                    BackupJob.job_type.in_(("full", "incremental")),
                    BackupJob.status == "SUCCESS",
                    _scope(BackupJob.cluster_id, cluster),
                )
                .all()
            )
            recorded_bytes = sum(int(row[0] or 0) for row in all_successful)
            window_bytes = sum(int(row.size_bytes or 0) for row in successful)
            daily_growth = window_bytes / window_days
            latest_full = max(
                (int(row.size_bytes or 0) for row in successful if row.job_type == "full"),
                default=0,
            )
            try:
                metadata = backend.probe_metadata()
                metadata = metadata if isinstance(metadata, dict) else {}
                probe_error = None
            except Exception as exc:
                metadata = {}
                probe_error = _safe_error(exc)
            capacity_bytes = _round_bytes(metadata.get("capacity_bytes"))
            free_bytes = _round_bytes(metadata.get("free_bytes"))
            days_until_full = free_bytes / daily_growth if free_bytes is not None and daily_growth > 0 else None
            warning = bool(
                (capacity_bytes and free_bytes is not None and free_bytes <= capacity_bytes * 0.10)
                or (days_until_full is not None and days_until_full < 7)
            )
            rows.append({
                "slot": slot,
                "transport": metadata.get("transport") or metadata.get("endpoint") or None,
                "capacity_bytes": capacity_bytes,
                "free_bytes": free_bytes,
                "recorded_bytes": recorded_bytes,
                "window_bytes": window_bytes,
                "daily_growth_bytes": round(daily_growth, 2),
                "next_full_estimate_bytes": latest_full or None,
                "days_until_full": round(days_until_full, 1) if days_until_full is not None else None,
                "status": "unknown" if probe_error or free_bytes is None else "warning" if warning else "healthy",
                "probe_error": probe_error,
            })
    return {"window_days": window_days, "temp": _temp_capacity(), "targets": rows}


def _temp_capacity() -> dict:
    usage = shutil.disk_usage(tempfile.gettempdir())
    return {"path": tempfile.gettempdir(), "capacity_bytes": usage.total, "free_bytes": usage.free}
