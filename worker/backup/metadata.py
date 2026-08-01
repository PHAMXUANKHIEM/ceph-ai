"""Cluster metadata backup (Story 9.3, PRD FR-7) — `backup_metadata_run`
action_id, dispatched from `worker/backup/engine.py::run()` (own branch,
not a new `_execute_approved_action()` dispatch — Story 9.1's Task 4
already routes every `backup_action_ids` member through
`backup_engine.run()`).

Deliberately the simplest Epic 9 module: monmap/osdmap/crushmap/auth/
config outputs are all a few KB-MB, never multi-GB like an RBD export, so
`worker/executor/ssh_executor.py::execute_command()`'s buffer-the-whole-
output-in-RAM behavior is safe here — no chunk-streaming
(`_ProgressTrackingReader`) needed, unlike `engine.py`'s RBD path.
"""

from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime

from config.settings import settings
from shared import db
from shared.models import BackupJob
from worker.backup import ai_analysis
from worker.backup.policy_config import backup_targets_from_policy
from worker.backup.storage.factory import get_backend
from worker.executor.ssh_executor import execute_command

logger = logging.getLogger(__name__)

# (artifact name, shell command) — output written to
# `metadata/{timestamp}/{artifact name}` on each configured BackupTarget.
_ARTIFACTS = [
    ("monmap.bin", "ceph mon getmap -o -"),
    ("osdmap.bin", "ceph osd getmap -o -"),
    ("crushmap.bin", "ceph osd getcrushmap -o -"),
    ("auth_export.txt", "ceph auth export"),
    ("config_dump.json", "ceph config dump"),
]


def latest_successful_metadata_job() -> BackupJob | None:
    """Story 9.7, Task 2 — the DR `_phase_restore_metadata` phase needs the
    most recent successful metadata backup to restore from. `remote_key`
    on a metadata BackupJob is the PREFIX directory (`metadata/{timestamp}`),
    not a single artifact key — see `download_artifact()` below."""
    with db.SessionLocal() as session:
        return (
            session.query(BackupJob)
            .filter(BackupJob.job_type == "metadata", BackupJob.status == "SUCCESS")
            .order_by(BackupJob.created_at.desc())
            .first()
        )


def download_artifact(backend, remote_key_prefix: str, artifact_name: str) -> bytes:
    """Downloads one named artifact (see `_ARTIFACTS` above for the valid
    names) from under a metadata BackupJob's `remote_key` prefix."""
    buf = io.BytesIO()
    backend.download(f"{remote_key_prefix}/{artifact_name}", buf)
    return buf.getvalue()


def _first_mon_node() -> str:
    nodes = [n.strip() for n in settings.ceph_mon_nodes.split(",") if n.strip()]
    if not nodes:
        raise RuntimeError("ceph_mon_nodes is not configured — cannot back up cluster metadata")
    return nodes[0]


def run(
    action_pk: str, action_params: dict, incident_id: str, write_progress, check_kill_switch
) -> bool:
    if check_kill_switch(incident_id):
        logger.warning("backup_metadata.run: kill-switch is ON — skipping metadata backup")
        return False

    mon_ip = _first_mon_node()
    prefix = f"metadata/{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    run_id_base = prefix

    progress = [
        {"step": name, "status": "pending", "message": None, "started_at": None, "finished_at": None}
        for name, _cmd in _ARTIFACTS
    ]
    write_progress(action_pk, progress)

    collected: dict[str, bytes] = {}
    for index, (name, cmd) in enumerate(_ARTIFACTS):
        progress[index]["status"] = "running"
        progress[index]["started_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)
        try:
            output = execute_command(mon_ip, cmd)
        except Exception as exc:
            logger.exception("backup_metadata.run: command failed for %s (%s)", name, cmd)
            progress[index]["status"] = "failed"
            progress[index]["message"] = str(exc)
            write_progress(action_pk, progress)
            _record_failure(run_id_base, f"{name}: {exc}")
            return False
        collected[name] = output.encode() if isinstance(output, str) else output
        progress[index]["status"] = "done"
        progress[index]["finished_at"] = datetime.utcnow().isoformat()
        write_progress(action_pk, progress)

    targets = backup_targets_from_policy()
    if not targets:
        logger.error("backup_metadata.run: no backup_targets configured in backup_policy.yaml")
        _record_failure(run_id_base, "no backup_targets configured")
        return False

    for target in targets:
        slot = target["slot"]
        backend = get_backend(slot, settings, immutable_enabled=bool(target.get("immutable")))
        for name, content in collected.items():
            remote_key = f"{prefix}/{name}"
            sha256 = hashlib.sha256(content).hexdigest()
            try:
                result = backend.upload(io.BytesIO(content), remote_key)
                if not backend.verify(remote_key, result.size, result.sha256):
                    raise RuntimeError(f"verify() failed after upload to slot {slot} for {remote_key}")
                if result.sha256 != sha256:
                    raise RuntimeError(f"sha256 mismatch after upload to slot {slot} for {remote_key}")
            except Exception as exc:
                logger.exception(
                    "backup_metadata.run: upload/verify failed for slot %s, key %s", slot, remote_key
                )
                _record_failure(run_id_base, str(exc), backup_target_slot=slot)
                return False

        with db.SessionLocal() as session:
            session.add(
                BackupJob(
                    run_id=run_id_base,
                    job_type="metadata",
                    status="SUCCESS",
                    backup_target_slot=slot,
                    remote_key=prefix,
                    finished_at=datetime.utcnow(),
                )
            )
            session.commit()

    return True


def _record_failure(run_id: str, message: str, backup_target_slot: str | None = None) -> None:
    with db.SessionLocal() as session:
        failed_job = BackupJob(
            run_id=run_id,
            job_type="metadata",
            status="FAILED",
            backup_target_slot=backup_target_slot,
            error_message=message,
            finished_at=datetime.utcnow(),
        )
        session.add(failed_job)
        session.commit()
        # Story 9.5 (AC #1, #2, #5): AI analysis for every failure, same as
        # worker/backup/engine.py's RBD path.
        ai_analysis.analyze_backup_job(failed_job)
