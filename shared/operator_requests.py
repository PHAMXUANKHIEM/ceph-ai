"""Incident codes Ceph AI opens for operator requests, never for a Ceph health check.

A request (create a pool, replace an OSD, reboot nodes, create CephFS…) is
carried by a synthetic Incident whose Action waits for approval. Its code can
never appear in `ceph health detail`, so a health-driven pass must leave it
alone: on 11/10/2026 the Watcher's recovery pass closed four pending
CS-STAGING service requests as "recovered" and cancelled their actions.
Keep this in sync with the *_CEPH_CODE constants of the Dashboard routes.
"""

from __future__ import annotations

OPERATOR_REQUEST_CODES = frozenset({
    "BACKUP_MANUAL", "BACKUP_SCHEDULED", "CHAT_REQUEST", "CINDER_SNAPSHOT_CREATE", "CINDER_SNAPSHOT_DELETE",
    "CINDER_SNAPSHOT_SCHEDULED", "CINDER_VOLUME_ATTACH", "CINDER_VOLUME_DETACH", "CLUSTER_DELETE", "CLUSTER_DEPLOY",
    "CLUSTER_NODE_ADD", "CLUSTER_NODE_REMOVE", "CLUSTER_UPGRADE", "CONVERT_CLUSTER_TO_CEPHADM", "NODE_MAINTENANCE",
    "NODE_OS_GATE", "OSD_REPLACE", "POOL_ACTION_REQUEST", "POOL_CREATE_REQUEST", "RBD_TRASH_PURGE_ALL",
    "RBD_TRASH_REMOVE", "RBD_VOLUME_CLONE", "RBD_VOLUME_COPY", "RBD_VOLUME_CREATE", "RBD_VOLUME_FLATTEN",
    "RBD_VOLUME_MOVE", "RBD_VOLUME_MOVE_CLEANUP", "RBD_VOLUME_QOS", "RBD_VOLUME_RENAME", "RBD_VOLUME_RESIZE",
    "RBD_VOLUME_TEMPLATE", "RBD_VOLUME_TRASH_MOVE", "RBD_VOLUME_TRASH_RESTORE", "RESTORE_CLUSTER_FROM_BACKUP",
    "RESTORE_RBD_IMAGE_AS_NEW", "RESTORE_RBD_IMAGE_TO_PRODUCTION", "VM_PERF_BENCHMARK", "VOLUME_PERF_SWEEP",
})
# Families of request codes with a suffix, e.g. "FILE_SERVICE_CHANGE:cephfs_create_volume".
OPERATOR_REQUEST_PREFIXES = ("FILE_SERVICE_CHANGE:",)


def is_operator_request(code: str | None) -> bool:
    code = code or ""
    return code in OPERATOR_REQUEST_CODES or code.startswith(OPERATOR_REQUEST_PREFIXES)
