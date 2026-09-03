import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config.settings import settings
from shared import env_config
from shared.models import Cluster

logger = logging.getLogger(__name__)

DEFAULT_CLUSTER_NAME_FALLBACK = "Cụm mặc định"

_DEFAULT_CLUSTER_SETTING_FIELDS = (
    "ceph_mon_nodes",
    "ceph_mgr_nodes",
    "ceph_osd_nodes",
    "ceph_rgw_nodes",
    "ceph_container_name",
    "ceph_keyring_path",
    "ceph_rgw_container_name",
    "ceph_mon_hostnames",
    "ceph_osd_container_name",
    "ssh_user",
    "ssh_key_path",
    "ceph_exec_mode",
)


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
        ceph_mgr_nodes=settings.ceph_mgr_nodes,
        ceph_osd_nodes=settings.ceph_osd_nodes,
        ceph_rgw_nodes=settings.ceph_rgw_nodes,
        ceph_container_name=settings.ceph_container_name,
        ceph_keyring_path=settings.ceph_keyring_path,
        ceph_rgw_container_name=settings.ceph_rgw_container_name,
        ceph_mon_hostnames=settings.ceph_mon_hostnames,
        ceph_osd_container_name=settings.ceph_osd_container_name,
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
    # The committed identity already has the generated primary key and all
    # values supplied above. Avoid a second query/refresh here: TestClient
    # lifespans may replace the in-memory SQLite binding while startup is
    # still unwinding, which made a healthy committed row appear to vanish.
    return cluster


def sync_default_cluster_from_settings(session: Session) -> Cluster:
    """Persist the current .env-backed default-cluster connection in DB.

    ``ensure_default_cluster`` intentionally remains an idempotent seed: read
    paths must not unexpectedly write on every request.  Settings mutations
    call this explicit synchronizer so the DB row used by the multi-cluster
    dashboard/watcher cannot retain an older connection.
    """
    cluster = ensure_default_cluster(session)
    cluster.name = settings.cluster_name.strip() or DEFAULT_CLUSTER_NAME_FALLBACK
    for field in _DEFAULT_CLUSTER_SETTING_FIELDS:
        setattr(cluster, field, getattr(settings, field))
    session.commit()
    return cluster


def sync_default_cluster_from_env(session: Session) -> Cluster:
    """Refresh the default-cluster DB mirror from the current .env file.

    The Worker can update .env after a successful lifecycle action while its
    Settings singleton is still old. Reading the file here keeps Dashboard,
    Watcher and multi-cluster queries from retaining the pre-action node list.
    """
    cluster = ensure_default_cluster(session)
    values = env_config.read_env_values(list(env_config.CLUSTER_ENV_NAMES.values()))
    for field, env_name in env_config.CLUSTER_ENV_NAMES.items():
        # Lifecycle epilogues intentionally write only fields they own
        # (node lists, mode and keyring). Keep unrelated Settings values when
        # their env key is absent instead of replacing them with an empty
        # string.
        if hasattr(cluster, field) and env_name in values:
            setattr(cluster, field, values.get(env_name, ""))
    # ssh_key_path is intentionally not part of CLUSTER_ENV_NAMES; it is a
    # process/server setting rather than lifecycle-managed cluster metadata.
    cluster.ssh_key_path = settings.ssh_key_path
    session.commit()
    return cluster


def get_default_cluster_id(session: Session) -> str:
    return ensure_default_cluster(session).id


def list_active_clusters(session: Session) -> list[Cluster]:
    """Every cluster Watcher should poll — the default cluster plus any
    additional ones added via dashboard/routes/clusters.py, excluding
    soft-deactivated rows."""
    return list(session.scalars(select(Cluster).where(Cluster.is_active.is_(True))))
