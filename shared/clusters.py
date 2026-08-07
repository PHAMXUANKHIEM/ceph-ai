import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import settings
from shared.models import Cluster

logger = logging.getLogger(__name__)

DEFAULT_CLUSTER_NAME_FALLBACK = "Cụm mặc định"


def ensure_default_cluster(session: Session) -> Cluster:
    """Idempotently seeds the one `is_default=True` Cluster row from the
    `.env`-configured `settings` singleton — called once at startup by all
    3 processes (Dashboard/Watcher/Worker), so whichever starts first
    creates it and the others just see it already exists.

    Safe under a startup race between processes: `clusters.uq_clusters_
    single_default` (the migration's partial unique index) is the REAL
    guard — a concurrent second insert raises IntegrityError here, which
    this function catches and turns into "re-read what the winner
    committed" rather than a crash. Without that DB-level constraint,
    checking-then-inserting in Python alone would not be race-safe (two
    processes can both see "no default yet" before either commits).

    See shared/models.py::Cluster's docstring for why this row mirrors
    `.env` rather than being edited through the Cluster CRUD UI."""
    existing = session.scalar(select(Cluster).where(Cluster.is_default.is_(True)))
    if existing is not None:
        return existing

    cluster = Cluster(
        name=settings.cluster_name.strip() or DEFAULT_CLUSTER_NAME_FALLBACK,
        ceph_mon_nodes=settings.ceph_mon_nodes,
        ceph_container_name=settings.ceph_container_name,
        ssh_user=settings.ssh_user,
        ssh_key_path=settings.ssh_key_path,
        ceph_exec_mode=settings.ceph_exec_mode,
        is_default=True,
        is_active=True,
    )
    session.add(cluster)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalar(select(Cluster).where(Cluster.is_default.is_(True)))
        if existing is None:
            raise
        logger.info("ensure_default_cluster: lost startup race to another process, reusing its row")
        return existing
    session.refresh(cluster)
    return cluster


def get_default_cluster_id(session: Session) -> str:
    return ensure_default_cluster(session).id


def list_active_clusters(session: Session) -> list[Cluster]:
    """Every cluster Watcher should poll — the default cluster plus any
    additional ones added via dashboard/routes/clusters.py, excluding
    soft-deactivated rows."""
    return list(session.scalars(select(Cluster).where(Cluster.is_active.is_(True))))
