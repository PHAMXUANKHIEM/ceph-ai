"""Bounded read-only RBD volume/snapshot evidence for capacity forecasting."""

from __future__ import annotations

import logging
import shlex
from datetime import datetime, timedelta, timezone
from sqlalchemy import func

from shared import db
from shared.models import RbdCapacitySample
from watcher import ceph_client
from watcher.capacity_evidence import _query

logger = logging.getLogger(__name__)
MAX_POOLS_PER_TICK = 4
MAX_RBD_ROWS = 5000
MAX_HISTORY_DAYS = 90
MAX_EVIDENCE_AGE = timedelta(hours=2)


def _nonnegative(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def summarize_rbd_du(payload: object) -> dict | None:
    """Separate image heads from snapshots; unknown usage stays unknown."""
    rows = payload.get("images") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or len(rows) > MAX_RBD_ROWS:
        return None
    images: set[str] = set()
    provisioned = head_used = snapshot_used = snapshots = 0
    head_usage_known = snapshot_usage_known = True
    for row in rows:
        if not isinstance(row, dict):
            return None
        name = row.get("name") or row.get("image")
        if not isinstance(name, str) or not name.strip():
            return None
        used = _nonnegative(row.get("used_size"))
        if row.get("snapshot") is not None:
            snapshots += 1
            if used is None:
                snapshot_usage_known = False
            else:
                snapshot_used += used
            continue
        if name in images:
            return None
        images.add(name)
        size = _nonnegative(row.get("provisioned_size", row.get("size")))
        if size is None:
            return None
        provisioned += size
        if used is None:
            head_usage_known = False
        else:
            head_used += used
    return {
        "image_count": len(images),
        "snapshot_count": snapshots,
        "provisioned_bytes": provisioned,
        "head_used_bytes": head_used if head_usage_known else None,
        "snapshot_used_bytes": snapshot_used if snapshot_usage_known else None,
    }


def _configured_pools(cluster) -> list[str]:
    if cluster is None or cluster.is_default:
        candidates = ceph_client.configured_rbd_pools()
    else:
        candidates = [entry.strip().split("/", 1)[0] for entry in (cluster.backup_tracked_images or "").split(",")]
    return sorted({pool for pool in candidates if pool and len(pool) <= 128})


def collect_and_store(cluster_id: str, cluster, pool_stats: list[dict], *, now: datetime) -> dict:
    """Collect a bounded configured-pool subset; failures never invent zeros."""
    available = {row["pool"]: row for row in pool_stats if isinstance(row, dict) and row.get("pool")}
    selected = [pool for pool in _configured_pools(cluster) if pool in available]
    selected, skipped = selected[:MAX_POOLS_PER_TICK], max(0, len(selected) - MAX_POOLS_PER_TICK)
    stored = 0
    gaps = []
    if skipped:
        gaps.append(f"{skipped} configured RBD pools skipped by scan limit")
    if not selected:
        gaps.append("no configured RBD pool in current ceph df sample")
    for pool in selected:
        try:
            summary = summarize_rbd_du(_query(cluster, f"rbd du --pool {shlex.quote(pool)}"))
            if summary is None:
                gaps.append(f"{pool}: invalid or oversized rbd du payload")
                continue
            physical = _nonnegative(available[pool].get("used_bytes"))
            with db.SessionLocal() as session:
                session.add(RbdCapacitySample(
                    cluster_id=cluster_id, pool=pool, captured_at=now,
                    physical_pool_used_bytes=physical, **summary,
                ))
                session.commit()
            stored += 1
        except Exception:
            logger.exception("RBD capacity evidence collection failed for cluster %s pool %s", cluster_id, pool)
            gaps.append(f"{pool}: RBD usage unavailable")
    return {"stored": stored, "selected": len(selected), "skipped": skipped, "gaps": gaps}


def evidence(cluster_id: str, *, now: datetime) -> dict:
    """Return latest pool attribution and observed growth, never extrapolated sizes."""
    cutoff = now - timedelta(days=MAX_HISTORY_DAYS)
    with db.SessionLocal() as session:
        ranked = session.query(
            RbdCapacitySample.id.label("sample_id"),
            func.row_number().over(
                partition_by=RbdCapacitySample.pool,
                order_by=(RbdCapacitySample.captured_at.desc(), RbdCapacitySample.id.desc()),
            ).label("sample_rank"),
        ).filter(
            RbdCapacitySample.cluster_id == cluster_id,
            RbdCapacitySample.captured_at >= cutoff,
            RbdCapacitySample.captured_at <= now,
        ).subquery()
        rows = session.query(RbdCapacitySample).filter(
            RbdCapacitySample.id == ranked.c.sample_id,
            ranked.c.sample_rank <= 2,
        ).order_by(RbdCapacitySample.pool, RbdCapacitySample.captured_at.desc()).all()
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            series = grouped.setdefault(row.pool, [])
            if len(series) < 2:
                series.append({
                    "captured_at": row.captured_at, "image_count": row.image_count,
                    "snapshot_count": row.snapshot_count, "provisioned_bytes": row.provisioned_bytes,
                    "head_used_bytes": row.head_used_bytes,
                    "snapshot_used_bytes": row.snapshot_used_bytes,
                    "physical_pool_used_bytes": row.physical_pool_used_bytes,
                })
    result = []
    for pool, series in grouped.items():
        latest = series[0]
        age = max(0, (now - latest["captured_at"]).total_seconds())
        previous = series[1] if len(series) > 1 else None
        elapsed_days = (latest["captured_at"] - previous["captured_at"]).total_seconds() / 86400 if previous else 0
        growth = None
        if previous and elapsed_days > 0:
            growth = {
                "provisioned_bytes_per_day": round((latest["provisioned_bytes"] - previous["provisioned_bytes"]) / elapsed_days),
                "snapshot_count_per_day": round((latest["snapshot_count"] - previous["snapshot_count"]) / elapsed_days, 3),
                "snapshot_used_bytes_per_day": (
                    round((latest["snapshot_used_bytes"] - previous["snapshot_used_bytes"]) / elapsed_days)
                    if latest["snapshot_used_bytes"] is not None and previous["snapshot_used_bytes"] is not None else None
                ),
            }
        result.append({
            "pool": pool,
            "captured_at": latest["captured_at"].replace(tzinfo=timezone.utc).isoformat(),
            "age_seconds": round(age),
            "stale": age > MAX_EVIDENCE_AGE.total_seconds(),
            "image_count": latest["image_count"],
            "snapshot_count": latest["snapshot_count"],
            "provisioned_bytes": latest["provisioned_bytes"],
            "head_used_bytes": latest["head_used_bytes"],
            "snapshot_used_bytes": latest["snapshot_used_bytes"],
            "physical_pool_used_bytes": latest["physical_pool_used_bytes"],
            "thin_provisioned_to_physical_used_ratio": (
                round(latest["provisioned_bytes"] / latest["physical_pool_used_bytes"], 4)
                if latest["physical_pool_used_bytes"] else None
            ),
            "observed_growth": growth,
        })
    return {
        "status": "ready" if result and all(not row["stale"] for row in result) else "insufficient_evidence",
        "source": "rbd du + ceph df detail",
        "scope": "configured_rbd_pools_only",
        "pools": result,
        "gaps": ([f"{row['pool']}: RBD capacity evidence is stale" for row in result if row["stale"]]
                 if result else ["no RBD capacity samples"]),
    }
