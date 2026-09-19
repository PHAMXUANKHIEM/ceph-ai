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
from typing import Iterable, Mapping


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
