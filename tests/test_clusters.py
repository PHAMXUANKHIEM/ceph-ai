from shared.clusters import ensure_default_cluster, get_default_cluster_id, list_active_clusters
from shared.models import Cluster


def test_ensure_default_cluster_seeds_from_settings(db_session, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.10,10.20.1.11", raising=False)
    monkeypatch.setattr(settings, "cluster_name", "cluster-hcm-01", raising=False)

    cluster = ensure_default_cluster(db_session)

    assert cluster.is_default is True
    assert cluster.is_active is True
    assert cluster.name == "cluster-hcm-01"
    assert cluster.ceph_mon_nodes == "10.20.1.10,10.20.1.11"


def test_ensure_default_cluster_falls_back_to_default_name_when_cluster_name_blank(db_session, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "cluster_name", "", raising=False)

    cluster = ensure_default_cluster(db_session)

    assert cluster.name  # non-empty fallback, not blank


def test_ensure_default_cluster_is_idempotent(db_session):
    first = ensure_default_cluster(db_session)
    second = ensure_default_cluster(db_session)

    assert first.id == second.id
    assert db_session.query(Cluster).filter_by(is_default=True).count() == 1


def test_only_one_default_cluster_row_allowed(db_session):
    """The migration's partial unique index (uq_clusters_single_default) is
    the real guard shared/clusters.py::ensure_default_cluster relies on to
    stay race-safe across Watcher/Worker/Dashboard starting simultaneously —
    this confirms the constraint actually exists and is enforced, not just
    assumed."""
    import uuid

    from sqlalchemy.exc import IntegrityError

    ensure_default_cluster(db_session)

    db_session.add(
        Cluster(
            id=str(uuid.uuid4()),
            name="second-default-attempt",
            ceph_mon_nodes="10.20.1.99",
            ceph_container_name="",
            ssh_user="root",
            ssh_key_path="/root/.ssh/key",
            ceph_exec_mode="docker",
            is_default=True,
            is_active=True,
        )
    )
    try:
        db_session.commit()
        assert False, "expected IntegrityError from the partial unique index"
    except IntegrityError:
        db_session.rollback()


def test_get_default_cluster_id_returns_the_seeded_row_id(db_session):
    cluster = ensure_default_cluster(db_session)
    assert get_default_cluster_id(db_session) == cluster.id


def test_list_active_clusters_excludes_deactivated(db_session):
    import uuid

    default_cluster = ensure_default_cluster(db_session)
    active_extra = Cluster(
        id=str(uuid.uuid4()),
        name="cluster-b",
        ceph_mon_nodes="10.20.2.10",
        ceph_container_name="",
        ssh_user="root",
        ssh_key_path="/root/.ssh/key",
        ceph_exec_mode="cephadm",
        is_default=False,
        is_active=True,
    )
    inactive_extra = Cluster(
        id=str(uuid.uuid4()),
        name="cluster-c",
        ceph_mon_nodes="10.20.3.10",
        ceph_container_name="",
        ssh_user="root",
        ssh_key_path="/root/.ssh/key",
        ceph_exec_mode="cephadm",
        is_default=False,
        is_active=False,
    )
    db_session.add_all([active_extra, inactive_extra])
    db_session.commit()

    active_ids = {c.id for c in list_active_clusters(db_session)}

    assert active_ids == {default_cluster.id, active_extra.id}
