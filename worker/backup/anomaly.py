"""Anomaly detection heuristic (Story 9.5, PRD FR-15) — runs BEFORE any AI
call, on every SUCCESSFUL BackupJob, comparing duration/size against that
same image's own history using a stddev-based threshold from
`backup_policy.yaml` (never a hardcoded absolute number). A plain
statistical check, not an AI call — AI is only invoked when this returns
non-None or the job itself failed (AC #8), keeping cost/noise down (NFR10,
kế thừa từ PRD gốc).
"""

from __future__ import annotations

import statistics

from shared import db
from shared.models import BackupJob
from worker.backup.policy_config import load_backup_policy

MIN_HISTORY_SIZE = 30
DEFAULT_THRESHOLD_STDDEV = 3


def _history(pool: str, image: str, job_type: str, exclude_id: str, cluster_id: str | None) -> list[BackupJob]:
    with db.SessionLocal() as session:
        return (
            session.query(BackupJob)
            .filter(
                BackupJob.pool == pool,
                BackupJob.image == image,
                BackupJob.cluster_id == cluster_id,
                BackupJob.job_type == job_type,
                BackupJob.status == "SUCCESS",
                BackupJob.id != exclude_id,
            )
            .order_by(BackupJob.created_at.desc())
            .limit(MIN_HISTORY_SIZE)
            .all()
        )


def check_anomaly(job: BackupJob) -> dict | None:
    """Returns `{"kind": "duration"|"size", "details": str}` if `job`
    deviates from its own image's history by more than the configured
    threshold, else `None`. Requires >= MIN_HISTORY_SIZE prior successful
    runs of the SAME (pool, image, job_type) — insufficient history means
    "can't judge yet", not "normal"."""
    if job.status != "SUCCESS" or job.pool is None or job.image is None:
        return None  # metadata jobs (pool=None) have no per-image baseline to compare against

    history = _history(job.pool, job.image, job.job_type, job.id, job.cluster_id)
    if len(history) < MIN_HISTORY_SIZE:
        return None

    threshold = load_backup_policy().get("anomaly_threshold_stddev") or DEFAULT_THRESHOLD_STDDEV

    for attr, kind, label_vi in (
        ("duration_seconds", "duration", "Thời gian chạy"),
        ("size_bytes", "size", "Kích thước"),
    ):
        values = [getattr(h, attr) for h in history if getattr(h, attr) is not None]
        current = getattr(job, attr)
        if current is None or len(values) < MIN_HISTORY_SIZE:
            continue
        mean = statistics.mean(values)
        stdev = statistics.pstdev(values)
        if stdev == 0:
            continue
        deviation = abs(current - mean) / stdev
        if deviation >= threshold:
            return {
                "kind": kind,
                "details": (
                    f"{label_vi} của job này ({current:.1f}) lệch {deviation:.1f} độ lệch chuẩn so với "
                    f"trung bình {len(values)} lần chạy gần nhất ({mean:.1f})"
                ),
            }
    return None
