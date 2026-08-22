from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import capability_matrix as cm
from shared import db as db_module
from shared.db import Base
from shared.models import ActionPolicyOverride, CapabilityStatus, Cluster
from watcher import capability_inventory as ci
from watcher.ceph_client import CephQueryError
from worker import preflight


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", lambda dbapi_conn, _: dbapi_conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    yield engine


def _default_cluster_id() -> str:
    with db_module.SessionLocal() as session:
        from shared.clusters import ensure_default_cluster
        return ensure_default_cluster(session).id


def test_blocked_when_no_capability_inventory_snapshot(isolated_db):
    cluster_id = _default_cluster_id()
    with db_module.SessionLocal() as session:
        result = preflight.run_preflight(session, cluster_id=cluster_id, action_id="resync_ntp")
    assert result.allowed is False
    assert result.capability_status == CapabilityStatus.UNKNOWN.value
    assert "INSUFFICIENT_EVIDENCE" in result.reason


def test_blocked_when_inventory_unavailable(isolated_db, monkeypatch):
    cluster_id = _default_cluster_id()

    def raise_error():
        raise CephQueryError("timeout")

    monkeypatch.setattr(ci.ceph_client, "summarize_cluster_versions", raise_error)
    ci.scan_and_store(cluster_id)

    with db_module.SessionLocal() as session:
        result = preflight.run_preflight(session, cluster_id=cluster_id, action_id="resync_ntp")
    assert result.allowed is False
    assert result.capability_status == CapabilityStatus.UNAVAILABLE.value


def test_blocked_when_no_matrix_entry_for_action(isolated_db, monkeypatch):
    cluster_id = _default_cluster_id()
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(
            {"daemon0": {"ceph version 18.2.2 (abc) reef (stable)": 1}}
        ),
    )
    ci.scan_and_store(cluster_id)

    with db_module.SessionLocal() as session:
        result = preflight.run_preflight(session, cluster_id=cluster_id, action_id="resync_ntp")
    assert result.allowed is False
    assert result.capability_status == CapabilityStatus.UNKNOWN.value
    assert "resync_ntp" in result.reason


def test_admin_safe_override_allows_unknown_matrix_only(isolated_db, monkeypatch):
    cluster_id = _default_cluster_id()
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(
            {"daemon0": {"ceph version 18.2.2 (abc) reef (stable)": 1}}
        ),
    )
    ci.scan_and_store(cluster_id)
    with db_module.SessionLocal() as session:
        session.add(ActionPolicyOverride(
            action_id="restart_osd_daemon", classification="SAFE",
            updated_by="admin", reason="Bounded automatic OSD recovery",
        ))
        session.commit()
        result = preflight.run_preflight(
            session, cluster_id=cluster_id, action_id="restart_osd_daemon"
        )
    assert result.allowed is True
    assert result.capability_status == CapabilityStatus.UNKNOWN.value
    assert "admin SAFE override" in result.reason


def test_blocked_when_matrix_entry_doesnt_cover_version(isolated_db, monkeypatch):
    cluster_id = _default_cluster_id()
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(
            {"daemon0": {"ceph version 12.2.13 (abc) luminous (stable)": 1}}
        ),
    )
    ci.scan_and_store(cluster_id)
    cm.create_entry(
        command_id="resync_ntp", inner_command="chronyc makestep",
        doc_url="https://docs.ceph.com/en/latest/", verified_by="admin",
        min_major=15,
    )

    with db_module.SessionLocal() as session:
        result = preflight.run_preflight(session, cluster_id=cluster_id, action_id="resync_ntp")
    assert result.allowed is False
    assert result.capability_status == CapabilityStatus.UNSUPPORTED_VERSION.value


def test_admin_safe_override_does_not_bypass_unsupported_version(isolated_db, monkeypatch):
    cluster_id = _default_cluster_id()
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(
            {"daemon0": {"ceph version 18.2.2 (abc) reef (stable)": 1}}
        ),
    )
    ci.scan_and_store(cluster_id)
    cm.create_entry(
        command_id="restart_osd_daemon", inner_command="systemctl restart ceph-osd@N",
        doc_url="https://docs.ceph.com/en/latest/", verified_by="admin", min_major=1, max_major=17,
    )
    with db_module.SessionLocal() as session:
        session.add(ActionPolicyOverride(
            action_id="restart_osd_daemon", classification="SAFE",
            updated_by="admin", reason="Bounded automatic OSD recovery",
        ))
        session.commit()
        result = preflight.run_preflight(
            session, cluster_id=cluster_id, action_id="restart_osd_daemon"
        )
    assert result.allowed is False
    assert result.capability_status == CapabilityStatus.UNSUPPORTED_VERSION.value


def test_allowed_when_everything_checks_out(isolated_db, monkeypatch):
    cluster_id = _default_cluster_id()
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(
            {"daemon0": {"ceph version 18.2.2 (abc) reef (stable)": 1}}
        ),
    )
    ci.scan_and_store(cluster_id)
    cm.create_entry(
        command_id="resync_ntp", inner_command="chronyc makestep",
        doc_url="https://docs.ceph.com/en/latest/", verified_by="admin",
        min_major=15,
    )

    with db_module.SessionLocal() as session:
        result = preflight.run_preflight(session, cluster_id=cluster_id, action_id="resync_ntp")
    assert result.allowed is True
    assert result.capability_status == CapabilityStatus.SUPPORTED.value


def test_blocked_when_mixed_version(isolated_db, monkeypatch):
    cluster_id = _default_cluster_id()
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(
            {
                "daemon0": {"ceph version 17.2.7 (abc) quincy (stable)": 1},
                "daemon1": {"ceph version 18.2.2 (abc) reef (stable)": 1},
            }
        ),
    )
    ci.scan_and_store(cluster_id)
    cm.create_entry(
        command_id="resync_ntp", inner_command="chronyc makestep",
        doc_url="https://docs.ceph.com/en/latest/", verified_by="admin",
        min_major=15,
    )

    with db_module.SessionLocal() as session:
        result = preflight.run_preflight(session, cluster_id=cluster_id, action_id="resync_ntp")
    assert result.allowed is False


def test_blocked_when_cluster_inactive(isolated_db, monkeypatch):
    cluster_id = _default_cluster_id()
    monkeypatch.setattr(
        ci.ceph_client, "summarize_cluster_versions",
        lambda: ci.ceph_client.summarize_versions_payload(
            {"daemon0": {"ceph version 18.2.2 (abc) reef (stable)": 1}}
        ),
    )
    ci.scan_and_store(cluster_id)
    cm.create_entry(
        command_id="resync_ntp", inner_command="chronyc makestep",
        doc_url="https://docs.ceph.com/en/latest/", verified_by="admin",
        min_major=15,
    )
    with db_module.SessionLocal() as session:
        cluster = session.get(Cluster, cluster_id)
        cluster.is_active = False
        session.commit()

    with db_module.SessionLocal() as session:
        result = preflight.run_preflight(session, cluster_id=cluster_id, action_id="resync_ntp")
    assert result.allowed is False
    assert "vô hiệu hoá" in result.reason
