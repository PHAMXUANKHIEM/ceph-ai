"""Conservative, read-only Block Storage inventory insights.

This module intentionally does not call an LLM and never creates an Action.
It turns bounded Ceph inventory, watcher/lock and persisted I/O evidence into
explainable advisory findings. Missing evidence is reported explicitly instead
of being interpreted as an unused or reclaimable volume.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping


ADVISORY_TTL_SECONDS = 15 * 60
DEFAULT_BACKUP_MAX_AGE_HOURS = 24


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
    lock_count: int | None
    last_io_at: str | None
    evidence_at: str
    evidence_gaps: tuple[str, ...] = ()
    clone_child_count: int | None = None
    has_parent: bool = False
    backup_state: str | None = None
    latest_backup_at: str | None = None


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
    except (TypeError, ValueError):
        return None


def _metric_summary(
    rows: Iterable[Mapping[str, object]], since: datetime,
) -> tuple[datetime | None, bool]:
    recent: list[tuple[datetime, float]] = []
    for row in rows:
        captured_at = _as_datetime(row.get("polled_at") or row.get("captured_at"))
        if captured_at is None or captured_at < since:
            continue
        try:
            iops = max(0.0, float(row.get("iops") or 0))
        except (TypeError, ValueError):
            iops = 0.0
        recent.append((captured_at, iops))
    if not recent:
        return None, False
    last_io = max((at for at, iops in recent if iops > 0), default=None)
    return last_io, True


def _backup_summary(
    rows: Iterable[Mapping[str, object]], captured: datetime, max_age_hours: int,
) -> tuple[str, datetime | None]:
    successful: list[datetime] = []
    for row in rows:
        if str(row.get("status") or "").upper() != "SUCCESS":
            continue
        finished_at = _as_datetime(row.get("finished_at") or row.get("created_at"))
        if finished_at is not None:
            successful.append(finished_at)
    if not successful:
        return "missing", None
    latest = max(successful)
    state = "protected" if latest >= captured - timedelta(hours=max(1, int(max_age_hours))) else "stale"
    return state, latest


def _advisory(payload: dict, *, captured_at: datetime, saving: int | None = None) -> dict:
    payload.update({
        "recommendation_mode": "ADVISORY",
        "read_only": True,
        "action_id": None,
        "expected_saving_bytes": max(0, int(saving)) if saving is not None else None,
        "impact": "Không thay đổi Ceph; operator phải review trước mọi mutation.",
        "ttl_seconds": ADVISORY_TTL_SECONDS,
        "evidence_expires_at": (
            captured_at + timedelta(seconds=ADVISORY_TTL_SECONDS)
        ).isoformat() + "Z",
    })
    return payload


def build_inventory_insights(
    inventory_rows: Iterable[Mapping[str, object]],
    metric_rows: Mapping[tuple[str, str], Iterable[Mapping[str, object]]],
    *,
    backup_rows: Mapping[tuple[str, str], Iterable[Mapping[str, object]]] | None = None,
    now: datetime | None = None,
    history_days: int = 7,
    backup_max_age_hours: int = DEFAULT_BACKUP_MAX_AGE_HOURS,
) -> list[dict]:
    """Build bounded stale/attachment findings from real evidence.

    ``STALE_UNATTACHED`` requires all of the following:
    - attachment state is explicitly idle;
    - watcher and lock counts are both known and zero;
    - recent persisted I/O samples exist; and
    - every recent sample has zero IOPS.

    A missing sample, watcher, lock or attachment signal becomes
    ``INSUFFICIENT_EVIDENCE``. Snapshot/clone and backup findings are emitted
    separately so an operator cannot mistake an idle image for a safe delete.
    """
    captured = (now or datetime.utcnow()).replace(tzinfo=None)
    days = max(1, int(history_days))
    since = captured - timedelta(days=days)
    findings: list[dict] = []

    for raw in inventory_rows:
        pool = str(raw.get("pool") or "")
        image = str(raw.get("name") or raw.get("image") or "")
        if not pool or not image:
            continue

        provisioned = _as_int(raw.get("provisioned_size") or raw.get("size"))
        used = _as_int(raw.get("used_size"))
        snapshots = _as_int(raw.get("snapshot_count"))
        watcher_value = raw.get("watcher_count")
        lock_value = raw.get("lock_count")
        watcher_count = _as_int(watcher_value) if watcher_value is not None else None
        lock_count = _as_int(lock_value) if lock_value is not None else None
        attachment = str(raw.get("attachment_state") or "unknown").lower()
        clone_child_value = raw.get("clone_child_count")
        clone_child_count = _as_int(clone_child_value) if clone_child_value is not None else None
        has_parent = bool(raw.get("has_parent") or raw.get("parent"))
        last_io, has_metrics = _metric_summary(metric_rows.get((pool, image), ()), since)
        gaps: list[str] = []
        if attachment == "unknown":
            gaps.append("attachment state unavailable")
        if watcher_count is None:
            gaps.append("watcher evidence unavailable")
        if lock_count is None:
            gaps.append("lock evidence unavailable")
        if not has_metrics:
            gaps.append(f"no I/O samples in the last {days} days")
        if raw.get("snapshot_evidence") is False:
            gaps.append("snapshot evidence unavailable")
        if raw.get("clone_evidence") is False:
            gaps.append("clone dependency evidence unavailable")
        backup_state = None
        latest_backup = None
        if backup_rows is not None:
            backup_state, latest_backup = _backup_summary(
                backup_rows.get((pool, image), ()), captured, backup_max_age_hours
            )
            if backup_state == "missing":
                gaps.append("no successful backup evidence")
            elif backup_state == "stale":
                gaps.append(f"last successful backup is older than {backup_max_age_hours} hours")

        common = {
            "pool": pool,
            "image": image,
            "provisioned_bytes": provisioned,
            "used_bytes": used,
            "snapshot_count": snapshots,
            "attachment_state": attachment,
            "watcher_count": watcher_count,
            "lock_count": lock_count,
            "clone_child_count": clone_child_count,
            "has_parent": has_parent,
            "backup_state": backup_state,
            "latest_backup_at": latest_backup.isoformat() + "Z" if latest_backup else None,
            "last_io_at": last_io.isoformat() + "Z" if last_io else None,
            "evidence_at": captured.isoformat() + "Z",
            "evidence_gaps": tuple(gaps),
        }

        if snapshots:
            findings.append(_advisory({
                **asdict(InventoryInsight(
                    **common,
                    kind="SNAPSHOT_DEPENDENCY",
                    confidence=1.0,
                    reason=f"Image còn {snapshots} snapshot; chưa thể coi toàn bộ dung lượng là thu hồi an toàn",
                    recommendation="Review snapshot retention và dependency trước khi xoá hoặc flatten",
                    estimated_reclaimable_bytes=0,
                )),
            }, captured_at=captured, saving=0))

        if clone_child_count is not None and clone_child_count > 0 or has_parent:
            findings.append(_advisory({
                **asdict(InventoryInsight(
                    **common,
                    kind="CLONE_DEPENDENCY",
                    confidence=1.0,
                    reason=(
                        f"Image có {clone_child_count} clone phụ thuộc" if clone_child_count
                        else "Image có parent snapshot/image dependency"
                    ),
                    recommendation="Review dependency graph trước khi flatten, xoá parent hoặc thay đổi image",
                    estimated_reclaimable_bytes=0,
                )),
            }, captured_at=captured, saving=0))

        if backup_rows is not None and backup_state in {"missing", "stale"}:
            findings.append(_advisory({
                **asdict(InventoryInsight(
                    **common,
                    kind="BACKUP_PROTECTION_GAP",
                    confidence=None,
                    reason=(
                        "Chưa có backup thành công được ghi nhận"
                        if backup_state == "missing"
                        else f"Backup thành công gần nhất đã quá {backup_max_age_hours} giờ"
                    ),
                    recommendation="Tạo hoặc xác minh backup ngoài cụm trước khi thực hiện mutation",
                    estimated_reclaimable_bytes=None,
                )),
            }, captured_at=captured))

        if (
            attachment == "idle"
            and watcher_count == 0
            and lock_count == 0
            and has_metrics
            and last_io is None
        ):
            findings.append(_advisory({
                **asdict(InventoryInsight(
                    **common,
                    kind="STALE_UNATTACHED",
                    confidence=0.9,
                    reason=(
                        f"Không có watcher/lock và không có I/O khác 0 trong {days} ngày"
                    ),
                    recommendation=(
                        "Review owner, Cinder attachment và backup trước khi quyết định retain/trash"
                    ),
                    estimated_reclaimable_bytes=0 if snapshots else provisioned,
                )),
            }, captured_at=captured, saving=0 if snapshots else provisioned))
            continue

        if not has_metrics or attachment == "unknown" or watcher_count is None or lock_count is None:
            findings.append(_advisory({
                **asdict(InventoryInsight(
                    **common,
                    kind="INSUFFICIENT_EVIDENCE",
                    confidence=None,
                    reason="Chưa đủ evidence để kết luận volume stale hoặc có thể thu hồi",
                    recommendation="Thu thập đủ attachment, watcher, lock và I/O evidence trước khi review",
                    estimated_reclaimable_bytes=None,
                )),
            }, captured_at=captured))

    order = {
        "STALE_UNATTACHED": 0,
        "SNAPSHOT_DEPENDENCY": 1,
        "CLONE_DEPENDENCY": 2,
        "BACKUP_PROTECTION_GAP": 3,
        "INSUFFICIENT_EVIDENCE": 4,
    }
    return sorted(findings, key=lambda row: (order.get(row["kind"], 9), row["pool"], row["image"]))
