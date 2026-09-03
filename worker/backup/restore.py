"""Shared RBD restore script (Story 9.7, PRD FR-19) — rebuilds one RBD
image from its latest successful full backup plus the complete chain of
successful incremental (export-diff) backups built on top of it, applied
in creation order.

The ONLY general-purpose restore-import logic in the project — both of
Story 9.7's consumers call `restore_image()` below instead of writing
their own import loop:
  - `worker/executor/cluster_deploy.py`'s `restore_cluster_from_backup`
    DR phases (Task 2) — restores every tracked image onto a freshly
    rebuilt cluster.
  - `restore_rbd_image_to_production` (Task 3, `worker/backup/engine.py`)
    — restores exactly one image over live production data.

`worker/backup/restore_drill.py` (Story 9.4) predates this module and
still has its own MINIMAL, full-only restore-to-scratch helper — that
story's own Dev Notes already flagged this module as the eventual
factor-out target. Left unchanged here, per this story's own scope (Task
1 only, no refactor of 9.4's already-shipped, already-tested code).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import paramiko

from shared import db
from shared.cluster_nodes import resolve_ssh_creds
from shared.models import BackupJob
from worker.backup.cluster_scope import first_mon_node, get_cluster
from worker.backup.storage.base import BackupStorageBackend
from worker.executor.ssh_executor import KNOWN_HOSTS_PATH

if TYPE_CHECKING:
    from shared.models import Cluster

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10
CHUNK_SIZE = 4 * 1024 * 1024  # matches every other streaming path in worker/backup/


class RestoreError(Exception):
    """Raised for any unrecoverable failure inside `restore_image()` — a
    missing backup chain, a checksum mismatch on download, or an `rbd
    import`/`import-diff` command failure."""


@dataclass
class RestoreResult:
    success: bool
    full_job_id: str | None = None
    applied_diff_job_ids: list[str] = field(default_factory=list)
    size_bytes: int = 0
    duration_seconds: float = 0.0
    error_message: str | None = None


def _backup_chain(
    pool: str,
    image: str,
    cluster_id: str | None = None,
    recovery_point_job_id: str | None = None,
) -> tuple[BackupJob | None, list[BackupJob]]:
    """The latest successful full backup for (pool, image) IN cluster_id's
    OWN cluster (Phase 3 -- `None` means the default cluster, same as
    every other BackupJob query in this module family), plus every
    successful incremental built on top of IT SPECIFICALLY (`base_job_id`
    lineage, Story 9.1), oldest-first — the exact order `rbd import-diff`
    must apply them in."""
    with db.SessionLocal() as session:
        target_job = session.get(BackupJob, recovery_point_job_id) if recovery_point_job_id else None
        if recovery_point_job_id and (
            target_job is None
            or target_job.pool != pool
            or target_job.image != image
            or target_job.cluster_id != cluster_id
            or target_job.status != "SUCCESS"
            or target_job.job_type not in ("full", "incremental")
        ):
            return None, []
        if target_job is not None and target_job.job_type == "incremental":
            full_job = session.get(BackupJob, target_job.base_job_id) if target_job.base_job_id else None
        elif target_job is not None:
            full_job = target_job
        else:
            full_job = (
                session.query(BackupJob)
                .filter(
                    BackupJob.pool == pool,
                    BackupJob.image == image,
                    BackupJob.cluster_id == cluster_id,
                    BackupJob.job_type == "full",
                    BackupJob.status == "SUCCESS",
                )
                .order_by(BackupJob.created_at.desc())
                .first()
            )
        if (
            full_job is None
            or full_job.pool != pool
            or full_job.image != image
            or full_job.cluster_id != cluster_id
            or full_job.job_type != "full"
            or full_job.status != "SUCCESS"
            or (target_job is not None and target_job.backup_target_slot != full_job.backup_target_slot)
        ):
            return None, []
        diff_query = (
            session.query(BackupJob)
            .filter(
                BackupJob.base_job_id == full_job.id,
                BackupJob.cluster_id == cluster_id,
                BackupJob.job_type == "incremental",
                BackupJob.status == "SUCCESS",
                BackupJob.backup_target_slot == full_job.backup_target_slot,
            )
        )
        if target_job is not None:
            diff_query = diff_query.filter(BackupJob.created_at <= target_job.created_at)
        diff_jobs = diff_query.order_by(BackupJob.created_at.asc()).all()
        return full_job, diff_jobs


def latest_backup_target_slot(
    pool: str, image: str, cluster_id: str | None = None, recovery_point_job_id: str | None = None
) -> str | None:
    """Which `BackupTarget` slot ("a"/"b", or "cluster" for an additional
    cluster's own single target) the latest successful full backup for
    (pool, image) was written to. Callers need this to pick the right
    `BackupStorageBackend` (`worker/backup/storage/factory.py::
    get_backend()`/`get_backend_for_cluster()`) BEFORE calling
    `restore_image()` below — that function re-resolves the full backup
    job internally once given a backend, but has no way to pick a backend
    on its own. Shared by both Story 9.7 consumers (Task 2, Task 3) so
    neither re-queries this itself."""
    full_job, _ = _backup_chain(pool, image, cluster_id=cluster_id,
                                recovery_point_job_id=recovery_point_job_id)
    return full_job.backup_target_slot if full_job is not None else None


def latest_full_backup_job(pool: str, image: str, cluster_id: str | None = None) -> BackupJob | None:
    """The latest successful full backup for (pool, image) — exposed
    separately from `restore_image()` for callers that only need its
    metadata (e.g. `size_bytes`, for post-restore integrity comparison —
    Story 9.7 Task 2's `_phase_verify_integrity`) without running an
    actual restore."""
    full_job, _ = _backup_chain(pool, image, cluster_id=cluster_id)
    return full_job


def _first_mon_node(cluster: "Cluster | None" = None) -> str:
    try:
        return first_mon_node(cluster)
    except ValueError as exc:
        raise RestoreError(str(exc)) from exc


def _ssh_connect(mon_ip: str, cluster: "Cluster | None" = None) -> paramiko.SSHClient:
    ssh_user, ssh_key_path, _exec_mode, _container_name = resolve_ssh_creds(cluster)
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        hostname=mon_ip,
        username=ssh_user,
        key_filename=ssh_key_path,
        timeout=CONNECT_TIMEOUT_SECONDS,
    )
    return client


def _download_and_verify(storage: BackupStorageBackend, job: BackupJob) -> tuple[str, int]:
    """Downloads `job.remote_key` to a temp file, computing SHA256/size as
    it streams, then round-trips those values through `storage.verify()`
    — the SAME check `engine.py`'s upload path already does right after
    writing (that confirms a WRITE landed intact; this confirms a READ
    did), so a download corrupted in transit is caught here instead of
    silently feeding bad bytes into `rbd import`. Returns the local temp
    file path and its size — caller is responsible for deleting the file."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
            storage.download(job.remote_key, tmp)

        digest = hashlib.sha256()
        size = 0
        with open(tmp_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)

        expected_size = job.size_bytes
        expected_sha256 = job.sha256
        if expected_size is None or not expected_sha256:
            raise RestoreError(
                f"BackupJob {job.id} has no persisted source checksum/size; refusing restore"
            )
        actual_sha256 = digest.hexdigest()
        if size != expected_size or actual_sha256 != expected_sha256:
            raise RestoreError(
                f"checksum/size mismatch downloading {job.remote_key} (BackupJob {job.id}): "
                f"expected(size={expected_size}, sha256={expected_sha256}) vs "
                f"download(size={size}, sha256={actual_sha256})"
            )
        if not storage.verify(job.remote_key, expected_size, expected_sha256):
            raise RestoreError(f"backend verification failed for {job.remote_key} (BackupJob {job.id})")
        return tmp_path, size
    except Exception:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _stream_file_to_rbd(mon_ip: str, local_path: str, command: str, cluster: "Cluster | None" = None) -> None:
    """Streams `local_path` into `command` (an `rbd import -`/`rbd
    import-diff -` invocation) over a raw SSH session — same direction and
    pattern as `restore_drill.py::_import_backup_to_scratch`, generalized
    to accept any destination command instead of a scratch-only one."""
    client = _ssh_connect(mon_ip, cluster)
    try:
        stdin, stdout, stderr = client.exec_command(command)
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                stdin.write(chunk)
        stdin.close()
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            error_output = stderr.read().decode(errors="replace")
            raise RestoreError(f"{command} exited {exit_status}: {error_output}")
    finally:
        client.close()


def _run_rbd_command(mon_ip: str, command: str, cluster: "Cluster | None" = None) -> None:
    """Run a small non-streaming verification/cleanup command."""
    client = _ssh_connect(mon_ip, cluster)
    try:
        _stdin, stdout, stderr = client.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            error_output = stderr.read().decode(errors="replace")
            raise RestoreError(f"{command} exited {exit_status}: {error_output}")
    finally:
        client.close()


def restore_image(
    pool: str,
    image: str,
    storage: BackupStorageBackend,
    dest_pool: str,
    dest_image: str,
    cluster_id: str | None = None,
    cleanup_new_destination_on_failure: bool = False,
    recovery_point_job_id: str | None = None,
) -> RestoreResult:
    """Rebuilds `dest_pool/dest_image` (on `cluster_id`'s own cluster --
    `None` means the default cluster/`settings.ceph_mon_nodes`, Phase 3)
    from `pool/image`'s latest successful full backup plus every
    successful incremental chained on top of it, applied oldest-first:
    `rbd import` for the full export, then `rbd import-diff` for each
    incremental in `created_at` order. Never raises — failures come back
    as `RestoreResult(success=False, error_message=...)` so callers (a DR
    phase, or a single-image action) can record/report them their own
    way."""
    started_at = datetime.utcnow()
    cluster = get_cluster(cluster_id)
    full_job, diff_jobs = _backup_chain(
        pool, image, cluster_id=cluster_id, recovery_point_job_id=recovery_point_job_id
    )
    if full_job is None:
        message = f"No successful full backup found for {pool}/{image} — nothing to restore"
        logger.error("restore.restore_image: %s", message)
        return RestoreResult(success=False, error_message=message)

    mon_ip = _first_mon_node(cluster)
    total_size = 0
    applied_diff_ids: list[str] = []
    tmp_paths: list[str] = []
    destination_created = False
    destination_spec = f"{shlex.quote(dest_pool)}/{shlex.quote(dest_image)}"
    try:
        full_path, full_size = _download_and_verify(storage, full_job)
        tmp_paths.append(full_path)
        total_size += full_size
        # `rbd import` can create the destination before returning a non-zero
        # status. Mark it as potentially created before streaming so a failed
        # import cannot leave a partial image behind.
        destination_created = cleanup_new_destination_on_failure
        _stream_file_to_rbd(mon_ip, full_path, f"rbd import - {destination_spec}", cluster)
        destination_created = True

        for diff_job in diff_jobs:
            diff_path, diff_size = _download_and_verify(storage, diff_job)
            tmp_paths.append(diff_path)
            total_size += diff_size
            _stream_file_to_rbd(mon_ip, diff_path, f"rbd import-diff - {destination_spec}", cluster)
            applied_diff_ids.append(diff_job.id)

        # Do not treat a successful import stream as sufficient evidence:
        # Ceph must be able to resolve the resulting image afterwards.
        _run_rbd_command(mon_ip, f"rbd info {destination_spec} --format json", cluster)

        return RestoreResult(
            success=True,
            full_job_id=full_job.id,
            applied_diff_job_ids=applied_diff_ids,
            size_bytes=total_size,
            duration_seconds=(datetime.utcnow() - started_at).total_seconds(),
        )
    except Exception as exc:
        logger.exception(
            "restore.restore_image: failed restoring %s/%s into %s/%s", pool, image, dest_pool, dest_image
        )
        if cleanup_new_destination_on_failure and destination_created:
            try:
                _run_rbd_command(mon_ip, f"rbd rm {destination_spec}", cluster)
            except Exception:
                logger.exception("restore.restore_image: failed cleaning partial destination %s", destination_spec)
        return RestoreResult(
            success=False,
            full_job_id=full_job.id,
            applied_diff_job_ids=applied_diff_ids,
            size_bytes=total_size,
            duration_seconds=(datetime.utcnow() - started_at).total_seconds(),
            error_message=str(exc),
        )
    finally:
        for path in tmp_paths:
            if os.path.exists(path):
                os.remove(path)
