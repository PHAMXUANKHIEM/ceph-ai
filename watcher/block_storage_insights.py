"""Read-only RBD inventory signals for the block-storage roadmap.

This module deliberately does not call an LLM and does not propose an
execution-ready action.  It combines Ceph inventory metadata with persisted
I/O evidence and returns only bounded, explainable candidates.  Missing I/O or
attachment evidence is reported as insufficient evidence rather than guessed
to be stale.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import json
from typing import Iterable, Mapping

from sqlalchemy import delete

from shared import db
from shared.models import VolumeDependencySnapshot


@dataclass(frozen=True)
class InventoryInsight:
    pool: str
    image: str
    kind: str
    confidence: float | None
    reason: str
    recommendation: str
    provisioned_bytes: int
    used_bytes: int
    snapshot_count: int
    estimated_reclaimable_bytes: int | None
    attachment_state: str
    watcher_count: int | None
    last_io_at: str | None
    evidence_at: str
    evidence_gaps: tuple[str, ...] = ()


def _as_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _metric_summary(rows: Iterable[Mapping[str, object]], since: datetime) -> tuple[datetime | None, bool]:
    recent = []
    for row in rows:
        captured_at = _as_datetime(row.get("polled_at") or row.get("captured_at"))
        if captured_at is None or captured_at < since:
            continue
        recent.append((captured_at, float(row.get("iops") or 0)))
    if not recent:
        return None, False
    last_io = max((at for at, iops in recent if iops > 0), default=None)
    return last_io, bool(recent)


def build_inventory_insights(
    inventory_rows: Iterable[Mapping[str, object]],
    metric_rows: Mapping[tuple[str, str], Iterable[Mapping[str, object]]],
    *,
    now: datetime | None = None,
    history_days: int = 7,
    policy_keys: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Return conservative stale/snapshot candidates from real evidence.

    A volume is marked ``STALE_UNATTACHED`` only when Ceph reports no watcher
    and the application has recent metric samples with zero I/O.  A volume
    with no metric history is ``INSUFFICIENT_EVIDENCE`` and is never counted
    as reclaimable.  Snapshot candidates are advisory only and remain
    protected when a snapshot policy exists.
    """

    now = (now or datetime.utcnow()).replace(tzinfo=None)
    since = now - timedelta(days=max(1, int(history_days)))
    policy_keys = policy_keys or set()
    result: list[dict] = []

    for raw in inventory_rows:
        pool = str(raw.get("pool") or "")
        image = str(raw.get("name") or raw.get("image") or "")
        if not pool or not image:
            continue
        key = (pool, image)
        provisioned = _as_int(raw.get("provisioned_size") or raw.get("size"))
        used = _as_int(raw.get("used_size"))
        snapshots = _as_int(raw.get("snapshot_count"))
        watcher_value = raw.get("watcher_count")
        watcher_count = _as_int(watcher_value) if watcher_value is not None else None
        attachment = str(raw.get("attachment_state") or "unknown").lower()
        last_io, has_metrics = _metric_summary(metric_rows.get(key, ()), since)
        policy = key in policy_keys
        gaps: list[str] = []

        if attachment == "unknown" or watcher_count is None:
            gaps.append("attachment evidence unavailable")
        if not has_metrics:
            gaps.append(f"no I/O samples in the last {max(1, int(history_days))} days")

        if attachment == "idle" and watcher_count == 0 and has_metrics and last_io is None:
            reclaimable = 0 if snapshots or policy else provisioned
            reason = (
                f"Ceph reports no watcher and no non-zero I/O sample in the last "
                f"{max(1, int(history_days))} days"
            )
            if snapshots:
                reason += f"; {snapshots} snapshot(s) still protect the image"
            result.append(asdict(InventoryInsight(
                pool=pool, image=image, kind="STALE_UNATTACHED", confidence=0.9,
                reason=reason,
                recommendation="Review owner and backup status before any retain/trash decision",
                provisioned_bytes=provisioned, used_bytes=used,
                snapshot_count=snapshots, estimated_reclaimable_bytes=reclaimable,
                attachment_state=attachment, watcher_count=watcher_count,
                last_io_at=None, evidence_at=now.isoformat() + "Z",
                evidence_gaps=tuple(gaps),
            )))
        elif snapshots and not policy:
            result.append(asdict(InventoryInsight(
                pool=pool, image=image, kind="SNAPSHOT_REVIEW", confidence=0.85,
                reason=f"{snapshots} snapshot(s) detected without a configured snapshot policy",
                recommendation="Review snapshot retention and clone dependencies; do not delete automatically",
                provisioned_bytes=provisioned, used_bytes=used,
                snapshot_count=snapshots, estimated_reclaimable_bytes=None,
                attachment_state=attachment, watcher_count=watcher_count,
                last_io_at=last_io.isoformat() + "Z" if last_io else None,
                evidence_at=now.isoformat() + "Z", evidence_gaps=tuple(gaps),
            )))
        elif not has_metrics or attachment == "unknown":
            result.append(asdict(InventoryInsight(
                pool=pool, image=image, kind="INSUFFICIENT_EVIDENCE", confidence=None,
                reason="Không đủ bằng chứng để kết luận volume stale hoặc có thể thu hồi",
                recommendation="Collect attachment and I/O evidence before making an inventory recommendation",
                provisioned_bytes=provisioned, used_bytes=used,
                snapshot_count=snapshots, estimated_reclaimable_bytes=None,
                attachment_state=attachment, watcher_count=watcher_count,
                last_io_at=last_io.isoformat() + "Z" if last_io else None,
                evidence_at=now.isoformat() + "Z", evidence_gaps=tuple(gaps),
            )))

    order = {"STALE_UNATTACHED": 0, "SNAPSHOT_REVIEW": 1, "INSUFFICIENT_EVIDENCE": 2}
    return sorted(result, key=lambda row: (order.get(row["kind"], 9), row["pool"], row["image"]))


def build_snapshot_clone_insights(
    detail_rows: Iterable[Mapping[str, object]],
    *,
    policy_keys: set[tuple[str, str]] | None = None,
    evidence_at: datetime | None = None,
) -> list[dict]:
    """Turn bounded RBD detail responses into dependency warnings.

    ``rbd snap ls`` and ``rbd children`` are authoritative for this report.
    Partial command errors therefore produce ``INSUFFICIENT_EVIDENCE`` rather
    than a false claim that a snapshot or clone dependency is absent.
    """

    policy_keys = policy_keys or set()
    captured = (evidence_at or datetime.utcnow()).replace(tzinfo=None).isoformat() + "Z"
    result: list[dict] = []
    for detail in detail_rows:
        pool = str(detail.get("pool") or "")
        image = str(detail.get("name") or detail.get("image") or "")
        if not pool or not image:
            continue
        key = (pool, image)
        errors = detail.get("partial_errors")
        errors = errors if isinstance(errors, Mapping) else {}
        snapshots = detail.get("snapshots")
        children = detail.get("children")
        parent = detail.get("parent")
        if not isinstance(snapshots, list):
            snapshots = []
        if not isinstance(children, list):
            children = []
        dependency = bool(parent or children)
        snapshot_count = len(snapshots)
        if errors.get("snapshots") or errors.get("children"):
            result.append({
                "pool": pool, "image": image, "kind": "INSUFFICIENT_EVIDENCE",
                "confidence": None,
                "reason": "Không đọc đủ snapshot/clone dependency từ RBD",
                "recommendation": "Retry read-only inventory before flatten, delete, or retention changes",
                "snapshot_count": snapshot_count, "parent": parent,
                "children": children, "evidence_at": captured,
                "evidence_gaps": sorted(str(name) for name in errors if name in {"snapshots", "children"}),
            })
            continue
        if snapshot_count and key not in policy_keys:
            result.append({
                "pool": pool, "image": image, "kind": "SNAPSHOT_RETENTION_GAP",
                "confidence": 0.95,
                "reason": f"Có {snapshot_count} snapshot nhưng chưa có snapshot policy trong ceph-ai",
                "recommendation": "Review retention and protection before deleting or flattening",
                "snapshot_count": snapshot_count, "parent": parent,
                "children": children, "evidence_at": captured,
                "evidence_gaps": ["Cinder/tenant ownership is not part of this RBD response"],
            })
        if parent:
            result.append({
                "pool": pool, "image": image, "kind": "CLONE_CHILD",
                "confidence": 0.95,
                "reason": f"Volume phụ thuộc parent clone {parent}",
                "recommendation": "Không flatten/xóa parent trước khi kiểm tra child dependency",
                "snapshot_count": snapshot_count, "parent": parent,
                "children": children, "evidence_at": captured,
                "evidence_gaps": [],
            })
        if children:
            result.append({
                "pool": pool, "image": image, "kind": "CLONE_PARENT",
                "confidence": 0.95,
                "reason": f"Có {len(children)} clone child đang phụ thuộc volume này",
                "recommendation": "Resolve child dependencies before deleting or flattening this image",
                "snapshot_count": snapshot_count, "parent": parent,
                "children": children, "evidence_at": captured,
                "evidence_gaps": [],
            })
        if not snapshot_count and not dependency and not errors:
            continue
    order = {"INSUFFICIENT_EVIDENCE": 0, "CLONE_PARENT": 1, "CLONE_CHILD": 2, "SNAPSHOT_RETENTION_GAP": 3}
    return sorted(result, key=lambda row: (order.get(row["kind"], 9), row["pool"], row["image"], row["kind"]))


def build_protection_gap_insights(
    inventory_rows: Iterable[Mapping[str, object]],
    backup_rows: Mapping[tuple[str, str], Iterable[Mapping[str, object]]],
    *,
    policy_keys: set[tuple[str, str]] | None = None,
    restore_drill_rows: Iterable[Mapping[str, object]] = (),
    now: datetime | None = None,
    backup_max_age_hours: int = 24,
    restore_drill_max_age_hours: int = 192,
    backup_history_available: bool = True,
) -> list[dict]:
    """Find conservative, read-only backup and restore protection gaps.

    ``BackupJob`` is application history, not proof that an external target
    still contains the artifact.  The output therefore keeps that limitation
    in ``evidence_gaps`` and never turns a missing row into an execution
    action.  RestoreDrill is currently cluster-scoped; at most one finding is
    emitted for it instead of pretending that a global drill covers every
    image individually.
    """

    now = (now or datetime.utcnow()).replace(tzinfo=None)
    policy_keys = policy_keys or set()
    backup_max_age_hours = max(1, int(backup_max_age_hours))
    restore_drill_max_age_hours = max(1, int(restore_drill_max_age_hours))
    result: list[dict] = []
    inventory = [row for row in inventory_rows if isinstance(row, Mapping)]

    def job_time(row: Mapping[str, object]) -> datetime | None:
        return _as_datetime(row.get("finished_at") or row.get("created_at"))

    def finding(
        *, pool: str, image: str, kind: str, confidence: float | None,
        reason: str, recommendation: str, snapshot_count: int,
        snapshot_policy_enabled: bool, last_success_at: datetime | None,
        last_failure_at: datetime | None, evidence_gaps: list[str],
        scope: str = "volume",
    ) -> dict:
        return {
            "pool": pool,
            "image": image,
            "scope": scope,
            "kind": kind,
            "confidence": confidence,
            "reason": reason,
            "recommendation": recommendation,
            "snapshot_count": snapshot_count,
            "snapshot_policy_enabled": snapshot_policy_enabled,
            "last_success_at": last_success_at.isoformat() + "Z" if last_success_at else None,
            "last_failure_at": last_failure_at.isoformat() + "Z" if last_failure_at else None,
            "evidence_at": now.isoformat() + "Z",
            "evidence_gaps": evidence_gaps,
        }

    for raw in inventory:
        pool = str(raw.get("pool") or "")
        image = str(raw.get("name") or raw.get("image") or "")
        if not pool or not image:
            continue
        key = (pool, image)
        snapshot_count = _as_int(raw.get("snapshot_count"))
        has_policy = key in policy_keys
        entries = [row for row in (backup_rows.get(key, ()) or ()) if isinstance(row, Mapping)]
        eligible = [
            row for row in entries
            if str(row.get("job_type") or "").lower() in {"full", "incremental"}
        ]
        successful = [row for row in eligible if str(row.get("status") or "").upper() == "SUCCESS"]
        failures = [row for row in eligible if str(row.get("status") or "").upper() == "FAILED"]
        latest_success = max(successful, key=lambda row: job_time(row) or datetime.min, default=None)
        latest_failure = max(failures, key=lambda row: job_time(row) or datetime.min, default=None)
        success_at = job_time(latest_success) if latest_success else None
        failure_at = job_time(latest_failure) if latest_failure else None
        evidence_gaps = [
            "BackupJob chỉ phản ánh các job backup được ghi nhận trong ceph-ai; chưa xác minh artifact ở target ngoài.",
        ]
        if has_policy:
            evidence_gaps.append("Snapshot policy không thay thế cho backup độc lập.")

        if not backup_history_available:
            result.append(finding(
                pool=pool, image=image, kind="INSUFFICIENT_EVIDENCE", confidence=None,
                reason="Không truy cập được lịch sử backup để đánh giá protection gap",
                recommendation="Retry read-only backup history before changing retention or protection settings",
                snapshot_count=snapshot_count, snapshot_policy_enabled=has_policy,
                last_success_at=success_at, last_failure_at=failure_at,
                evidence_gaps=evidence_gaps + ["Backup history query failed"],
            ))
            continue

        if not successful:
            kind = "BACKUP_FAILED" if failures else "NO_SUCCESSFUL_BACKUP"
            reason = (
                "Backup gần nhất thất bại và chưa có backup thành công mới hơn"
                if failures else "Chưa có backup full/incremental thành công được ghi nhận"
            )
            result.append(finding(
                pool=pool, image=image, kind=kind, confidence=0.92,
                reason=reason,
                recommendation="Verify backup target, schedule, and owner before considering this volume protected",
                snapshot_count=snapshot_count, snapshot_policy_enabled=has_policy,
                last_success_at=success_at, last_failure_at=failure_at,
                evidence_gaps=evidence_gaps,
            ))
        elif failure_at and success_at and failure_at > success_at:
            result.append(finding(
                pool=pool, image=image, kind="BACKUP_FAILED", confidence=0.94,
                reason="Có backup thất bại mới hơn lần backup thành công gần nhất",
                recommendation="Review the failed backup and confirm a newer successful copy before treating the volume as protected",
                snapshot_count=snapshot_count, snapshot_policy_enabled=has_policy,
                last_success_at=success_at, last_failure_at=failure_at,
                evidence_gaps=evidence_gaps,
            ))
        elif success_at is None or (now - success_at).total_seconds() > backup_max_age_hours * 3600:
            age_hours = None if success_at is None else max(0, int((now - success_at).total_seconds() // 3600))
            result.append(finding(
                pool=pool, image=image, kind="BACKUP_STALE", confidence=0.9,
                reason=(
                    f"Backup thành công gần nhất đã cách {age_hours} giờ, vượt ngưỡng {backup_max_age_hours} giờ"
                    if age_hours is not None else "Không xác định được thời điểm backup thành công gần nhất"
                ),
                recommendation="Run or schedule a backup after verifying target capacity and recent job failures",
                snapshot_count=snapshot_count, snapshot_policy_enabled=has_policy,
                last_success_at=success_at, last_failure_at=failure_at,
                evidence_gaps=evidence_gaps,
            ))

    drills = [row for row in restore_drill_rows if isinstance(row, Mapping)]
    latest_drill = max(drills, key=lambda row: job_time(row) or datetime.min, default=None)
    drill_at = job_time(latest_drill) if latest_drill else None
    drill_status = str(latest_drill.get("status") or "").upper() if latest_drill else ""
    drill_stale = drill_at is None or (now - drill_at).total_seconds() > restore_drill_max_age_hours * 3600
    if latest_drill is None or drill_status != "SUCCESS" or drill_stale:
        first = inventory[0] if inventory else {}
        drill_pool = str((latest_drill or {}).get("pool") or first.get("pool") or "")
        result.append(finding(
            pool=drill_pool, image=str((latest_drill or {}).get("image") or "cluster"),
            scope="cluster", kind="RESTORE_DRILL_GAP", confidence=0.75 if latest_drill else None,
            reason=(
                "Restore drill gần nhất chưa thành công"
                if latest_drill and drill_status != "SUCCESS" else
                f"Chưa có restore drill thành công trong {restore_drill_max_age_hours} giờ"
            ),
            recommendation="Run a controlled restore drill and record its result before relying on backup recovery",
            snapshot_count=0, snapshot_policy_enabled=False,
            last_success_at=drill_at if drill_status == "SUCCESS" else None,
            last_failure_at=drill_at if drill_status == "FAILED" else None,
            evidence_gaps=["RestoreDrill hiện là bằng chứng cấp cluster, chưa map coverage theo từng volume"],
        ))

    order = {
        "INSUFFICIENT_EVIDENCE": 0, "NO_SUCCESSFUL_BACKUP": 1,
        "BACKUP_FAILED": 2, "BACKUP_STALE": 3, "RESTORE_DRILL_GAP": 4,
    }
    return sorted(result, key=lambda row: (order.get(row["kind"], 9), row["pool"], row["image"]))


def persist_dependency_snapshots(
    cluster_id: str,
    detail_rows: Iterable[Mapping[str, object]],
    *,
    captured_at: datetime | None = None,
    retention_days: int = 30,
) -> int:
    """Persist one bounded observation per queried image and prune old rows."""

    captured = (captured_at or datetime.utcnow()).replace(tzinfo=None)
    rows = []
    for detail in detail_rows:
        pool = str(detail.get("pool") or "")
        image = str(detail.get("name") or detail.get("image") or "")
        if not pool or not image:
            continue
        snapshots = detail.get("snapshots") if isinstance(detail.get("snapshots"), list) else []
        children = detail.get("children") if isinstance(detail.get("children"), list) else []
        errors = detail.get("partial_errors") if isinstance(detail.get("partial_errors"), Mapping) else {}
        rows.append(VolumeDependencySnapshot(
            cluster_id=cluster_id, pool=pool, image=image,
            snapshot_count=len(snapshots),
            parent_json=json.dumps(detail.get("parent"), ensure_ascii=False, sort_keys=True),
            children_json=json.dumps(children, ensure_ascii=False, sort_keys=True),
            partial_errors_json=json.dumps(errors, ensure_ascii=False, sort_keys=True),
            captured_at=captured,
        ))
    cutoff = captured - timedelta(days=max(1, int(retention_days)))
    with db.SessionLocal() as session:
        if rows:
            session.add_all(rows)
        session.execute(delete(VolumeDependencySnapshot).where(
            VolumeDependencySnapshot.cluster_id == cluster_id,
            VolumeDependencySnapshot.captured_at < cutoff,
        ))
        session.commit()
    return len(rows)
