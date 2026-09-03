"""Cluster resolution helpers shared by worker/backup/{engine,metadata,
restore,restore_drill}.py (multi-tenant remediation Phase 3) —
consolidates what used to be 4 separately-duplicated `_first_mon_node()`
copies (each reading `config.settings.settings` directly) into one
cluster-aware version built on `shared/cluster_nodes.py::
configured_nodes()` (Phase 1's "cluster=None -> global settings,
unchanged" opt-in pattern).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from config.settings import settings
from shared import db
from shared.cluster_nodes import configured_nodes
from worker.backup.policy_config import backup_targets_from_policy
from worker.backup.storage.base import BackupStorageBackend
from worker.backup.storage.factory import get_backend, get_backend_for_cluster

if TYPE_CHECKING:
    from shared.models import Cluster

logger = logging.getLogger(__name__)

_RBD_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def is_valid_rbd_name(value: object) -> bool:
    """Return whether a pool/image component is safe for RBD commands and
    backup object keys. Callers still quote command arguments as defense in
    depth."""
    return (
        isinstance(value, str)
        and value not in {".", ".."}
        and _RBD_NAME_RE.fullmatch(value) is not None
    )


def get_cluster(cluster_id: str | None) -> "Cluster | None":
    """Re-fetches the `Cluster` row for `cluster_id` in a FRESH, short-lived
    session — every backup call site needs its own read, never a
    long-lived detached ORM object passed around a multi-minute SSH
    transfer (same reasoning `worker/llm/router_client.py::
    _execute_approved_action`'s own per-call re-fetch already follows).
    `None` stays `None` (the default cluster, zero DB round-trip)."""
    if cluster_id is None:
        return None
    from shared.models import Cluster

    with db.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id)
        if cluster is not None:
            session.expunge(cluster)
        return cluster


def first_mon_node(cluster: "Cluster | None") -> str:
    """First MON-role host for `cluster` (or the global `settings`
    singleton when `cluster` is None) — shared by every backup module's
    own `_first_mon_node()` wrapper, which translates the bare
    `ValueError` below into that module's own typed exception."""
    nodes = configured_nodes(cluster)
    mon_hosts = [n["host"] for n in nodes if "MON" in n["roles"]]
    if not mon_hosts:
        raise ValueError("no MON node configured — cannot run rbd/ceph commands")
    return mon_hosts[0]


def parse_tracked_images(raw: str) -> list[tuple[str, str]]:
    """Parses `Cluster.backup_tracked_images`'s CSV of "pool/image" pairs
    (multi-tenant remediation Phase 3) — same tolerant split/strip/skip-
    blanks posture as `shared/cluster_nodes.py::configured_nodes()` uses
    for `ceph_mon_nodes` etc. An entry without exactly one "/" is skipped
    with a warning rather than raising — one operator typo must never
    take down every other cluster's/image's schedule/alert registration.
    Shared by `scheduler.py` and `alerting.py` — neither may import the
    other (scheduler.py already imports alerting.py at module level for
    its own APScheduler job registration)."""
    pairs: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("/")
        if len(parts) != 2 or not all(is_valid_rbd_name(part) for part in parts):
            logger.warning(
                "cluster_scope.parse_tracked_images: skipping malformed entry %r "
                "(expected safe pool/image)",
                entry,
            )
            continue
        pairs.append((parts[0], parts[1]))
    return pairs


def resolve_targets(cluster: "Cluster | None") -> list[tuple[str, BackupStorageBackend]]:
    """(slot, backend) pairs to upload/sweep-retention for — the default
    cluster's fixed a/b pair from `backup_policy.yaml` (unchanged), or
    (multi-tenant remediation Phase 3) an additional cluster's own SINGLE
    target, stamped with the fixed "cluster" marker slot (see
    `shared/models.py::BackupJob.backup_target_slot`'s own comment).
    Shared by `engine.py` and `metadata.py` — both upload to and sweep
    retention over the same set of targets for a given cluster."""
    if cluster is None:
        return [
            (t["slot"], get_backend(t["slot"], settings, immutable_enabled=bool(t.get("immutable"))))
            for t in backup_targets_from_policy()
        ]
    return [("cluster", get_backend_for_cluster(cluster))]
