from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.bluestore_omap_monitor as bom
# 2026-08-20: resolve_osd_hosts đã dọn sang watcher/osd_hosts.py để
# watcher/collector.py dùng chung — patch đúng nơi hàm thật sự sống.
import watcher.osd_hosts as osd_hosts
from shared import db as db_module
from shared.db import Base
from shared.models import Action, ActionClassification, ActionStatus, AuditEntry, Incident, IncidentStatus


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


def _health(detail_messages=None, code=bom._REAL_CEPH_CODE, present=True):
    if not present:
        return {"status": "HEALTH_OK", "checks": {}}
    messages = detail_messages if detail_messages is not None else [
        "osd.5 legacy (not per-pool) BlueStore omap detected, suggest to run store repair to correct"
    ]
    return {
        "status": "HEALTH_WARN",
        "checks": {code: {"severity": "HEALTH_WARN", "detail": [{"message": m} for m in messages]}},
    }


# --- check_legacy_omap_osds() -----------------------------------------------


def test_check_legacy_omap_osds_empty_when_check_absent():
    assert bom.check_legacy_omap_osds(_health(present=False)) == {}


def test_check_legacy_omap_osds_extracts_single_osd_id():
    result = bom.check_legacy_omap_osds(_health())
    assert set(result.keys()) == {"BLUESTORE_NO_PER_POOL_OMAP:5"}
    assert result["BLUESTORE_NO_PER_POOL_OMAP:5"]["osd_id"] == 5


def test_check_legacy_omap_osds_extracts_multiple_osd_ids_across_detail_lines():
    result = bom.check_legacy_omap_osds(
        _health(
            detail_messages=[
                "osd.5 legacy (not per-pool) BlueStore omap detected, suggest to run store repair to correct",
                "osd.7 legacy (not per-pool) BlueStore omap detected, suggest to run store repair to correct",
            ]
        )
    )
    assert set(result.keys()) == {"BLUESTORE_NO_PER_POOL_OMAP:5", "BLUESTORE_NO_PER_POOL_OMAP:7"}


def test_check_legacy_omap_osds_extracts_multiple_osd_ids_bundled_in_one_line():
    result = bom.check_legacy_omap_osds(
        _health(detail_messages=["osd.5 osd.7 osd.9 have legacy (not per-pool) BlueStore omap"])
    )
    assert set(result.keys()) == {
        "BLUESTORE_NO_PER_POOL_OMAP:5",
        "BLUESTORE_NO_PER_POOL_OMAP:7",
        "BLUESTORE_NO_PER_POOL_OMAP:9",
    }


def test_check_legacy_omap_osds_ignores_unrelated_check():
    result = bom.check_legacy_omap_osds(_health(code="OSD_DOWN"))
    assert result == {}


def test_check_legacy_omap_osds_handles_malformed_health_gracefully():
    assert bom.check_legacy_omap_osds({}) == {}
    assert bom.check_legacy_omap_osds({"checks": None}) == {}
    assert bom.check_legacy_omap_osds({"checks": {bom._REAL_CEPH_CODE: "not a dict"}}) == {}


# --- resolve_osd_hosts() -----------------------------------------------------


def _node(host, roles):
    return {"host": host, "roles": roles}


def test_resolve_osd_hosts_maps_id_to_the_host_that_actually_has_the_unit(monkeypatch):
    monkeypatch.setattr(
        osd_hosts, "configured_nodes", lambda *_a: [_node("10.20.1.112", ["MON", "OSD"]), _node("10.20.1.95", ["OSD"])]
    )

    def fake_run(host, command):
        if host == "10.20.1.112":
            return "ceph-osd@0.service                    loaded active running\n"
        if host == "10.20.1.95":
            return "ceph-osd@1.service                    loaded active running\n"
        raise AssertionError(f"unexpected host: {host}")

    monkeypatch.setattr(osd_hosts.ceph_client, "run_command_on_node", fake_run)

    result = bom.resolve_osd_hosts({0, 1})

    assert result == {0: "10.20.1.112", 1: "10.20.1.95"}


def test_resolve_osd_hosts_matches_cephadm_style_unit_names(monkeypatch):
    monkeypatch.setattr(osd_hosts, "configured_nodes", lambda *_a: [_node("10.20.1.112", ["OSD"])])
    monkeypatch.setattr(
        osd_hosts.ceph_client,
        "run_command_on_node",
        lambda host, cmd: "ceph-48a9efa2@osd.3.service          loaded active running\n",
    )

    assert bom.resolve_osd_hosts({3}) == {3: "10.20.1.112"}


def test_resolve_osd_hosts_skips_non_osd_role_nodes(monkeypatch):
    monkeypatch.setattr(osd_hosts, "configured_nodes", lambda *_a: [_node("10.20.1.150", ["MON"])])
    calls = []
    monkeypatch.setattr(
        osd_hosts.ceph_client, "run_command_on_node", lambda host, cmd: calls.append(host) or ""
    )

    assert bom.resolve_osd_hosts({0}) == {}
    assert calls == []


def test_resolve_osd_hosts_survives_unreachable_host(monkeypatch):
    monkeypatch.setattr(osd_hosts, "configured_nodes", lambda *_a: [_node("10.20.1.112", ["OSD"])])

    def raising(host, cmd):
        raise Exception("connection refused")

    monkeypatch.setattr(osd_hosts.ceph_client, "run_command_on_node", raising)

    assert bom.resolve_osd_hosts({0}) == {}  # must not raise


def test_resolve_osd_hosts_stops_early_once_every_id_resolved(monkeypatch):
    monkeypatch.setattr(
        osd_hosts, "configured_nodes", lambda *_a: [_node("10.20.1.112", ["OSD"]), _node("10.20.1.95", ["OSD"])]
    )
    calls = []

    def fake_run(host, cmd):
        calls.append(host)
        return "ceph-osd@0.service loaded active running\n"

    monkeypatch.setattr(osd_hosts.ceph_client, "run_command_on_node", fake_run)

    result = bom.resolve_osd_hosts({0})

    assert result == {0: "10.20.1.112"}
    assert calls == ["10.20.1.112"]  # never probed the 2nd host once 0 was found


# --- create_or_resolve_bluestore_incidents() --------------------------------


def _detail(osd_id=5):
    return {"osd_id": osd_id, "raw_messages": [f"osd.{osd_id} legacy (not per-pool) BlueStore omap"]}


def test_create_or_resolve_creates_incident_and_action(isolated_db, monkeypatch):
    monkeypatch.setattr(bom, "resolve_osd_hosts", lambda ids: {5: "10.20.1.112"})
    current = {"BLUESTORE_NO_PER_POOL_OMAP:5": _detail()}

    bom.create_or_resolve_bluestore_incidents(current)

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="BLUESTORE_NO_PER_POOL_OMAP:5").one()
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert "osd.5" in incident.log_excerpt

        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert action.action_id == "bluestore_omap_quick_fix"
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.target_nodes == '["10.20.1.112"]'
        assert action.action_params == '{"osd_id": 5}'

        audit_entry = session.query(AuditEntry).filter_by(incident_id=incident.id).one()
        assert audit_entry.event_type == "risky_action_pending_approval"
        assert audit_entry.actor == "system"


def test_create_or_resolve_skips_when_host_cannot_be_resolved(isolated_db, monkeypatch):
    monkeypatch.setattr(bom, "resolve_osd_hosts", lambda ids: {})  # can't find it anywhere
    current = {"BLUESTORE_NO_PER_POOL_OMAP:5": _detail()}

    bom.create_or_resolve_bluestore_incidents(current)

    with db_module.SessionLocal() as session:
        assert session.query(Incident).filter_by(ceph_code="BLUESTORE_NO_PER_POOL_OMAP:5").count() == 0


def test_create_or_resolve_does_not_duplicate_an_already_open_incident(isolated_db, monkeypatch):
    monkeypatch.setattr(bom, "resolve_osd_hosts", lambda ids: {5: "10.20.1.112"})
    current = {"BLUESTORE_NO_PER_POOL_OMAP:5": _detail()}

    bom.create_or_resolve_bluestore_incidents(current)
    bom.create_or_resolve_bluestore_incidents(current)

    with db_module.SessionLocal() as session:
        count = session.query(Incident).filter_by(ceph_code="BLUESTORE_NO_PER_POOL_OMAP:5").count()
        assert count == 1


def test_create_or_resolve_resolves_when_no_longer_a_candidate(isolated_db, monkeypatch):
    monkeypatch.setattr(bom, "resolve_osd_hosts", lambda ids: {5: "10.20.1.112"})
    bom.create_or_resolve_bluestore_incidents({"BLUESTORE_NO_PER_POOL_OMAP:5": _detail()})

    bom.create_or_resolve_bluestore_incidents({})  # fixed, or osd gone

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="BLUESTORE_NO_PER_POOL_OMAP:5").one()
        assert incident.status == IncidentStatus.RESOLVED.value


def test_create_or_resolve_only_touches_its_own_ceph_code_family(isolated_db, monkeypatch):
    monkeypatch.setattr(bom, "resolve_osd_hosts", lambda ids: {})
    with db_module.SessionLocal() as session:
        unrelated = Incident(
            ceph_code="OSD_DOWN", status=IncidentStatus.FAILED.value, detected_at=datetime.utcnow()
        )
        session.add(unrelated)
        session.commit()
        unrelated_id = unrelated.id

    bom.create_or_resolve_bluestore_incidents({})

    with db_module.SessionLocal() as session:
        assert session.get(Incident, unrelated_id).status == IncidentStatus.FAILED.value


def test_create_or_resolve_does_not_re_resolve_hosts_for_already_open_incidents(isolated_db, monkeypatch):
    """resolve_osd_hosts should only be called for genuinely NEW codes, not
    ones that already have an open Incident -- avoids an unnecessary SSH
    probe every single scan for an osd_id already proposed."""
    monkeypatch.setattr(bom, "resolve_osd_hosts", lambda ids: {5: "10.20.1.112"})
    bom.create_or_resolve_bluestore_incidents({"BLUESTORE_NO_PER_POOL_OMAP:5": _detail()})

    calls = []
    monkeypatch.setattr(bom, "resolve_osd_hosts", lambda ids: calls.append(ids) or {})
    bom.create_or_resolve_bluestore_incidents({"BLUESTORE_NO_PER_POOL_OMAP:5": _detail()})

    assert calls == []
