import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db as db_module
from shared.db import Base
from shared.models import CapabilityStatus, ClusterCapabilityInventory
from watcher import capability_inventory as ci
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


def _versions_payload(*version_strings):
    """Shape `ceph versions` actually returns: {daemon_type: {"ceph version X": count}}."""
    payload = {}
    for i, version in enumerate(version_strings):
        payload[f"daemon{i}"] = {f"ceph version {version} (abc) reef (stable)": 1}
    return payload


# --- collect_capability_snapshot() ------------------------------------------


def test_collect_snapshot_unavailable_on_query_error(monkeypatch):
    def raise_error():
        raise CephQueryError("all MON nodes failed")

    monkeypatch.setattr(ci.ceph_client, "summarize_cluster_versions", raise_error)

    snapshot = ci.collect_capability_snapshot()
    assert snapshot["status"] == CapabilityStatus.UNAVAILABLE.value
    assert snapshot["error_message"] == "all MON nodes failed"
    assert snapshot["current_version"] is None


def test_collect_snapshot_supported_single_known_version(monkeypatch):
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(_versions_payload("18.2.2", "18.2.2")),
    )
    monkeypatch.setattr(ci.ceph_client.settings, "ceph_exec_mode", "cephadm")

    snapshot = ci.collect_capability_snapshot()
    assert snapshot["status"] == CapabilityStatus.SUPPORTED.value
    assert snapshot["current_version"] == "18.2.2"
    assert snapshot["current_major"] == 18
    assert snapshot["is_mixed_version"] is False
    assert snapshot["deployment_mode"] == "cephadm"


def test_collect_snapshot_unsupported_version_unknown_major(monkeypatch):
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(_versions_payload("999.1.1")),
    )

    snapshot = ci.collect_capability_snapshot()
    assert snapshot["status"] == CapabilityStatus.UNSUPPORTED_VERSION.value
    assert snapshot["current_version"] == "999.1.1"


def test_collect_snapshot_mixed_version_is_unsupported_verdict(monkeypatch):
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(_versions_payload("17.2.7", "18.2.2")),
    )

    snapshot = ci.collect_capability_snapshot()
    assert snapshot["is_mixed_version"] is True
    assert snapshot["current_version"] is None
    assert snapshot["status"] == CapabilityStatus.UNSUPPORTED_VERSION.value
    assert set(snapshot["distinct_versions"]) == {"17.2.7", "18.2.2"}


# --- scan_and_store() / latest_snapshot() -----------------------------------


def test_scan_and_store_writes_row(isolated_db, monkeypatch):
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(_versions_payload("18.2.2")),
    )
    monkeypatch.setattr(ci.ceph_client.settings, "ceph_exec_mode", "cephadm")

    ci.scan_and_store()

    with db_module.SessionLocal() as session:
        rows = session.query(ClusterCapabilityInventory).all()
        assert len(rows) == 1
        assert rows[0].status == CapabilityStatus.SUPPORTED.value
        assert rows[0].current_version == "18.2.2"
        assert rows[0].deployment_mode == "cephadm"
        assert json.loads(rows[0].distinct_versions_json) == ["18.2.2"]


def test_scan_and_store_always_appends_even_when_unchanged(isolated_db, monkeypatch):
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(_versions_payload("18.2.2")),
    )

    ci.scan_and_store()
    ci.scan_and_store()

    with db_module.SessionLocal() as session:
        assert session.query(ClusterCapabilityInventory).count() == 2


def test_scan_and_store_query_failure_writes_unavailable_row(isolated_db, monkeypatch):
    def raise_error():
        raise CephQueryError("all MON nodes failed")

    monkeypatch.setattr(ci.ceph_client, "summarize_cluster_versions", raise_error)

    ci.scan_and_store()

    with db_module.SessionLocal() as session:
        row = session.query(ClusterCapabilityInventory).one()
        assert row.status == CapabilityStatus.UNAVAILABLE.value
        assert row.error_message == "all MON nodes failed"


def test_latest_snapshot_returns_none_before_first_scan(isolated_db):
    assert ci.latest_snapshot("nonexistent-cluster-id") is None


def test_latest_snapshot_returns_most_recent_row(isolated_db, monkeypatch):
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(_versions_payload("18.2.2")),
    )
    ci.scan_and_store()

    with db_module.SessionLocal() as session:
        from shared.clusters import ensure_default_cluster
        cluster_id = ensure_default_cluster(session).id

    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(_versions_payload("18.2.3")),
    )
    ci.scan_and_store()

    latest = ci.latest_snapshot(cluster_id)
    assert latest is not None
    assert latest.current_version == "18.2.3"
