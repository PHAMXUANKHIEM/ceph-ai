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
        3: {
            "host": "node2",
            "bytes_used": 1000 * 1024,
            "bytes_total": 2000 * 1024,
            "pgs": 42,
            "stats_json": '{"device_class":null,"crush_weight":null,"reweight":null,"size_bytes":2048000,"raw_used_bytes":1024000,"data_bytes":null,"omap_bytes":null,"meta_bytes":null,"avail_bytes":null,"used_percent":null,"variance":null,"pgs":42,"status":null}',
        }
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


def test_osd_df_details_are_kept_for_the_crush_table_including_a_down_osd(monkeypatch):
    """Real `ceph osd df` rows from CS-STAGING (12/10/2026), osd.2 stopped to simulate a failed disk."""
    import json

    payload = _fake_osd_df([
        {"id": 0, "name": "osd.0", "device_class": "hdd", "crush_weight": 0.0194854736328125, "reweight": 1,
         "kb": 20951040, "kb_used": 13062008, "kb_used_data": 12585328, "kb_used_omap": 1, "kb_used_meta": 476670,
         "kb_avail": 7889032, "utilization": 62.345391923264906, "var": 1.014401352907079, "pgs": 241, "status": "up"},
        {"id": 2, "name": "osd.2", "device_class": "hdd", "crush_weight": 0.0194854736328125, "reweight": 0,
         "kb": 0, "kb_used": 0, "kb_used_data": 0, "kb_used_omap": 0, "kb_used_meta": 0, "kb_avail": 0,
         "utilization": 0, "var": 0, "pgs": 0, "status": "down"},
    ])
    monkeypatch.setattr(cdm.ceph_client, "run_ceph_json_command", lambda _cmd: ("mon1", payload))
    monkeypatch.setattr(cdm.ceph_client, "list_osds", lambda: [
        {"osd_id": 0, "crush_host": "rnd-khiempx-ceph-1"}, {"osd_id": 2, "crush_host": "rnd-khiempx-ceph-3"}])

    result = cdm.collect_osd_distribution()

    up, down = json.loads(result[0]["stats_json"]), json.loads(result[2]["stats_json"])
    assert up == {"device_class": "hdd", "crush_weight": 0.0194854736328125, "reweight": 1,
                  "size_bytes": 20951040 * 1024, "raw_used_bytes": 13062008 * 1024, "data_bytes": 12585328 * 1024,
                  "omap_bytes": 1024, "meta_bytes": 476670 * 1024, "avail_bytes": 7889032 * 1024,
                  "used_percent": 62.345391923264906, "variance": 1.014401352907079, "pgs": 241, "status": "up"}
    assert (down["status"], down["reweight"], down["pgs"], down["size_bytes"]) == ("down", 0, 0, 0)
    assert result[2]["host"] == "rnd-khiempx-ceph-3"


def test_sync_distribution_stores_the_osd_df_details(isolated_db, monkeypatch):
    import json

    monkeypatch.setattr(cdm, "collect_osd_distribution", lambda cluster=None: {
        5: {"host": "h", "bytes_used": 1, "bytes_total": 2, "pgs": 3,
            "stats_json": json.dumps({"status": "down", "reweight": 0})}})
    with db_module.SessionLocal() as session:
        cluster_id = ensure_default_cluster(session).id

    cdm.sync_distribution(cluster_id)

    with db_module.SessionLocal() as session:
        row = session.query(CrushOsdDistribution).filter_by(osd_id=5).one()
        assert json.loads(row.stats_json) == {"status": "down", "reweight": 0}
