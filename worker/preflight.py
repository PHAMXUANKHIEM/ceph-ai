"""AI roadmap Pha 0.3 (Plan/ai-missing-features-roadmap.md) -- AI preflight
validator: the single gate `worker/llm/router_client.py::diagnose_incident`
calls right before a new remediation `Action` proposal is allowed to be
created, so a proposal can never reach the executor without first proving
it's compatible with the target cluster's real, evidenced version/
capability state (Pha 0's own "Hoàn thành khi" bar).

Three checks, in order, first failure wins -- see `run_preflight`'s own
docstring for what each one covers. Deliberately narrow in scope for this
first cut: "dependency" (roadmap wording) is interpreted here as just "is
the target Cluster row still active", not a general command dependency
graph -- nothing in this codebase today models cross-action dependencies,
and inventing one speculatively would be exactly the kind of premature
abstraction this codebase avoids elsewhere.

Enforcement is controlled by `settings.ai_preflight_enforcement_enabled`
and defaults to True. Unknown or stale evidence therefore fails closed on
new installations; the switch exists only as an explicit compatibility
escape hatch during an operator-controlled migration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from config.settings import settings
from shared import capability_matrix
from shared.models import CapabilityStatus, Cluster
from watcher import capability_inventory


@dataclass
class PreflightResult:
    allowed: bool
    reason: str | None = None
    # The CapabilityStatus.value (or None if blocked for a non-capability
    # reason, e.g. an inactive cluster) that drove this verdict -- callers
    # persist this alongside `reason` so a blocked Incident's diagnosis
    # text can show WHICH of Pha 0.1/0.2's fail-closed states applied.
    capability_status: str | None = None


def run_preflight(session, *, cluster_id: str | None, action_id: str) -> PreflightResult:
    """Fail-closed preflight check for a proposed `action_id` against
    `cluster_id` (None means the default cluster, same convention as
    `Incident.cluster_id`/`WatcherHeartbeat.cluster_id` elsewhere).

    1. The target `Cluster` row must still be active -- a disabled/removed
       cluster is never a valid remediation target, independent of what
       Pha 0.1/0.2 know about its version.
    2. Pha 0.1's `ClusterCapabilityInventory`: the most recent scan must
       have actually succeeded (`status == SUPPORTED` -- i.e. a single,
       recognized Ceph version, not UNKNOWN/UNAVAILABLE/UNSUPPORTED_VERSION
       and not a mixed-version window) AND be recent (younger than
       `settings.capability_inventory_max_age_seconds`, default 1h) -- an
       old-but-once-SUPPORTED snapshot from a Watcher that has since
       stopped scanning this cluster (found while writing Pha 0.5's own
       stale-evidence test) is exactly the "evidence quá cũ" case roadmap
       section 3.1 requires INSUFFICIENT_EVIDENCE for, not silent trust.
       No snapshot at all (Watcher hasn't scanned this cluster yet) is the
       same INSUFFICIENT_EVIDENCE verdict -- never treated as "assume
       fine".
    3. Pha 0.2's `CapabilityMatrixEntry`: `action_id` must have a
       SUPPORTED verdict (`shared/capability_matrix.py::check_capability`)
       at the cluster's current Ceph major version. UNKNOWN (no matrix
       entry yet) and UNSUPPORTED_VERSION both block, per section 3.2's
       explicit fail-closed rule -- there is no "assume supported because
       nothing says otherwise" case anywhere in this function.

    Never raises -- every DB/attribute access here is on already-validated
    ORM objects from the caller's own open session, so a failure here would
    only ever be this function's own bug. Callers should treat an
    unexpected exception as a bug to fix, not as "must fail closed harder"
    -- see this module's own docstring for why the ENFORCEMENT toggle,
    not an exception-driven fail-closed default, is what actually decides
    whether a would-block verdict stops Action creation.
    """
    cluster = session.get(Cluster, cluster_id) if cluster_id else None
    if cluster is not None and not cluster.is_active:
        return PreflightResult(False, reason=f"Cụm {cluster.name!r} đã bị vô hiệu hoá (is_active=false).")

    snapshot = capability_inventory.latest_snapshot(cluster_id, session=session)
    if snapshot is None:
        return PreflightResult(
            False,
            reason=(
                "Chưa có Cluster Capability Inventory (Pha 0.1) cho cụm này — Watcher chưa quét lần "
                "nào kể từ khi cụm được thêm. INSUFFICIENT_EVIDENCE."
            ),
            capability_status=CapabilityStatus.UNKNOWN.value,
        )
    if snapshot.status != CapabilityStatus.SUPPORTED.value:
        detail = f", lỗi: {snapshot.error_message}" if snapshot.error_message else ""
        mixed = " (đang có nhiều phiên bản Ceph khác nhau — có thể giữa quá trình nâng cấp)" if snapshot.is_mixed_version else ""
        return PreflightResult(
            False,
            reason=(
                f"Capability inventory gần nhất ({snapshot.collected_at.isoformat()}) cho cụm này là "
                f"{snapshot.status}{mixed}{detail} — chưa đủ evidence để tạo đề xuất hành động."
            ),
            capability_status=snapshot.status,
        )
    snapshot_age = datetime.utcnow() - snapshot.collected_at
    max_age = timedelta(seconds=settings.capability_inventory_max_age_seconds)
    if snapshot_age > max_age:
        return PreflightResult(
            False,
            reason=(
                f"Capability inventory gần nhất cho cụm này thu thập lúc "
                f"{snapshot.collected_at.isoformat()} — đã quá cũ ({snapshot_age} > "
                f"{max_age}, có thể Watcher đã ngừng quét cụm này) — INSUFFICIENT_EVIDENCE, "
                f"không dùng evidence cũ để tạo đề xuất hành động."
            ),
            capability_status=CapabilityStatus.UNKNOWN.value,
        )

    cap_result = capability_matrix.check_capability(action_id, snapshot.current_major, session=session)
    if cap_result.status != CapabilityStatus.SUPPORTED:
        return PreflightResult(
            False,
            reason=(
                f"Capability Matrix (Pha 0.2) trả {cap_result.status.value} cho action_id={action_id!r} "
                f"trên Ceph major {snapshot.current_major} (phiên bản: {snapshot.current_version}): "
                f"{cap_result.reason}"
            ),
            capability_status=cap_result.status.value,
        )

    return PreflightResult(True, capability_status=CapabilityStatus.SUPPORTED.value)
