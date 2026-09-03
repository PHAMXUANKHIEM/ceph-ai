"""Scheduler (Story 9.1, Architecture AD-11 as corrected after reading the
real codebase) — runs as a THIRD coroutine inside `worker/main.py::
_main()`'s existing `asyncio.gather(run(...), poll_approved_actions())`,
not a separate process or RabbitMQ queue. Schedule state persists via
APScheduler's `SQLAlchemyJobStore(engine=shared.db.engine)` — reusing the
app's own DB/engine, not a second one — so a Worker restart doesn't lose
the schedule.

When a job is due, it creates a synthetic `Incident`+`Action` (same
"propose-and-immediately-execute" pattern Chat-with-AI/Story 6.1
established for actions that don't originate from a real detected health
Incident) and calls DIRECTLY into `worker/llm/router_client.py::
_execute_approved_action()` — no queue, no separate poll loop for this.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from shared import db
from config.settings import settings
from shared.clusters import list_active_clusters
from shared.models import Action, ActionClassification, ActionStatus, Cluster, Incident, IncidentStatus
from worker.backup import alerting, digest
from worker import ai_ops_digest
from worker.backup.cluster_scope import first_mon_node, get_cluster, parse_tracked_images
from worker.backup.policy_config import load_backup_policy

logger = logging.getLogger(__name__)

BACKUP_SCHEDULED_CEPH_CODE = "BACKUP_SCHEDULED"
# How often build_scheduler() itself is a no-op no-arg poll of "is there
# anything to do" — the coroutine wrapper below just needs to stay alive
# for asyncio.gather; APScheduler's own thread/event-loop integration does
# the actual waking-up-at-cron-time.
IDLE_SLEEP_SECONDS = 3600


def _target_fields_ready(source, prefix: str) -> bool:
    """Return True only when a target has all fields needed to construct
    and use its backend.  Scheduling against a blank/partial target creates
    guaranteed failures every tick, so the scheduler deliberately fails
    closed until configuration is complete."""
    transport = getattr(source, f"{prefix}_transport", "")
    if transport == "ssh":
        fields = ("ssh_host", "ssh_user", "ssh_key_path", "ssh_landing_dir")
    elif transport == "s3":
        # An empty endpoint selects the real AWS S3 endpoint in
        # storage/s3_backend.py; only S3-compatible services such as MinIO
        # need this field populated.
        fields = ("s3_access_key", "s3_secret_key", "s3_bucket")
    else:
        return False
    return all(getattr(source, f"{prefix}_{field}", None) for field in fields)


def _default_backup_target_ready(policy: dict) -> bool:
    if not isinstance(policy, dict):
        logger.error("scheduler: backup policy must be a mapping; default targets are not ready")
        return False
    try:
        required_copy_count = max(int(policy.get("required_copy_count") or 1), 1)
    except (TypeError, ValueError):
        logger.error(
            "scheduler: invalid required_copy_count=%r; default targets are not ready",
            policy.get("required_copy_count"),
        )
        return False
    targets = policy.get("backup_targets")
    if not isinstance(targets, list):
        logger.error("scheduler: backup_targets must be a list; default targets are not ready")
        return False
    ready_slots = {
        slot
        for target in targets
        if isinstance(target, dict)
        and (slot := target.get("slot")) in ("a", "b")
        and _target_fields_ready(settings, f"backup_target_{slot}")
    }
    return len(ready_slots) >= required_copy_count


def _cluster_backup_target_ready(cluster: Cluster) -> bool:
    return _target_fields_ready(cluster, "backup")


def _first_mon_node(cluster: "Cluster | None" = None) -> str:
    try:
        return first_mon_node(cluster)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def _create_scheduled_action(
    action_id: str, action_params: dict, log_excerpt: str, cluster_id: str | None = None
) -> str:
    """Creates the synthetic Incident+Action a scheduled job runs as,
    already APPROVED (Safe, no operator step) so it can go straight to
    `_execute_approved_action()`. Returns the new Action's primary key.
    Shared by `trigger_backup` (rbd_backup_run) and `trigger_metadata_backup`
    (backup_metadata_run, Story 9.3) — same shape, different action_id/params.

    `target_nodes` MUST be a non-empty single-host list — verified against
    a real, previously-shipped bug (dashboard/routes/volumes.py's own
    2026-07-28 fix comment): `_execute_approved_action` rejects an empty/
    missing `target_nodes` as "malformed" and marks the Action FAILED
    before ever reaching ANY orchestrator branch, cluster_deploy/
    volume_perf/backup_engine alike — `worker/backup/engine.py`/
    `worker/backup/metadata.py` themselves never read this column (they
    resolve their own mon node from `action_params`/settings), this exists
    purely to pass that gate.

    `cluster_id` (multi-tenant remediation Phase 3): `None` means the
    default cluster (byte-for-byte unchanged), else the synthetic
    Incident is stamped with it so `worker/llm/router_client.py::
    _execute_approved_action` resolves the RIGHT cluster's SSH creds/
    backup target when it later dispatches to `backup_engine.run()`.
    """
    cluster = get_cluster(cluster_id)
    mon_ip = _first_mon_node(cluster)
    with db.SessionLocal() as session:
        incident = Incident(
            cluster_id=cluster_id,
            ceph_code=BACKUP_SCHEDULED_CEPH_CODE,
            status=IncidentStatus.EXECUTING.value,
            log_excerpt=log_excerpt,
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification=ActionClassification.SAFE.value,
            status=ActionStatus.APPROVED.value,
            target_nodes=json.dumps([mon_ip]),
            action_params=json.dumps(action_params),
        )
        session.add(action)
        session.commit()
        return action.id


async def _dispatch(action_pk: str, error_context: str) -> None:
    """`_execute_approved_action` is synchronous, blocking I/O (SSH/DB)
    that can run for minutes on a large export — routed through
    `asyncio.to_thread`, same as `worker/llm/router_client.py::
    poll_approved_actions()` already does for its own blocking dispatch,
    so a long-running backup never stalls the Worker's shared event loop
    (RabbitMQ consumer, approval poller)."""
    from worker.llm.router_client import _execute_approved_action

    try:
        await asyncio.to_thread(_execute_approved_action, action_pk)
    except Exception:
        logger.exception("scheduler: unexpected error executing %s", error_context)


async def trigger_backup(pool: str, image: str, cluster_id: str | None = None) -> None:
    """The APScheduler job callable for a scheduled RBD backup. `cluster_id`
    (Phase 3): `None` means the default cluster, unchanged; an additional
    cluster's own job (registered by `build_scheduler()` below) passes its
    id so the backup runs against THAT cluster."""
    action_pk = _create_scheduled_action(
        "rbd_backup_run",
        {"pool": pool, "image": image},
        f"Backup RBD theo lịch cho {pool}/{image}",
        cluster_id=cluster_id,
    )
    await _dispatch(action_pk, f"scheduled RBD backup for {pool}/{image} (cluster_id={cluster_id})")


async def trigger_metadata_backup(cluster_id: str | None = None) -> None:
    """The APScheduler job callable for a scheduled cluster metadata
    backup (Story 9.3) — not tied to a (pool, image) pair. `cluster_id`:
    see `trigger_backup`'s own docstring."""
    action_pk = _create_scheduled_action(
        "backup_metadata_run",
        {},
        "Backup metadata cụm theo lịch (monmap/osdmap/crushmap/auth/config)",
        cluster_id=cluster_id,
    )
    await _dispatch(action_pk, f"scheduled cluster metadata backup (cluster_id={cluster_id})")


async def trigger_restore_drill() -> None:
    """The APScheduler job callable for the periodic RestoreDrill
    (Story 9.4, PRD FR-10) — `worker/backup/restore_drill.py` itself reads
    which (pool, image)/scratch target to use from `backup_policy.yaml`'s
    `restore_drill:` section, so no params are needed here."""
    action_pk = _create_scheduled_action(
        "restore_drill_execute", {}, "RestoreDrill theo lịch"
    )
    await _dispatch(action_pk, "scheduled RestoreDrill")


def _register_cluster_backup_jobs(
    scheduler: AsyncIOScheduler,
    cron: dict,
    metadata_cron: dict,
    digest_cron: dict,
    desired_job_ids: set[str],
) -> None:
    """Multi-tenant remediation Phase 3 — registers `trigger_backup`/
    `trigger_metadata_backup` jobs for every ADDITIONAL cluster that has
    opted in (`Cluster.backup_enabled`), on the SAME shared global cron as
    the default cluster's own jobs above (Phase 3 does not give each
    cluster its own cron config — explicit narrowing, see this session's
    plan). RestoreDrill stays default-cluster-only."""
    with db.SessionLocal() as session:
        clusters = [
            c for c in list_active_clusters(session)
            if not c.is_default and c.backup_enabled and _cluster_backup_target_ready(c)
        ]
        session.expunge_all()

    for cluster in clusters:
        for pool, image in parse_tracked_images(cluster.backup_tracked_images):
            job_id = f"rbd_backup_{cluster.id}_{pool}_{image}"
            scheduler.add_job(
                trigger_backup,
                trigger=CronTrigger(hour=cron.get("hour", 2), minute=cron.get("minute", 0)),
                args=[pool, image, cluster.id],
                id=job_id,
                replace_existing=True,
            )
            desired_job_ids.add(job_id)
        if metadata_cron:
            metadata_job_id = f"backup_metadata_run_{cluster.id}"
            scheduler.add_job(
                trigger_metadata_backup,
                trigger=CronTrigger(
                    hour=metadata_cron.get("hour", "*/6"), minute=metadata_cron.get("minute", 0)
                ),
                args=[cluster.id],
                id=metadata_job_id,
                replace_existing=True,
            )
            desired_job_ids.add(metadata_job_id)
        digest_job_id = f"backup_digest_run_{cluster.id}"
        scheduler.add_job(
            digest.run_digest,
            trigger=CronTrigger(hour=digest_cron.get("hour", 7), minute=digest_cron.get("minute", 0)),
            args=[cluster.id],
            id=digest_job_id,
            replace_existing=True,
        )
        desired_job_ids.add(digest_job_id)


def _reconcile_backup_jobs(scheduler: AsyncIOScheduler, desired_job_ids: set[str]) -> None:
    """Remove persisted jobs owned by this module that are no longer
    represented by the current policy/configuration.

    APScheduler's SQLAlchemy job store survives worker restarts; conditional
    ``add_job`` calls alone therefore leave obsolete jobs active forever.
    """
    def is_managed(job_id: str) -> bool:
        return (
            job_id.startswith("rbd_backup_")
            or job_id.startswith("backup_metadata_run")
            or job_id in {
                "restore_drill_execute",
                "backup_alert_check",
                "backup_digest_run",
                "ai_ops_weekly_digest",
            }
        )

    for job in scheduler.get_jobs():
        if is_managed(job.id) and job.id not in desired_job_ids:
            logger.info("scheduler: removing obsolete persisted backup job %s", job.id)
            scheduler.remove_job(job.id)


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_jobstore(SQLAlchemyJobStore(engine=db.engine), alias="default")
    desired_job_ids: set[str] = set()

    policy = load_backup_policy()
    default_target_ready = _default_backup_target_ready(policy)
    schedule = policy.get("schedule") or {}
    cron = schedule.get("cron") or {}
    for tracked in (policy.get("tracked_images") or []) if default_target_ready else []:
        pool = tracked.get("pool")
        image = tracked.get("image")
        if not pool or not image:
            continue
        job_id = f"rbd_backup_{pool}_{image}"
        scheduler.add_job(
            trigger_backup,
            trigger=CronTrigger(hour=cron.get("hour", 2), minute=cron.get("minute", 0)),
            args=[pool, image],
            id=job_id,
            replace_existing=True,
        )
        desired_job_ids.add(job_id)

    metadata_cron = schedule.get("metadata_cron") or {}
    if metadata_cron and default_target_ready:
        scheduler.add_job(
            trigger_metadata_backup,
            trigger=CronTrigger(hour=metadata_cron.get("hour", "*/6"), minute=metadata_cron.get("minute", 0)),
            id="backup_metadata_run",
            replace_existing=True,
        )
        desired_job_ids.add("backup_metadata_run")

    # Multi-tenant remediation Phase 3 — every ADDITIONAL cluster with
    # backup_enabled gets its own rbd_backup_run/backup_metadata_run/backup_digest_run jobs
    # on this SAME shared cron/metadata_cron, registered alongside (never
    # replacing) the default cluster's jobs above.
    digest_cron = schedule.get("digest_cron") or {}
    _register_cluster_backup_jobs(scheduler, cron, metadata_cron, digest_cron, desired_job_ids)

    # Story 9.4 (AC #3): only register if restore_drill is actually
    # configured (pool/image + scratch_pool/scratch_image) — same "blank
    # means not set up yet" posture as tracked_images being empty.
    drill_config = policy.get("restore_drill") or {}
    if default_target_ready and all(
        drill_config.get(k) for k in ("pool", "image", "scratch_pool", "scratch_image")
    ):
        drill_cron = schedule.get("restore_drill_cron") or {}
        scheduler.add_job(
            trigger_restore_drill,
            trigger=CronTrigger(
                day_of_week=drill_cron.get("day_of_week", "mon"),
                hour=drill_cron.get("hour", 3),
                minute=drill_cron.get("minute", 0),
            ),
            id="restore_drill_execute",
            replace_existing=True,
        )
        desired_job_ids.add("restore_drill_execute")

    # Story 9.4 (AC #2): fail/overdue check — a plain sync callable,
    # APScheduler runs it in its own thread-pool executor automatically
    # (same as any blocking job), no asyncio.to_thread wrapper needed here.
    alert_interval = schedule.get("alert_check_interval_minutes", 5)
    scheduler.add_job(
        alerting.check_overdue_and_failed_backups,
        trigger=IntervalTrigger(minutes=alert_interval),
        id="backup_alert_check",
        replace_existing=True,
    )
    desired_job_ids.add("backup_alert_check")

    # Story 9.5 (AC #6): BackupDigest — also a plain sync callable, same
    # thread-pool-executor posture as backup_alert_check above.
    scheduler.add_job(
        digest.run_digest,
        trigger=CronTrigger(hour=digest_cron.get("hour", 7), minute=digest_cron.get("minute", 0)),
        id="backup_digest_run",
        replace_existing=True,
    )
    desired_job_ids.add("backup_digest_run")
    if settings.ai_ops_weekly_digest_enabled:
        weekday = str(settings.ai_ops_weekly_digest_day or "mon").strip().lower()
        if weekday not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
            logger.warning("Invalid AI Ops digest weekday=%r; using mon", weekday)
            weekday = "mon"
        scheduler.add_job(
            ai_ops_digest.run_digest,
            trigger=CronTrigger(
                day_of_week=weekday,
                hour=settings.ai_ops_weekly_digest_hour,
                minute=settings.ai_ops_weekly_digest_minute,
            ),
            id="ai_ops_weekly_digest",
            replace_existing=True,
        )
        desired_job_ids.add("ai_ops_weekly_digest")
    # Keep the desired set on the scheduler so ``run()`` can reconcile jobs
    # loaded from the persistent job store after APScheduler starts.  Before
    # start(), ``get_jobs()`` only exposes jobs queued during construction;
    # persisted jobs are loaded lazily by the job store at startup.
    scheduler._desired_backup_job_ids = desired_job_ids
    _reconcile_backup_jobs(scheduler, desired_job_ids)
    return scheduler


async def run() -> None:
    """Long-running coroutine for `worker/main.py::_main()`'s
    `asyncio.gather` — starts the scheduler (which then fires
    `trigger_backup` on its own cron schedule via the running event loop)
    and blocks until cancelled, the same "runs forever inside gather"
    shape `run()`/`poll_approved_actions()` already have."""
    scheduler = build_scheduler()
    scheduler.start()
    _reconcile_backup_jobs(scheduler, scheduler._desired_backup_job_ids)
    try:
        while True:
            await asyncio.sleep(IDLE_SLEEP_SECONDS)
    except asyncio.CancelledError:
        scheduler.shutdown(wait=False)
        raise
