"""RestoreDrill (Story 9.4, PRD FR-10) — `restore_drill_execute` action_id,
dispatched from `worker/backup/engine.py::run()`. Periodically restores
the most recent successful FULL backup of a configured "canary" image into
a dedicated scratch pool/image (never the operator's real data), verifies
it byte-for-byte via a re-export + checksum compare, then cleans up —
proves a backup is actually restorable, not just present.

Scope note: only restores the latest FULL export, not a full+diff chain —
proving the restore MECHANISM works end-to-end is this story's job; full
chain restore for a real disaster is Story 9.7's `worker/backup/restore.py`
(not yet written when this story was implemented — see Dev Notes below).

Contains a MINIMAL restore-to-scratch helper duplicated here rather than in
a shared `worker/backup/restore.py` — Story 9.7 is expected to factor the
real, general-purpose (full+diff chain) restore script out into that
module; this file will then call it instead of its own private helpers.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from datetime import datetime

import paramiko

from config.settings import settings
from shared import db
from shared.models import BackupJob
from worker.backup import alerting
from worker.backup.policy_config import load_backup_policy
from worker.backup.storage.factory import get_backend
from worker.executor.ssh_executor import KNOWN_HOSTS_PATH, execute_command

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10
CHUNK_SIZE = 4 * 1024 * 1024


class RestoreDrillError(Exception):
    """Raised for any unrecoverable failure inside a drill run."""


def _first_mon_node() -> str:
    nodes = [n.strip() for n in settings.ceph_mon_nodes.split(",") if n.strip()]
    if not nodes:
        raise RestoreDrillError("ceph_mon_nodes is not configured — cannot run a restore drill")
    return nodes[0]


def _latest_successful_full_backup(pool: str, image: str) -> BackupJob | None:
    """Default cluster only (multi-tenant remediation Phase 3 keeps
    RestoreDrill out of scope — its own `backup_policy.yaml` config is a
    single global dict, not a per-cluster list). `cluster_id.is_(None)`
    is REQUIRED, not decorative: an additional cluster can now have its
    own BackupJob rows for a same-named pool/image (Phase 3), which must
    never leak into the default cluster's drill."""
    with db.SessionLocal() as session:
        return (
            session.query(BackupJob)
            .filter(
                BackupJob.pool == pool,
                BackupJob.image == image,
                BackupJob.cluster_id.is_(None),
                BackupJob.job_type == "full",
                BackupJob.status == "SUCCESS",
            )
            .order_by(BackupJob.created_at.desc())
            .first()
        )


def _import_backup_to_scratch(mon_ip: str, local_path: str, scratch_pool: str, scratch_image: str) -> None:
    """Streams `local_path` into `rbd import - {scratch_pool}/{scratch_image}`
    over a raw SSH session — the inverse direction of engine.py's export
    streaming."""
    client = paramiko.SSHClient()
    if os.path.exists(KNOWN_HOSTS_PATH):
        client.load_host_keys(KNOWN_HOSTS_PATH)
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=mon_ip, username=settings.ssh_user, key_filename=settings.ssh_key_path, timeout=CONNECT_TIMEOUT_SECONDS
    )
    client.save_host_keys(KNOWN_HOSTS_PATH)
    try:
        stdin, stdout, stderr = client.exec_command(f"rbd import - {scratch_pool}/{scratch_image}")
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
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=mon_ip, username=settings.ssh_user, key_filename=settings.ssh_key_path, timeout=CONNECT_TIMEOUT_SECONDS
    )
    client.save_host_keys(KNOWN_HOSTS_PATH)
    try:
        _stdin, stdout, stderr = client.exec_command(f"rbd export {scratch_pool}/{scratch_image} -")
        digest = hashlib.sha256()
        while True:
            chunk = stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        exit_status = stdout.channel.recv_exit_status()
        if exit_status != 0:
            error_output = stderr.read().decode(errors="replace")
            raise RestoreDrillError(f"rbd export (verify) exited {exit_status}: {error_output}")
        return digest.hexdigest()
    finally:
        client.close()


def _cleanup_scratch(mon_ip: str, scratch_pool: str, scratch_image: str) -> None:
    try:
        execute_command(mon_ip, f"rbd rm {scratch_pool}/{scratch_image}")
    except Exception:
        logger.exception(
            "restore_drill: failed to clean up scratch image %s/%s — may need manual cleanup",
            scratch_pool,
            scratch_image,
        )


def _record_result(
    pool: str, image: str, success: bool, started_at: datetime, error_message: str | None
) -> None:
    with db.SessionLocal() as session:
        session.add(
            BackupJob(
                run_id=f"drill-{started_at.strftime('%Y%m%dT%H%M%SZ')}",
                pool=pool,
                image=image,
                job_type="restore_drill",
                status="SUCCESS" if success else "FAILED",
                error_message=error_message,
                duration_seconds=(datetime.utcnow() - started_at).total_seconds(),
                created_at=started_at,
                finished_at=datetime.utcnow(),
            )
        )
        session.commit()


def run(action_pk: str, action_params: dict, incident_id: str, write_progress, *_unused) -> bool:
    drill_config = load_backup_policy().get("restore_drill") or {}
    pool = drill_config.get("pool")
    image = drill_config.get("image")
    scratch_pool = drill_config.get("scratch_pool")
    scratch_image = drill_config.get("scratch_image")
    if not all([pool, image, scratch_pool, scratch_image]):
        logger.error("restore_drill.run: 'restore_drill' is not fully configured in backup_policy.yaml")
        return False

    started_at = datetime.utcnow()
    progress = [{"step": "restore_drill", "status": "running", "started_at": started_at.isoformat()}]
    write_progress(action_pk, progress)

    backup_job = _latest_successful_full_backup(pool, image)
    if backup_job is None:
        message = f"Không có bản backup full thành công nào cho {pool}/{image} để thử khôi phục"
        logger.error("restore_drill.run: %s", message)
        _record_result(pool, image, False, started_at, message)
        alerting.send_alert("critical", message)
        return False

    mon_ip = _first_mon_node()
    backend = get_backend(backup_job.backup_target_slot, settings)

    tmp_path = None
    try:
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

        _import_backup_to_scratch(mon_ip, tmp_path, scratch_pool, scratch_image)
        restored_sha256 = _export_scratch_sha256(mon_ip, scratch_pool, scratch_image)

        if restored_sha256 != source_sha256:
            raise RestoreDrillError(
                f"checksum mismatch after restore: source={source_sha256} restored={restored_sha256}"
            )

        _record_result(pool, image, True, started_at, None)
        progress[0]["status"] = "done"
        progress[0]["finished_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)
        return True
    except Exception as exc:
        logger.exception("restore_drill.run: failed for %s/%s", pool, image)
        _record_result(pool, image, False, started_at, str(exc))
        progress[0]["status"] = "failed"
        progress[0]["message"] = str(exc)
        write_progress(action_pk, progress)
        alerting.send_alert(
            "critical", f"RestoreDrill thất bại cho {pool}/{image}: {exc}", backup_job_id=backup_job.id
        )
        return False
    finally:
        _cleanup_scratch(mon_ip, scratch_pool, scratch_image)
        if tmp_path is not None and os.path.exists(tmp_path):
            os.remove(tmp_path)
