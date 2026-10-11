"""Staging first: a production upgrade needs the same version proven on the staging cluster.

Plan/in-progress/croit-parity-operations-plan-2026-10-10.md, C4. croit ships
tested release channels; Ceph AI's equivalent is CS-STAGING. Before Ceph AI
proposes upgrading a production cluster to version X, the staging cluster
(an active cluster with ``autonomy_environment = lab``) must have run X on
every daemon, without interruption, for at least SOAK_HOURS, and be
HEALTH_OK now. The evidence is the Watcher's ``ceph versions`` history
(``cluster_capability_inventory``) and its stored health snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shared.models import Cluster, ClusterCapabilityInventory
from shared.time import utc_now

SOAK_HOURS = 24


@dataclass(frozen=True)
class SoakResult:
    ok: bool
    reason: str
    staging_name: str | None = None
    hours: float = 0.0

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason, "staging_name": self.staging_name,
                "hours": round(self.hours, 1), "required_hours": SOAK_HOURS}


def staging_cluster(session, *, excluding: str | None = None) -> Cluster | None:
    query = session.query(Cluster).filter(Cluster.is_active.is_(True), Cluster.autonomy_environment == "lab")
    if excluding:
        query = query.filter(Cluster.id != excluding)
    return query.order_by(Cluster.name).first()


def _soaked_since(session, cluster_id: str, target_version: str) -> datetime | None:
    """Start of the latest unbroken run of scans that saw ``target_version`` on every daemon."""
    rows = (session.query(ClusterCapabilityInventory)
            .filter(ClusterCapabilityInventory.cluster_id == cluster_id)
            .order_by(ClusterCapabilityInventory.collected_at.desc()).limit(2000).all())
    since = None
    for row in rows:
        if row.current_version != target_version or row.is_mixed_version:
            break
        since = row.collected_at
    return since


def _health(cluster_id: str) -> tuple[str | None, list[str]]:
    from shared import cluster_snapshot

    snapshot = cluster_snapshot.read_snapshot(cluster_id) or {}
    raw = snapshot.get("health")
    health: dict = raw if isinstance(raw, dict) else {}
    return health.get("status"), sorted((health.get("checks") or {}).keys())


def staging_soak(session, target_version: str, *, cluster_id: str | None = None,
                 now: datetime | None = None) -> SoakResult:
    """Has the staging cluster run ``target_version`` long enough, and is it healthy now?"""
    return _staging_soak(session, target_version, cluster_id=cluster_id, now=now)


def _staging_soak(session, target_version: str, *, cluster_id: str | None, now: datetime | None) -> SoakResult:
    staging = staging_cluster(session, excluding=cluster_id)
    if staging is None:
        return SoakResult(False, "Chưa có cụm staging (môi trường lab) để chạy thử phiên bản này trước.")
    since = _soaked_since(session, staging.id, target_version)
    if since is None:
        return SoakResult(False, f"{staging.name} chưa chạy Ceph {target_version} trên mọi daemon.", staging.name)
    hours = ((now or utc_now()) - since).total_seconds() / 3600
    if hours < SOAK_HOURS:
        return SoakResult(False, f"{staging.name} mới chạy Ceph {target_version} {hours:.1f} giờ "
                                 f"(cần ≥ {SOAK_HOURS} giờ).", staging.name, hours)
    status, checks = _health(staging.id)
    if status != "HEALTH_OK":
        return SoakResult(False, f"{staging.name} đang {status or 'chưa rõ health'}"
                                 + (f" ({', '.join(checks)})" if checks else "") + " sau khi lên "
                                 f"{target_version}.", staging.name, hours)
    return SoakResult(True, f"{staging.name} đã chạy Ceph {target_version} {hours:.0f} giờ, HEALTH_OK.",
                      staging.name, hours)
