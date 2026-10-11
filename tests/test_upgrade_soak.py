from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import upgrade_soak
from shared.models import Base, Cluster, ClusterCapabilityInventory

NOW = datetime(2026, 10, 10, 12, 0)


@pytest.fixture
def real_soak():
    """conftest stubs staging_soak for unrelated upgrade tests; these tests call the real gate."""
    return upgrade_soak._staging_soak


def _session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    prod = Cluster(name="CS-LAB", ceph_mon_nodes="10.0.0.1", ssh_user="root", ssh_key_path="/k",
                   autonomy_environment="production", is_default=True)
    stg = Cluster(name="CS-STAGING", ceph_mon_nodes="10.0.1.1", ssh_user="root", ssh_key_path="/k",
                  autonomy_environment="lab")
    session.add_all([prod, stg])
    session.flush()
    return session, prod, stg


def _scan(session, cluster, version, hours_ago, mixed=False):
    session.add(ClusterCapabilityInventory(cluster_id=cluster.id, status="SUPPORTED", current_version=version,
                                           is_mixed_version=mixed, collected_at=NOW - timedelta(hours=hours_ago)))


def _healthy(monkeypatch, status="HEALTH_OK", checks=()):
    monkeypatch.setattr(upgrade_soak, "_health", lambda cluster_id: (status, list(checks)))


def test_a_version_run_on_staging_for_a_day_and_healthy_passes(real_soak, monkeypatch):
    session, prod, stg = _session()
    for hours in (40, 30, 20, 10, 1):
        _scan(session, stg, "19.2.3", hours)
    _scan(session, stg, "19.2.2", 50)
    _healthy(monkeypatch)

    result = real_soak(session, "19.2.3", cluster_id=prod.id, now=NOW)

    assert result.ok and result.staging_name == "CS-STAGING" and round(result.hours) == 40


@pytest.mark.parametrize(("scans", "health", "reason"), [
    ([("19.2.3", 10)], ("HEALTH_OK", ()), "10.0 giờ"),
    ([("19.2.2", 40)], ("HEALTH_OK", ()), "chưa chạy Ceph 19.2.3"),
    ([("19.2.3", 40), ("19.2.3", 5, True)], ("HEALTH_OK", ()), "chưa chạy Ceph 19.2.3"),  # mixed now
    ([("19.2.3", 40)], ("HEALTH_WARN", ("OSD_DOWN",)), "HEALTH_WARN (OSD_DOWN)"),
])
def test_short_mixed_or_unhealthy_staging_is_not_enough(real_soak, monkeypatch, scans, health, reason):
    session, prod, stg = _session()
    for scan in scans:
        _scan(session, stg, scan[0], scan[1], *(scan[2:] or ()))
    _healthy(monkeypatch, *health)

    result = real_soak(session, "19.2.3", cluster_id=prod.id, now=NOW)

    assert not result.ok and reason in result.reason


def test_without_a_staging_cluster_nothing_passes(real_soak):
    session, prod, stg = _session()
    stg.autonomy_environment = "production"

    assert "Chưa có cụm staging" in real_soak(session, "19.2.3", cluster_id=prod.id, now=NOW).reason
