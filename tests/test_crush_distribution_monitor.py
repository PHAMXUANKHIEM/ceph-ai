import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db as db_module
from shared.db import Base
from shared.clusters import ensure_default_cluster
from shared.models import CrushOsdDistribution
from watcher import crush_distribution_monitor as cdm
from watcher.ceph_client import CephQueryError


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    with db_module.SessionLocal() as session:
        cluster_id = ensure_default_cluster(session).id
    yield cluster_id


def _fake_osd_df(entries):
    return {"nodes": entries}


# --- collect_osd_distribution() ---------------------------------------------


def test_collect_osd_distribution_returns_none_on_query_error(monkeypatch):
    def raise_error(_cmd):
        raise CephQueryError("all MON nodes failed")

    monkeypatch.setattr(cdm.ceph_client, "run_ceph_json_command", raise_error)
    assert cdm.collect_osd_distribution() is None


def test_collect_osd_distribution_parses_bytes_and_pgs(monkeypatch):
    payload = _fake_osd_df([{"id": 3, "kb": 2000, "kb_used": 1000, "pgs": 42}])
    monkeypatch.setattr(cdm.ceph_client, "run_ceph_json_command", lambda _cmd: ("mon1", payload))
    monkeypatch.setattr(cdm.ceph_client, "list_osds", lambda: [{"osd_id": 3, "crush_host": "node2"}])

    result = cdm.collect_osd_distribution()

    assert result == {
        3: {"host": "node2", "bytes_used": 1000 * 1024, "bytes_total": 2000 * 1024, "pgs": 42}
    }


def test_collect_osd_distribution_skips_malformed_entries(monkeypatch):
    payload = _fake_osd_df([{"not_an_id_field": True}, {"id": 5, "kb": 100, "kb_used": 10, "pgs": 1}])
    monkeypatch.setattr(cdm.ceph_client, "run_ceph_json_command", lambda _cmd: ("mon1", payload))
    monkeypatch.setattr(cdm.ceph_client, "list_osds", lambda: [{"osd_id": 5, "crush_host": "node1"}])

    result = cdm.collect_osd_distribution()

    assert list(result.keys()) == [5]


def test_collect_osd_distribution_missing_bytes_fields_become_none(monkeypatch):
    payload = _fake_osd_df([{"id": 7, "pgs": 3}])
    monkeypatch.setattr(cdm.ceph_client, "run_ceph_json_command", lambda _cmd: ("mon1", payload))
    monkeypatch.setattr(cdm.ceph_client, "list_osds", lambda: [])

    result = cdm.collect_osd_distribution()

    assert result[7]["bytes_used"] is None
    assert result[7]["bytes_total"] is None
    assert result[7]["host"] is None
    assert result[7]["pgs"] == 3


# --- sync_distribution() ------------------------------------------------------


def test_sync_distribution_noop_on_query_error(isolated_db, monkeypatch):
    cluster_id = isolated_db
    with db_module.SessionLocal() as session:
        session.add(CrushOsdDistribution(cluster_id=cluster_id, osd_id=1, bytes_used=1, bytes_total=2, pgs=1))
        session.commit()

    monkeypatch.setattr(cdm, "collect_osd_distribution", lambda: None)
    cdm.sync_distribution(cluster_id)

    with db_module.SessionLocal() as session:
        # AC #5: a FAILED scan must leave existing data untouched.
        row = session.get(CrushOsdDistribution, (cluster_id, 1))
        assert row is not None
        assert row.bytes_used == 1


def test_sync_distribution_upserts_new_osd(isolated_db, monkeypatch):
    cluster_id = isolated_db
    monkeypatch.setattr(
        cdm, "collect_osd_distribution",
        lambda: {3: {"host": "node2", "bytes_used": 1000, "bytes_total": 2000, "pgs": 42}},
    )

    cdm.sync_distribution(cluster_id)

    with db_module.SessionLocal() as session:
        row = session.get(CrushOsdDistribution, (cluster_id, 3))
        assert row.host == "node2"
        assert row.bytes_used == 1000
        assert row.pgs == 42


def test_sync_distribution_overwrites_existing_osd_in_place(isolated_db, monkeypatch):
    cluster_id = isolated_db
    monkeypatch.setattr(
        cdm, "collect_osd_distribution",
        lambda: {3: {"host": "node2", "bytes_used": 1000, "bytes_total": 2000, "pgs": 42}},
    )
    cdm.sync_distribution(cluster_id)

    monkeypatch.setattr(
        cdm, "collect_osd_distribution",
        lambda: {3: {"host": "node2", "bytes_used": 1500, "bytes_total": 2000, "pgs": 50}},
    )
    cdm.sync_distribution(cluster_id)

    with db_module.SessionLocal() as session:
        assert session.query(CrushOsdDistribution).count() == 1
        row = session.get(CrushOsdDistribution, (cluster_id, 3))
        assert row.bytes_used == 1500
        assert row.pgs == 50


def test_sync_distribution_deletes_osd_confirmed_removed(isolated_db, monkeypatch):
    cluster_id = isolated_db
    # AC #6: a SUCCESSFUL scan that no longer sees a previously-known osd_id
    # means it genuinely left the cluster — the stale row must be deleted,
    # NOT left in place (that would be indistinguishable from AC #5's
    # "scan failed, keep old data" case).
    with db_module.SessionLocal() as session:
        session.add(CrushOsdDistribution(cluster_id=cluster_id, osd_id=1, bytes_used=1, bytes_total=2, pgs=1))
        session.add(CrushOsdDistribution(cluster_id=cluster_id, osd_id=2, bytes_used=1, bytes_total=2, pgs=1))
        session.commit()

    monkeypatch.setattr(
        cdm, "collect_osd_distribution",
        lambda: {2: {"host": "node1", "bytes_used": 5, "bytes_total": 10, "pgs": 2}},
    )
    cdm.sync_distribution(cluster_id)

    with db_module.SessionLocal() as session:
        assert session.get(CrushOsdDistribution, (cluster_id, 1)) is None
        assert session.get(CrushOsdDistribution, (cluster_id, 2)) is not None
