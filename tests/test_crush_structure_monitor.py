import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db as db_module
from shared.db import Base
from shared.models import CrushStructureSnapshot
from watcher import crush_structure_monitor as csm
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
    yield engine


def _fake_crush_dump(host_weight=65536, osd_weight=65536, host_id=-3, osd_id=0, host_name="node1"):
    return {
        "devices": [{"id": osd_id, "name": f"osd.{osd_id}", "class": "hdd"}],
        "buckets": [
            {
                "id": -1,
                "name": "default",
                "type_name": "root",
                "weight": host_weight,
                "items": [{"id": host_id, "weight": host_weight, "pos": 0}],
            },
            {
                "id": host_id,
                "name": host_name,
                "type_name": "host",
                "weight": host_weight,
                "items": [{"id": osd_id, "weight": osd_weight, "pos": 0}],
            },
        ],
        "rules": [{"rule_id": 0, "rule_name": "replicated_rule", "type": 1, "min_size": 1, "max_size": 10,
                   "steps": [{"op": "take", "item": -1, "item_name": "default"},
                             {"op": "chooseleaf_firstn", "num": 0, "type": "host"}, {"op": "emit"}]}],
    }


# --- capture_crush_structure() ----------------------------------------------


def test_capture_crush_structure_returns_none_on_query_error(monkeypatch):
    def raise_error(_cmd):
        raise CephQueryError("all MON nodes failed")

    monkeypatch.setattr(csm.ceph_client, "run_ceph_json_command", raise_error)
    assert csm.capture_crush_structure() is None


def test_capture_crush_structure_builds_tree(monkeypatch):
    monkeypatch.setattr(
        csm.ceph_client, "run_ceph_json_command", lambda _cmd: ("mon1", _fake_crush_dump())
    )
    tree = csm.capture_crush_structure()
    assert tree is not None
    roots = tree["roots"]
    assert len(roots) == 1
    assert roots[0]["type"] == "root"
    host = roots[0]["children"][0]
    assert host["type"] == "host"
    assert host["name"] == "node1"
    osd = host["children"][0]
    assert osd["type"] == "osd"
    assert osd["name"] == "osd.0"
    assert tree["rules"][0]["rule_name"] == "replicated_rule"
    assert tree["rules"][0]["steps"][1] == {"op": "chooseleaf_firstn", "num": 0, "type": "host"}


# --- _canonicalize() — array order must not affect comparison (AD-26) ------


def test_canonicalize_ignores_child_array_order():
    tree_a = {
        "roots": [
            {"id": -1, "name": "default", "type": "root", "weight": 1, "children": [
                {"id": 1, "name": "b", "type": "host", "weight": 1, "children": []},
                {"id": 2, "name": "a", "type": "host", "weight": 1, "children": []},
            ]}
        ]
    }
    tree_b = {
        "roots": [
            {"id": -1, "name": "default", "type": "root", "weight": 1, "children": [
                {"id": 2, "name": "a", "type": "host", "weight": 1, "children": []},
                {"id": 1, "name": "b", "type": "host", "weight": 1, "children": []},
            ]}
        ]
    }
    assert csm._canonicalize(tree_a) == csm._canonicalize(tree_b)


def test_canonicalize_detects_real_weight_difference():
    tree_a = {"roots": [{"id": -1, "name": "r", "type": "root", "weight": 1, "children": []}]}
    tree_b = {"roots": [{"id": -1, "name": "r", "type": "root", "weight": 2, "children": []}]}
    assert csm._canonicalize(tree_a) != csm._canonicalize(tree_b)


# --- _compute_diff() ---------------------------------------------------------


def test_compute_diff_detects_added_and_removed_osd():
    old_tree = {"roots": [{"id": -1, "name": "r", "type": "root", "weight": 1, "children": [
        {"id": 0, "name": "osd.0", "type": "osd", "weight": 1, "children": []},
    ]}]}
    new_tree = {"roots": [{"id": -1, "name": "r", "type": "root", "weight": 1, "children": [
        {"id": 1, "name": "osd.1", "type": "osd", "weight": 1, "children": []},
    ]}]}
    diff = csm._compute_diff(old_tree, new_tree)
    assert {"id": 1, "name": "osd.1", "type": "osd", "weight": 1} in diff["added"]
    assert {"id": 0, "name": "osd.0", "type": "osd", "weight": 1} in diff["removed"]
    assert diff["reweighted"] == []


def test_compute_diff_detects_reweight():
    old_tree = {"roots": [{"id": 0, "name": "osd.0", "type": "osd", "weight": 1, "children": []}]}
    new_tree = {"roots": [{"id": 0, "name": "osd.0", "type": "osd", "weight": 2, "children": []}]}
    diff = csm._compute_diff(old_tree, new_tree)
    assert diff["added"] == []
    assert diff["removed"] == []
    assert diff["reweighted"] == [
        {"id": 0, "name": "osd.0", "type": "osd", "old_weight": 1, "new_weight": 2}
    ]


def test_compute_diff_empty_bucket_add_counts_as_diff():
    # AC #4's reverse: an EMPTY bucket (no OSD/child) being added must still
    # register as a structural diff, not just OSD/Weight-level changes.
    old_tree = {"roots": []}
    new_tree = {"roots": [{"id": -5, "name": "rack1", "type": "rack", "weight": 0, "children": []}]}
    diff = csm._compute_diff(old_tree, new_tree)
    assert diff["added"] == [{"id": -5, "name": "rack1", "type": "rack", "weight": 0}]


# --- scan_and_store() ---------------------------------------------------------


def test_scan_and_store_noop_on_query_error(isolated_db, monkeypatch):
    monkeypatch.setattr(csm, "capture_crush_structure", lambda: None)

    csm.scan_and_store()

    with db_module.SessionLocal() as session:
        assert session.query(CrushStructureSnapshot).count() == 0


def test_scan_and_store_first_snapshot_has_no_diff(isolated_db, monkeypatch):
    tree = {"roots": [{"id": 0, "name": "osd.0", "type": "osd", "weight": 1, "children": []}]}
    monkeypatch.setattr(csm, "capture_crush_structure", lambda: tree)

    csm.scan_and_store()

    with db_module.SessionLocal() as session:
        snapshot = session.query(CrushStructureSnapshot).one()
        assert snapshot.diff_json is None
        assert json.loads(snapshot.tree_json)["roots"][0]["name"] == "osd.0"


def test_scan_and_store_dedups_identical_structure(isolated_db, monkeypatch):
    tree = {"roots": [{"id": 0, "name": "osd.0", "type": "osd", "weight": 1, "children": []}]}
    monkeypatch.setattr(csm, "capture_crush_structure", lambda: tree)

    csm.scan_and_store()
    csm.scan_and_store()

    with db_module.SessionLocal() as session:
        assert session.query(CrushStructureSnapshot).count() == 1


def test_scan_and_store_writes_new_row_and_diff_on_real_change(isolated_db, monkeypatch):
    tree_v1 = {"roots": [{"id": 0, "name": "osd.0", "type": "osd", "weight": 1, "children": []}]}
    tree_v2 = {"roots": [{"id": 0, "name": "osd.0", "type": "osd", "weight": 2, "children": []}]}

    monkeypatch.setattr(csm, "capture_crush_structure", lambda: tree_v1)
    csm.scan_and_store()
    monkeypatch.setattr(csm, "capture_crush_structure", lambda: tree_v2)
    csm.scan_and_store()

    with db_module.SessionLocal() as session:
        assert session.query(CrushStructureSnapshot).count() == 2
        latest = (
            session.query(CrushStructureSnapshot)
            .order_by(CrushStructureSnapshot.created_at.desc())
            .first()
        )
        diff = json.loads(latest.diff_json)
        assert diff["reweighted"] == [
            {"id": 0, "name": "osd.0", "type": "osd", "old_weight": 1, "new_weight": 2}
        ]


def test_scan_and_store_up_down_only_change_is_not_a_diff(isolated_db, monkeypatch):
    # crush_structure_monitor never reads OSD up/down status at all — the
    # tree shape has no such field — so two scans with identical Weight/
    # position must never produce a diff regardless of real cluster health.
    tree = {"roots": [{"id": 0, "name": "osd.0", "type": "osd", "weight": 1, "children": []}]}
    monkeypatch.setattr(csm, "capture_crush_structure", lambda: tree)

    csm.scan_and_store()
    csm.scan_and_store()
    csm.scan_and_store()

    with db_module.SessionLocal() as session:
        assert session.query(CrushStructureSnapshot).count() == 1
