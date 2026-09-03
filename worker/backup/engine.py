"""Backup Engine — bespoke orchestrator for Epic 9's `backup_action_ids`
family (Story 9.1), dispatched from `worker/llm/router_client.py::
_execute_approved_action()` the same way `cluster_deploy.py`/`volume_perf.py`
already are (see `worker/policy/action_policy.yaml`'s `backup_action_ids:`
comment for why this is its own multi-step orchestrator rather than the
generic per-host command loop).

`backup_delete_manual` is the only remaining member of `BACKUP_ACTION_IDS`
not yet implemented here — registered in the policy enum since Story 9.1's
Task 6 (same "register the whole family, wire real execution
incrementally" precedent Story 8.1 set for `cluster_deploy_action_ids`),
`run()` returns False with a clear log for it until some later story wires
it up, mirroring the now-removed `_not_implemented_phase` placeholder
`cluster_deploy.py` used during Epic 8. Every other member is real:
`rbd_backup_run`/`retention_sweep_delete` (Story 9.1), `backup_metadata_run`
(Story 9.3), `restore_drill_execute` (Story 9.4), and
`restore_rbd_image_to_production` (Story 9.7 — restores ONE image over
live production data via `worker/backup/restore.py::restore_image()`, the
same shared restore path Story 9.7's DR cluster-rebuild phases use).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

import paramiko
from sqlalchemy.exc import IntegrityError

from config.settings import settings
from shared import audit, db
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared.models import BackupJob
from worker.backup import ai_analysis, anomaly
from worker.backup import metadata as backup_metadata
from worker.backup import restore
from worker.backup import restore_drill
from worker.backup.cluster_scope import first_mon_node, get_cluster, resolve_targets
from worker.backup.policy_config import load_backup_policy
from worker.backup.storage.base import RetentionPolicyLike
from worker.backup.storage.factory import get_backend, get_backend_for_cluster
from worker.executor.ssh_executor import KNOWN_HOSTS_PATH, execute_command
from worker.policy.gate import VALID_BACKUP_ACTION_IDS
from watcher import ceph_client
from watcher.ceph_client import CephQueryError

if TYPE_CHECKING:
    from shared.models import Cluster

logger = logging.getLogger(__name__)

# Same naming convention as CLUSTER_DEPLOY_ACTION_IDS/VOLUME_PERF_ACTION_IDS
# (worker/executor/cluster_deploy.py, volume_perf.py) — reads the FULL
# family from action_policy.yaml, not a hardcoded subset, so Story 9.4/9.7
# adding their ids there needs zero change here.
BACKUP_ACTION_IDS = VALID_BACKUP_ACTION_IDS

CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB — matches Story 9.2's storage backends
PROGRESS_WRITE_INTERVAL_SECONDS = 3
CONNECT_TIMEOUT_SECONDS = 10
# A BackupJob stuck RUNNING longer than this is presumed crashed (Worker
# died mid-export) rather than genuinely still in progress — the next
# scheduled trigger for the same (pool, image) supersedes it instead of
# skipping forever.
STALE_RUNNING_TIMEOUT_SECONDS = 3600


class BackupEngineError(Exception):
    """Raised for an unrecoverable failure inside a backup run — a Command
    failure, not a Router/transient failure (mirrors ExecutorError's own
    "don't retry a command unlikely to succeed on a bare retry" posture)."""


def _make_progress(total_bytes: int) -> list[dict]:
    return [
        {
            "step": "export",
            "label": "Xuất dữ liệu RBD",
            "status": "pending",
            "message": None,
            "started_at": None,
            "finished_at": None,
            "bytes_transferred": 0,
            "total_bytes": total_bytes,
            "speed_mbps": 0.0,
            "eta_seconds": None,
        }
    ]


class _ProgressTrackingReader:
    """Wraps a raw, one-shot readable stream (an SSH channel's stdout) so
    `BackupStorageBackend.upload()` can read it exactly like any other
    file-like object, while THIS class observes bytes as they pass through
    to compute %/speed/ETA and throttle `write_progress` calls — added
    here so Story 9.2's already-tested storage backends need no progress-
    callback parameter of their own (AD-12)."""

    def __init__(self, source, total_bytes: int, action_pk: str, write_progress, progress: list[dict]):
        self._source = source
        self._total_bytes = total_bytes
        self._action_pk = action_pk
        self._write_progress = write_progress
        self._progress = progress
        self._bytes_read = 0
        self._started_at = time.monotonic()
        self._last_write_at = 0.0
        self.sha256 = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        chunk = self._source.read(size if size and size > 0 else CHUNK_SIZE)
        if chunk:
            self._bytes_read += len(chunk)
            self.sha256.update(chunk)
            self._maybe_write_progress()
        return chunk

    def _maybe_write_progress(self) -> None:
        now = time.monotonic()
        if now - self._last_write_at < PROGRESS_WRITE_INTERVAL_SECONDS:
            return
        self._last_write_at = now
        elapsed = max(now - self._started_at, 0.001)
        speed_mbps = (self._bytes_read / (1024 * 1024)) / elapsed
        remaining = max(self._total_bytes - self._bytes_read, 0)
        eta_seconds = int(remaining / (speed_mbps * 1024 * 1024)) if speed_mbps > 0 else None
        step = self._progress[0]
        step["bytes_transferred"] = self._bytes_read
        step["speed_mbps"] = round(speed_mbps, 2)
        step["eta_seconds"] = eta_seconds
        self._write_progress(self._action_pk, self._progress)


def _first_mon_node(cluster: "Cluster | None" = None) -> str:
    try:
        return first_mon_node(cluster)
    except ValueError as exc:
        raise BackupEngineError(str(exc)) from exc


def _rbd_image_size_bytes(mon_ip: str, pool: str, image: str) -> int:
    output = execute_command(mon_ip, f"rbd info {pool}/{image} --format json")
    info = json.loads(output)
    return int(info["size"])


def _latest_backup_job(
    pool: str, image: str, job_type: str | None = None, cluster_id: str | None = None
) -> BackupJob | None:
    with db.SessionLocal() as session:
        query = session.query(BackupJob).filter(
            BackupJob.pool == pool, BackupJob.image == image, BackupJob.cluster_id == cluster_id
        )
        if job_type is not None:
            query = query.filter(BackupJob.job_type == job_type)
        return query.order_by(BackupJob.created_at.desc()).first()


def _protected_keys(pool: str, image: str, cluster_id: str | None = None) -> frozenset[str]:
    """Full-export keys that a currently-kept incremental still depends
    on — retention (FR-2) must never delete these even if they'd
    otherwise fall outside keep_full_count by age alone."""
    with db.SessionLocal() as session:
        incrementals = (
            session.query(BackupJob)
            .filter(
                BackupJob.pool == pool,
                BackupJob.image == image,
                BackupJob.cluster_id == cluster_id,
                BackupJob.job_type == "incremental",
            )
            .filter(BackupJob.status == "SUCCESS")
            .all()
        )
        protected = set()
        for inc in incrementals:
            if inc.base_job_id is not None:
                base = session.get(BackupJob, inc.base_job_id)
                if base is not None and base.remote_key:
                    protected.add(base.remote_key)
        return frozenset(protected)


def _claim_running_backup(
    run_id: str,
    cluster_id: str | None,
    pool: str,
    image: str,
    job_type: str,
    base_job_id: str | None,
) -> str | None:
    """Atomically claim the image for one RBD export.

    The partial unique index on ``backup_jobs`` is the race-safe guard. The
    pre-flight query in ``_run_rbd_backup`` is useful for the normal path and
    for the stale-job policy, but it cannot protect two workers that read at
    the same time; the insert below closes that gap.
    """
    with db.SessionLocal() as session:
        row = BackupJob(
            run_id=run_id,
            cluster_id=cluster_id,
            pool=pool,
            image=image,
            job_type=job_type,
            status="RUNNING",
            base_job_id=base_job_id,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if "uq_backup_jobs_active_rbd_run" not in str(exc.orig):
                raise
            return None
        return row.id


def _mark_running_failed(running_job_id: str, error_message: str) -> None:
    with db.SessionLocal() as session:
        row = session.get(BackupJob, running_job_id)
        if row is None or row.status != "RUNNING":
            return
        row.status = "FAILED"
        row.error_message = error_message
        row.finished_at = datetime.utcnow()
        session.commit()


def run(
    action_pk: str,
    action_id: str,
    action_params: dict,
    incident_id: str,
    cluster_id: str | None,
    write_progress,
    *_unused,
) -> bool:
    """Dispatches by `action_id` — this family has more than one member
    needing different logic (unlike volume_perf.py's single-id module),
    so this takes `action_id` the way `cluster_deploy.run()` does.

    `cluster_id` (multi-tenant remediation Phase 3): `None` means the
    default cluster, byte-for-byte the same behavior as before this param
    existed — an additional cluster's own Incident/Action carries its
    `cluster_id` through here from `worker/llm/router_client.py::
    _execute_approved_action`/`worker/backup/scheduler.py`'s scheduled
    jobs. `restore_drill_execute` does NOT forward it — RestoreDrill stays
    default-cluster-only this phase (explicit narrowing, see this
    session's plan)."""
    if action_id == "rbd_backup_run":
        return _run_rbd_backup(action_pk, action_params, incident_id, cluster_id, write_progress)
    if action_id == "retention_sweep_delete":
        return _run_retention_sweep(
            action_pk, action_params, incident_id, cluster_id, write_progress
        )
    if action_id == "backup_metadata_run":
        return backup_metadata.run(
            action_pk, action_params, incident_id, cluster_id, write_progress
        )
    if action_id == "restore_drill_execute":
        return restore_drill.run(action_pk, action_params, incident_id, write_progress)
    if action_id in ("restore_rbd_image_to_production", "restore_rbd_image_as_new"):
        return _run_restore_to_production(
            action_pk, action_params, incident_id, cluster_id, write_progress
        )
    logger.error(
        "backup_engine.run: action_id=%s is registered in backup_action_ids but not yet "
        "implemented (lands in a later Epic 9 story) — marking FAILED instead of guessing",
        action_id,
    )
    return False


def _run_rbd_backup(
    action_pk: str, action_params: dict, incident_id: str, cluster_id: str | None, write_progress
) -> bool:
    pool = action_params.get("pool")
    image = action_params.get("image")
    if not pool or not image:
        logger.error("backup_engine._run_rbd_backup: missing pool/image in action_params for action %s", action_pk)
        return False

    cluster = get_cluster(cluster_id)

    stale_cutoff = datetime.utcnow().timestamp() - STALE_RUNNING_TIMEOUT_SECONDS
    running = _latest_backup_job(pool, image, job_type=None, cluster_id=cluster_id)
    if running is not None and running.status == "RUNNING":
        if running.created_at.timestamp() > stale_cutoff:
            logger.info(
                "backup_engine._run_rbd_backup: a RUNNING BackupJob for %s/%s is still fresh — "
                "skipping this trigger to avoid a duplicate (idempotent, AC #7)",
                pool,
                image,
            )
            return True
        logger.warning(
            "backup_engine._run_rbd_backup: a stale RUNNING BackupJob (id=%s) for %s/%s found — "
            "marking it FAILED (presumed crashed) and starting a fresh job",
            running.id,
            pool,
            image,
        )
        with db.SessionLocal() as session:
            row = session.get(BackupJob, running.id)
            if row is not None:
                row.status = "FAILED"
                row.error_message = "Presumed crashed — superseded by a fresh scheduled run"
                row.finished_at = datetime.utcnow()
                session.commit()

    last_full = _latest_backup_job(pool, image, job_type="full", cluster_id=cluster_id)
    last_success = _latest_backup_job(pool, image, job_type=None, cluster_id=cluster_id)
    if cluster is not None:
        # Additional cluster (Phase 3): backup_policy.yaml's tracked_images
        # list only ever describes the default cluster's own pools/images —
        # this cluster's full-refresh cadence is its own Cluster.
        # backup_full_refresh_days column instead (see that column's docstring).
        full_refresh_days = cluster.backup_full_refresh_days
    else:
        tracked = next(
            (t for t in (load_backup_policy().get("tracked_images") or []) if t.get("pool") == pool and t.get("image") == image),
            {},
        )
        full_refresh_days = tracked.get("full_refresh_every_n_days")

    is_full = last_full is None or last_full.status != "SUCCESS"
    if not is_full and full_refresh_days:
        age_days = (datetime.utcnow() - last_full.created_at).days
        is_full = age_days >= full_refresh_days
    job_type = "full" if is_full else "incremental"
    base_job = None if is_full else last_full

    run_id = str(uuid.uuid4())
    mon_ip = _first_mon_node(cluster)
    running_job_id = _claim_running_backup(
        run_id,
        cluster_id,
        pool,
        image,
        job_type,
        base_job.id if base_job is not None else None,
    )
    if running_job_id is None:
        logger.info(
            "backup_engine._run_rbd_backup: another worker claimed %s/%s — "
            "skipping this trigger (idempotent)",
            pool,
            image,
        )
        return True
    snap_name = f"backup-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"

    progress = _make_progress(total_bytes=0)
    write_progress(action_pk, progress)

    try:
        execute_command(mon_ip, f"rbd snap create {pool}/{image}@{snap_name}")
    except Exception as exc:
        logger.exception("backup_engine._run_rbd_backup: rbd snap create failed for %s/%s", pool, image)
        _mark_running_failed(running_job_id, str(exc))
        progress[0]["status"] = "failed"
        progress[0]["message"] = str(exc)
        write_progress(action_pk, progress)
        return False

    try:
        total_bytes = _rbd_image_size_bytes(mon_ip, pool, image)
    except Exception:
        logger.exception("backup_engine._run_rbd_backup: rbd info failed for %s/%s", pool, image)
        total_bytes = 0
    progress[0]["total_bytes"] = total_bytes
    write_progress(action_pk, progress)

    if job_type == "full":
        export_cmd = f"rbd export {pool}/{image}@{snap_name} -"
    else:
        export_cmd = (
            f"rbd export-diff {pool}/{image}@{snap_name} "
            f"--from-snap {_snap_name_of(base_job)} -"
        )

    ssh_user, ssh_key_path, _exec_mode, _container_name = resolve_ssh_creds(cluster)

    job_ids: list[str] = []
    tmp_path = None
    backup_succeeded = False
    try:
        client = paramiko.SSHClient()
        if os.path.exists(KNOWN_HOSTS_PATH):
            client.load_host_keys(KNOWN_HOSTS_PATH)
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=mon_ip,
            username=ssh_user,
            key_filename=ssh_key_path,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        client.save_host_keys(KNOWN_HOSTS_PATH)
        try:
            _stdin, stdout, stderr = client.exec_command(export_cmd)
            progress[0]["status"] = "running"
            progress[0]["started_at"] = datetime.utcnow().isoformat()
            write_progress(action_pk, progress)

            tracked_stream = _ProgressTrackingReader(stdout, total_bytes, action_pk, write_progress, progress)
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name
                while True:
                    chunk = tracked_stream.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    tmp.write(chunk)

            exit_status = stdout.channel.recv_exit_status()
            if exit_status != 0:
                error_output = stderr.read().decode(errors="replace")
                raise BackupEngineError(f"{export_cmd} exited {exit_status}: {error_output}")
        finally:
            client.close()

        sha256 = tracked_stream.sha256.hexdigest()
        size_bytes = tracked_stream._bytes_read

        uploaded_targets: list[tuple[str, str, object]] = []
        for slot, backend in resolve_targets(cluster):
            remote_key = f"{job_type}/{pool}/{image}/{snap_name}.bin"
            with open(tmp_path, "rb") as f:
                result = backend.upload(f, remote_key)
            if not backend.verify(remote_key, result.size, result.sha256):
                raise BackupEngineError(f"verify() failed after upload to slot {slot} for {remote_key}")
            uploaded_targets.append((slot, remote_key, result))

        with db.SessionLocal() as session:
            running_job = session.get(BackupJob, running_job_id)
            if running_job is None or running_job.status != "RUNNING":
                raise BackupEngineError(
                    f"active BackupJob claim {running_job_id} disappeared before commit"
                )
            finished_at = datetime.utcnow()
            duration_seconds = time.monotonic() - tracked_stream._started_at
            first_slot, first_key, _first_result = uploaded_targets[0]
            running_job.status = "SUCCESS"
            running_job.backup_target_slot = first_slot
            running_job.remote_key = first_key
            running_job.size_bytes = size_bytes
            running_job.duration_seconds = duration_seconds
            running_job.finished_at = finished_at
            session.flush()
            job_ids.append(running_job.id)
            for slot, remote_key, _result in uploaded_targets[1:]:
                job = BackupJob(
                    run_id=run_id,
                    cluster_id=cluster_id,
                    pool=pool,
                    image=image,
                    job_type=job_type,
                    status="SUCCESS",
                    base_job_id=base_job.id if base_job is not None else None,
                    backup_target_slot=slot,
                    remote_key=remote_key,
                    size_bytes=size_bytes,
                    duration_seconds=duration_seconds,
                    finished_at=finished_at,
                )
                session.add(job)
                session.flush()
                job_ids.append(job.id)
            session.commit()

        progress[0]["status"] = "done"
        progress[0]["finished_at"] = datetime.utcnow().isoformat()
        progress[0]["bytes_transferred"] = size_bytes
        write_progress(action_pk, progress)

        # Story 9.5 (AC #7, #8): anomaly check runs on EVERY success, but
        # AI is only invoked if it actually flags something — re-fetch ONE
        # representative row (all slots share the same duration/size for
        # this run) in a FRESH session, since `job` above is expired the
        # moment its own `with db.SessionLocal()` block closed.
        if job_ids:
            with db.SessionLocal() as session:
                representative_job = session.get(BackupJob, job_ids[0])
                anomaly_result = anomaly.check_anomaly(representative_job)
                if anomaly_result is not None:
                    ai_analysis.analyze_backup_job(representative_job, anomaly_result)
        backup_succeeded = True

    except Exception as exc:
        logger.exception("backup_engine._run_rbd_backup: export/upload failed for %s/%s", pool, image)
        progress[0]["status"] = "failed"
        progress[0]["message"] = str(exc)
        write_progress(action_pk, progress)
        _mark_running_failed(running_job_id, str(exc))
        with db.SessionLocal() as session:
            failed_job = session.get(BackupJob, running_job_id)
            if failed_job is None:
                logger.error(
                    "backup_engine._run_rbd_backup: failed to persist failed job %s",
                    running_job_id,
                )
                return False
            # Story 9.5 (AC #1, #2, #5): AI analysis for every failure —
            # still inside this session so `failed_job`'s attributes are
            # readable (not yet expired/detached).
            ai_analysis.analyze_backup_job(failed_job)
        return False
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.remove(tmp_path)
        # Incremental snapshots are never used as a future diff base (all
        # diffs anchor to the latest full), and a failed run's snapshot is
        # unusable.  Remove both best-effort so repeated runs cannot fill
        # the Ceph cluster with orphaned backup-* snapshots.
        if job_type == "incremental" or not backup_succeeded:
            _remove_snapshot_best_effort(mon_ip, pool, image, snap_name)

    # A successful replacement full becomes the new incremental base.  Its
    # predecessor can now be removed; cleanup failure must not invalidate a
    # backup that has already uploaded and verified successfully.
    if job_type == "full" and last_full is not None and last_full.status == "SUCCESS":
        _remove_snapshot_best_effort(mon_ip, pool, image, _snap_name_of(last_full))

    _sweep_retention_after_success(pool, image, incident_id, action_pk, cluster_id)
    return True


def _snap_name_of(job: BackupJob) -> str:
    """The full export's own snapshot name is embedded in its `remote_key`
    (`{job_type}/{pool}/{image}/{snap_name}.bin`) — no separate column
    needed just to recover it."""
    return job.remote_key.rsplit("/", 1)[-1].removesuffix(".bin")


def _remove_snapshot_best_effort(mon_ip: str, pool: str, image: str, snap_name: str) -> None:
    try:
        execute_command(mon_ip, f"rbd snap rm {pool}/{image}@{snap_name}")
    except Exception:
        logger.warning(
            "backup_engine: could not remove snapshot %s/%s@%s",
            pool,
            image,
            snap_name,
            exc_info=True,
        )


def _sweep_retention_after_success(
    pool: str, image: str, incident_id: str, action_pk: str, cluster_id: str | None = None
) -> None:
    """Sweeps BOTH the full/ and incremental/ prefixes separately — each
    has its OWN keep count (AC #4: "giữ N bản full + M bản incremental"),
    not one combined count across both job types. `keep_full_count`/
    `keep_incremental_count` stay the GLOBAL `backup_policy.yaml` values
    for every cluster (Phase 3 does not make retention COUNTS per-cluster
    overridable — explicit narrowing, see this session's plan)."""
    cluster = get_cluster(cluster_id)
    policy_dict = load_backup_policy().get("retention") or {}
    keep_full_count = int(policy_dict.get("keep_full_count", 1))
    keep_incremental_count = int(policy_dict.get("keep_incremental_count", 0))
    protected = _protected_keys(pool, image, cluster_id=cluster_id)

    for job_type, keep_count in (("full", keep_full_count), ("incremental", keep_incremental_count)):
        policy = RetentionPolicyLike(
            keep_full_count=keep_count if job_type == "full" else 0,
            keep_incremental_count=keep_count if job_type == "incremental" else 0,
            protected_keys=protected,
        )
        for slot, backend in resolve_targets(cluster):
            prefix = f"{job_type}/{pool}/{image}"
            try:
                deleted = backend.apply_retention(prefix, policy)
            except Exception:
                logger.exception(
                    "backup_engine._sweep_retention_after_success: apply_retention failed for slot %s, %s",
                    slot,
                    prefix,
                )
                continue
            for _key in deleted:
                with db.SessionLocal() as session:
                    audit.record(
                        session,
                        incident_id=incident_id,
                        action_id=action_pk,
                        event_type=audit.EVENT_BACKUP_RETENTION_DELETE,
                        actor=audit.ACTOR_SYSTEM,
                    )
                    session.commit()


def _run_restore_to_production(
    action_pk: str,
    action_params: dict,
    incident_id: str,
    cluster_id: str | None,
    write_progress,
) -> bool:
    """`restore_rbd_image_to_production` (Story 9.7, Task 3) — restores ONE
    image OVER its own live production data (unlike `restore_drill_execute`,
    which only ever touches a dedicated scratch pool/image). Deliberately
    thin: all the actual full+diff-chain import logic lives in
    `worker/backup/restore.py::restore_image()`, shared with the DR
    cluster-rebuild phases (`worker/executor/cluster_deploy.py`) — this
    function only resolves which `BackupStorageBackend` slot to read from
    and turns the result into progress/log output."""
    pool = action_params.get("pool")
    image = action_params.get("image")
    dest_pool = action_params.get("dest_pool") or pool
    dest_image = action_params.get("dest_image") or image
    recovery_point_job_id = action_params.get("recovery_point_job_id")
    if not pool or not image:
        logger.error(
            "backup_engine._run_restore_to_production: missing pool/image in action_params for action %s",
            action_pk,
        )
        return False
    progress = [{"step": "preflight_recheck", "status": "running", "started_at": datetime.utcnow().isoformat()},
                {"step": "restore", "status": "pending"}]
    write_progress(action_pk, progress)

    cluster = get_cluster(cluster_id)
    approved_preflight = action_params.get("preflight")
    if isinstance(approved_preflight, dict):
        try:
            if cluster is None:
                inventory = ceph_client.query_rbd_inventory(dest_pool)
                overview = ceph_client.query_rbd_pool_overview(dest_pool)
            else:
                nodes = [row["host"] for row in configured_nodes(cluster) if "MON" in row["roles"]]
                ssh_user, ssh_key_path, exec_mode, container_name = resolve_ssh_creds(cluster)
                connection = (nodes, container_name, ssh_user, ssh_key_path, exec_mode)
                inventory = ceph_client.query_rbd_inventory_with(dest_pool, *connection)
                overview = ceph_client.query_rbd_pool_overview_with(dest_pool, *connection)
            required_bytes = int(approved_preflight.get("required_bytes") or 0)
            blockers = []
            if any(row.get("name") == dest_image for row in inventory):
                blockers.append("destination_exists")
            if overview.get("near_full"):
                blockers.append("destination_pool_near_full")
            if overview.get("rbd_enabled") is False:
                blockers.append("destination_pool_rbd_disabled")
            max_available = int(overview.get("max_available") or 0)
            if max_available and required_bytes > max_available:
                blockers.append("insufficient_capacity")
            if blockers:
                raise BackupEngineError("Restore preflight re-check failed: " + ", ".join(blockers))
        except (CephQueryError, ValueError, BackupEngineError) as exc:
            progress[0]["status"] = "failed"
            progress[0]["message"] = str(exc)
            progress[0]["finished_at"] = datetime.utcnow().isoformat()
            write_progress(action_pk, progress)
            logger.error("backup_engine._run_restore_to_production: %s", exc)
            return False
    progress[0]["status"] = "done"
    progress[0]["finished_at"] = datetime.utcnow().isoformat()
    progress[1].update({"status": "running", "started_at": datetime.utcnow().isoformat()})
    write_progress(action_pk, progress)
    slot = restore.latest_backup_target_slot(
        pool, image, cluster_id=cluster_id, recovery_point_job_id=recovery_point_job_id
    )
    if slot is None:
        message = f"Không có bản backup full thành công nào cho {pool}/{image} để khôi phục"
        logger.error("backup_engine._run_restore_to_production: %s", message)
        progress[1]["status"] = "failed"
        progress[1]["message"] = message
        write_progress(action_pk, progress)
        return False

    backend = get_backend_for_cluster(cluster) if cluster is not None else get_backend(slot, settings)
    restore_as_new = dest_pool != pool or dest_image != image
    result = restore.restore_image(
        pool,
        image,
        backend,
        dest_pool,
        dest_image,
        cluster_id=cluster_id,
        cleanup_new_destination_on_failure=restore_as_new,
        recovery_point_job_id=recovery_point_job_id,
    )

    if not result.success:
        progress[1]["status"] = "failed"
        progress[1]["message"] = result.error_message
        write_progress(action_pk, progress)
        logger.error(
            "backup_engine._run_restore_to_production: restore failed for %s/%s: %s",
            dest_pool,
            dest_image,
            result.error_message,
        )
        return False

    progress[1]["status"] = "done"
    progress[1]["finished_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)
    return True


def _run_retention_sweep(
    action_pk: str, action_params: dict, incident_id: str, cluster_id: str | None, write_progress
) -> bool:
    """Manual/on-demand retention sweep entry point (same underlying
    `_sweep_retention_after_success` the automatic post-backup sweep uses)
    — `action_params` must carry `pool`/`image`."""
    pool = action_params.get("pool")
    image = action_params.get("image")
    if not pool or not image:
        logger.error("backup_engine._run_retention_sweep: missing pool/image in action_params for %s", action_pk)
        return False
    progress = [{"step": "retention", "status": "running", "started_at": datetime.utcnow().isoformat()}]
    write_progress(action_pk, progress)
    try:
        _sweep_retention_after_success(pool, image, incident_id, action_pk, cluster_id)
    except Exception as exc:
        logger.exception("backup_engine._run_retention_sweep: failed for %s/%s", pool, image)
        progress[0]["status"] = "failed"
        progress[0]["message"] = str(exc)
        write_progress(action_pk, progress)
        return False
    progress[0]["status"] = "done"
    progress[0]["finished_at"] = datetime.utcnow().isoformat()
    write_progress(action_pk, progress)
    return True
