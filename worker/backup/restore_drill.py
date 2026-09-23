"""RestoreDrill (Story 9.4/7.1, PRD FR-10) — `restore_drill_execute` action_id,
dispatched from `worker/backup/engine.py::run()`. Periodically restores
the selected recovery point of a configured "canary" image into
a dedicated scratch pool/image (never the operator's real data), verifies
it byte-for-byte via a re-export + checksum compare, then cleans up —
proves a backup is actually restorable, not just present.

The operator may select a historical recovery point and target slot A/B. The
same full+incremental restore engine used by production restore is exercised;
the scratch image is always isolated and cleaned up in the finally path.

The legacy full-only helper remains for compatibility with installations that
have no incremental chain; new chain drills use `worker/backup/restore.py`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import tempfile
import time
from datetime import datetime
from shared.time import utc_now

import paramiko

from config.settings import settings
from shared import db
from shared.models import BackupJob
from worker.backup import alerting
from worker.backup import restore as restore_chain
from worker.backup.cluster_scope import first_mon_node, get_cluster, is_valid_rbd_name
from worker.backup.policy_config import cluster_schedule_policy, load_backup_policy
from worker.backup.ssh_io import read_all, read_chunk, wait_exit_status, write_chunk
from worker.backup.storage.factory import get_backend, get_backend_for_cluster
from worker.executor.ssh_executor import ExecutorError, KNOWN_HOSTS_PATH, execute_command

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = settings.ceph_ssh_connect_timeout
CHUNK_SIZE = 4 * 1024 * 1024


class RestoreDrillError(Exception):
    """Raised for any unrecoverable failure inside a drill run."""


def _first_mon_node() -> str:
    nodes = [n.strip() for n in settings.ceph_mon_nodes.split(",") if n.strip()]
    if not nodes:
        raise RestoreDrillError("ceph_mon_nodes is not configured — cannot run a restore drill")
    return nodes[0]


def _latest_successful_recovery_point(
    pool: str, image: str, target_slot: str | None = None, recovery_point_job_id: str | None = None,
) -> BackupJob | None:
    """Default cluster only (multi-tenant remediation Phase 3 keeps
    RestoreDrill out of scope — its own `backup_policy.yaml` config is a
    single global dict, not a per-cluster list). `cluster_id.is_(None)`
    is REQUIRED, not decorative: an additional cluster can now have its
    own BackupJob rows for a same-named pool/image (Phase 3), which must
    never leak into the default cluster's drill."""
    with db.SessionLocal() as session:
        if recovery_point_job_id:
            query = session.query(BackupJob).filter(BackupJob.id == recovery_point_job_id)
        else:
            query = session.query(BackupJob).filter(
                BackupJob.pool == pool, BackupJob.image == image,
                BackupJob.cluster_id.is_(None), BackupJob.job_type.in_(("full", "incremental")),
                BackupJob.status == "SUCCESS",
            ).order_by(BackupJob.created_at.desc())
        job = query.first()
        if job is None or job.pool != pool or job.image != image or job.cluster_id is not None:
            return None
        if job.job_type not in ("full", "incremental") or job.status != "SUCCESS":
            return None
        if target_slot and job.backup_target_slot != target_slot:
            return None
        return job


def _latest_successful_full_backup(pool: str, image: str) -> BackupJob | None:
    """Backward-compatible helper for callers/tests that need only a full."""
    return _latest_successful_recovery_point(pool, image, target_slot=None)


def _import_backup_to_scratch(mon_ip: str, local_path: str, scratch_pool: str, scratch_image: str) -> None:
    """Streams `local_path` into `rbd import - {scratch_pool}/{scratch_image}`
    over a raw SSH session — the inverse direction of engine.py's export
    streaming."""
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    deadline = time.monotonic() + float(settings.ceph_backup_operation_timeout)
    client.connect(
        hostname=mon_ip,
        username=settings.ssh_user,
        key_filename=settings.ssh_key_path,
        timeout=min(CONNECT_TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic())),
        banner_timeout=min(settings.ceph_ssh_banner_timeout, max(0.1, deadline - time.monotonic())),
        auth_timeout=min(settings.ceph_ssh_auth_timeout, max(0.1, deadline - time.monotonic())),
    )
    try:
        stdin, stdout, stderr = client.exec_command(
            f"rbd import - {shlex.quote(scratch_pool)}/{shlex.quote(scratch_image)}",
            timeout=max(0.1, deadline - time.monotonic()),
        )
        with open(local_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                write_chunk(stdin, chunk, deadline)
        stdin.close()
        exit_status = wait_exit_status(stdout.channel, deadline)
        if exit_status != 0:
            error_output = read_all(stderr, deadline).decode(errors="replace")
            raise RestoreDrillError(f"rbd import exited {exit_status}: {error_output}")
    finally:
        client.close()


def _export_scratch_sha256(mon_ip: str, scratch_pool: str, scratch_image: str) -> str:
    """Re-exports the just-imported scratch image and hashes it, to prove
    the restore is byte-identical to the backup — not just that `rbd
    import` exited 0."""
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    deadline = time.monotonic() + float(settings.ceph_backup_operation_timeout)
    client.connect(
        hostname=mon_ip,
        username=settings.ssh_user,
        key_filename=settings.ssh_key_path,
        timeout=min(CONNECT_TIMEOUT_SECONDS, max(0.1, deadline - time.monotonic())),
        banner_timeout=min(settings.ceph_ssh_banner_timeout, max(0.1, deadline - time.monotonic())),
        auth_timeout=min(settings.ceph_ssh_auth_timeout, max(0.1, deadline - time.monotonic())),
    )
    try:
        _stdin, stdout, stderr = client.exec_command(
            f"rbd export {shlex.quote(scratch_pool)}/{shlex.quote(scratch_image)} -",
            timeout=max(0.1, deadline - time.monotonic()),
        )
        digest = hashlib.sha256()
        while True:
            chunk = read_chunk(stdout, CHUNK_SIZE, deadline)
            if not chunk:
                break
            digest.update(chunk)
        exit_status = wait_exit_status(stdout.channel, deadline)
        if exit_status != 0:
            error_output = read_all(stderr, deadline).decode(errors="replace")
            raise RestoreDrillError(f"rbd export (verify) exited {exit_status}: {error_output}")
        return digest.hexdigest()
    finally:
        client.close()


def _cleanup_scratch(mon_ip: str, scratch_pool: str, scratch_image: str) -> None:
    try:
        execute_command(
            mon_ip, f"rbd rm {shlex.quote(scratch_pool)}/{shlex.quote(scratch_image)}"
        )
    except Exception:
        logger.exception(
            "restore_drill: failed to clean up scratch image %s/%s — may need manual cleanup",
            scratch_pool,
            scratch_image,
        )


def _assert_scratch_absent(mon_ip: str, scratch_pool: str, scratch_image: str) -> None:
    """Refuse to touch an operator-owned scratch image.

    Cleanup is allowed only after this preflight proves that the destination
    did not exist before the drill. Treat transport/permission failures as
    blockers; only Ceph's explicit not-found result is safe to continue.
    """
    spec = f"{shlex.quote(scratch_pool)}/{shlex.quote(scratch_image)}"
    try:
        execute_command(mon_ip, f"rbd info {spec} --format json")
    except ExecutorError as exc:
        message = str(exc).lower()
        if "exited 2" in message or "no such" in message or "not found" in message:
            return
        raise RestoreDrillError(f"Không xác minh được scratch image trước DR drill: {exc}") from exc
    raise RestoreDrillError(
        f"Scratch image {scratch_pool}/{scratch_image} đã tồn tại; từ chối ghi đè hoặc tự xóa image có sẵn"
    )
def _record_result(
    pool: str, image: str, success: bool, started_at: datetime, error_message: str | None,
    size_bytes: int = 0, backup_target_slot: str | None = None,
    recovery_point_job_id: str | None = None,
    cluster_id: str | None = None,
) -> None:
    with db.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id=f"drill-{started_at.strftime('%Y%m%dT%H%M%SZ')}-{backup_target_slot or 'auto'}",
                cluster_id=cluster_id,
                pool=pool,
                image=image,
                job_type="restore_drill",
                status="SUCCESS" if success else "FAILED",
                error_message=error_message,
                size_bytes=size_bytes,
                backup_target_slot=backup_target_slot,
                duration_seconds=(utc_now() - started_at).total_seconds(),
                created_at=started_at,
                finished_at=utc_now(),
            )
        )
        session.commit()


def _run_secondary_drill(action_pk: str, cluster_id: str, drill_config: dict, write_progress) -> bool:
    """Restore only the selected cluster's backup to its own scratch image."""
    cluster = get_cluster(cluster_id)
    if cluster is None or not cluster.is_active or not cluster.backup_enabled:
        logger.error("restore_drill: secondary cluster is missing or backup disabled")
        return False
    pool, image = drill_config["pool"], drill_config["image"]
    scratch_pool, scratch_image = drill_config["scratch_pool"], drill_config["scratch_image"]
    started_at = utc_now()
    full, diffs = restore_chain._backup_chain(pool, image, cluster_id=cluster_id)
    if full is None or full.backup_target_slot != "cluster":
        _record_result(pool, image, False, started_at, "Missing verified cluster full backup",
                       backup_target_slot="cluster", cluster_id=cluster_id)
        return False
    mon_ip = first_mon_node(cluster)
    spec = f"{shlex.quote(scratch_pool)}/{shlex.quote(scratch_image)}"
    try:
        restore_chain._run_rbd_command(mon_ip, f"rbd info {spec} --format json", cluster)
    except restore_chain.RestoreError as exc:
        if not any(marker in str(exc).lower() for marker in ("exited 2", "not found", "no such")):
            logger.error("restore_drill: cannot prove scratch absent: %s", exc)
            return False
    else:
        logger.error("restore_drill: scratch %s already exists", spec)
        return False
    result = None
    try:
        backend = get_backend_for_cluster(cluster)
        result = restore_chain.restore_image(
            pool, image, backend, scratch_pool, scratch_image,
            cluster_id=cluster_id, cleanup_new_destination_on_failure=True,
        )
        if not result.success:
            raise RestoreDrillError(result.error_message or "restore chain failed")
        restore_chain._run_rbd_command(mon_ip, f"rbd rm {spec}", cluster)
        _record_result(pool, image, True, started_at, None, result.size_bytes,
                       backup_target_slot="cluster", cluster_id=cluster_id)
        write_progress(action_pk, [{"step": "restore_drill", "status": "done", "full_job_id": full.id,
                                    "applied_diff_job_ids": result.applied_diff_job_ids}])
        return True
    except Exception as exc:
        logger.exception("restore_drill: secondary cluster drill failed")
        _record_result(pool, image, False, started_at, str(exc), result.size_bytes if result else 0,
                       backup_target_slot="cluster", cluster_id=cluster_id)
        alerting.send_alert("critical", f"RestoreDrill thất bại cho {pool}/{image}: {exc}", cluster=cluster)
        return False


def run(action_pk: str, action_params: dict, incident_id: str, write_progress, *_unused,
        cluster_id: str | None = None) -> bool:
    drill_config = cluster_schedule_policy(load_backup_policy(), cluster_id).get("restore_drill") or {}
    pool = drill_config.get("pool")
    image = drill_config.get("image")
    scratch_pool = drill_config.get("scratch_pool")
    scratch_image = drill_config.get("scratch_image")
    if not all(is_valid_rbd_name(value) for value in (pool, image, scratch_pool, scratch_image)):
        logger.error("restore_drill.run: 'restore_drill' is not fully configured in backup_policy.yaml")
        return False
    if pool == scratch_pool and image == scratch_image:
        logger.error("restore_drill.run: source and scratch image must differ")
        return False
    if cluster_id is not None:
        return _run_secondary_drill(action_pk, cluster_id, drill_config, write_progress)

    started_at = utc_now()
    progress = [{"step": "restore_drill", "status": "running", "started_at": started_at.isoformat()}]
    write_progress(action_pk, progress)

    target_slot = action_params.get("target_slot") or None
    recovery_point_job_id = action_params.get("recovery_point_job_id") or None
    if target_slot not in {None, "a", "b"}:
        logger.error("restore_drill.run: invalid target_slot=%r", target_slot)
        return False
    backup_job = _latest_successful_recovery_point(pool, image, target_slot, recovery_point_job_id)
    if backup_job is None:
        message = f"Không có recovery point thành công phù hợp cho {pool}/{image} để thử khôi phục"
        logger.error("restore_drill.run: %s", message)
        _record_result(pool, image, False, started_at, message, backup_target_slot=target_slot,
                       recovery_point_job_id=recovery_point_job_id)
        alerting.send_alert("critical", message)
        return False

    mon_ip = _first_mon_node()
    progress[0]["target_slot"] = backup_job.backup_target_slot
    progress[0]["recovery_point_job_id"] = backup_job.id
    write_progress(action_pk, progress)
    chain_full, diff_jobs = restore_chain._backup_chain(
        pool, image, cluster_id=None, recovery_point_job_id=backup_job.id
    )
    if chain_full is None:
        message = f"Recovery point {backup_job.id} không có full base hợp lệ hoặc lệch target"
        _record_result(pool, image, False, started_at, message,
                       backup_target_slot=backup_job.backup_target_slot,
                       recovery_point_job_id=backup_job.id)
        alerting.send_alert("critical", message, backup_job_id=backup_job.id)
        return False
    backend = get_backend(chain_full.backup_target_slot, settings)

    tmp_path = None
    scratch_created = False
    try:
        _assert_scratch_absent(mon_ip, scratch_pool, scratch_image)
        # Reuse the production restore engine when the selected full backup
        # has successful incrementals. This makes the drill exercise the same
        # full + import-diff chain used by an actual recovery, while the
        # existing full-only path remains the small compatibility path for
        # installations that have not enabled incremental backups.
        if diff_jobs:
            scratch_created = True
            progress[0]["mode"] = "full+incremental"
            progress[0]["full_job_id"] = chain_full.id
            progress[0]["incremental_job_ids"] = [job.id for job in diff_jobs]
            write_progress(action_pk, progress)
            result = restore_chain.restore_image(
                pool,
                image,
                backend,
                scratch_pool,
                scratch_image,
                cluster_id=None,
                cleanup_new_destination_on_failure=True,
                recovery_point_job_id=backup_job.id,
            )
            if not result.success:
                progress[0]["applied_diff_job_ids"] = result.applied_diff_job_ids
                progress[0]["message"] = result.error_message or "full + incremental restore chain failed"
                write_progress(action_pk, progress)
                raise RestoreDrillError(result.error_message or "full + incremental restore chain failed")
            _record_result(pool, image, True, started_at, None, result.size_bytes,
                           backup_target_slot=chain_full.backup_target_slot,
                           recovery_point_job_id=backup_job.id)
            progress[0]["status"] = "done"
            progress[0]["finished_at"] = utc_now().isoformat()
            progress[0]["full_job_id"] = result.full_job_id
            progress[0]["applied_diff_job_ids"] = result.applied_diff_job_ids
            write_progress(action_pk, progress)
            return True

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
            backend.download(backup_job.remote_key, tmp)

        digest = hashlib.sha256()
        with open(tmp_path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
        source_sha256 = digest.hexdigest()
        expected_sha256 = backup_job.sha256
        if not expected_sha256:
            raise RestoreDrillError(
                f"BackupJob {backup_job.id} has no persisted source checksum; refusing restore drill"
            )
        expected_size = backup_job.size_bytes
        if expected_size is None or expected_size != os.path.getsize(tmp_path) or source_sha256 != expected_sha256:
            raise RestoreDrillError(
                f"backup checksum/size mismatch before restore: "
                f"expected(size={expected_size}, sha256={expected_sha256}) vs "
                f"download(size={os.path.getsize(tmp_path)}, sha256={source_sha256})"
            )
        if not backend.verify(backup_job.remote_key, expected_size, expected_sha256):
            raise RestoreDrillError(
                f"backend verification failed for BackupJob {backup_job.id}"
            )

        # Mark before import: a failed import can still leave a partial RBD
        # image that must be removed during the finally block.
        scratch_created = True
        _import_backup_to_scratch(mon_ip, tmp_path, scratch_pool, scratch_image)
        restored_sha256 = _export_scratch_sha256(mon_ip, scratch_pool, scratch_image)

        if restored_sha256 != source_sha256:
            raise RestoreDrillError(
                f"checksum mismatch after restore: source={source_sha256} restored={restored_sha256}"
            )

        _record_result(pool, image, True, started_at, None, backup_job.size_bytes or 0,
                       backup_target_slot=chain_full.backup_target_slot,
                       recovery_point_job_id=backup_job.id)
        progress[0]["status"] = "done"
        progress[0]["finished_at"] = utc_now().isoformat()
        write_progress(action_pk, progress)
        return True
    except Exception as exc:
        logger.exception("restore_drill.run: failed for %s/%s", pool, image)
        _record_result(pool, image, False, started_at, str(exc), backup_job.size_bytes or 0,
                       backup_target_slot=chain_full.backup_target_slot,
                       recovery_point_job_id=backup_job.id)
        progress[0]["status"] = "failed"
        progress[0]["message"] = str(exc)
        write_progress(action_pk, progress)
        alerting.send_alert(
            "critical", f"RestoreDrill thất bại cho {pool}/{image}: {exc}", backup_job_id=backup_job.id
        )
        return False
    finally:
        if scratch_created:
            _cleanup_scratch(mon_ip, scratch_pool, scratch_image)
        if tmp_path is not None and os.path.exists(tmp_path):
            os.remove(tmp_path)
