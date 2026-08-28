import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from dashboard.alert_center import build_alert_groups, paginate_alert_groups
import dashboard.routes.incidents as incidents_route
from dashboard.routes.incidents import _rca_evidence
from shared.alert_lifecycle import inherit_active_mute
from shared.db import Base
from shared.models import Incident


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _incident(code, status, when, *, cluster_id=None, root=None, ident=None):
    return SimpleNamespace(
        id=ident or code + status + when.isoformat(),
        ceph_code=code,
        status=status,
        detected_at=when,
        cluster_id=cluster_id,
        group_root_incident_id=root,
    )


def test_alert_center_merges_repeated_code_and_tracks_open_rows():
    now = datetime(2026, 8, 28, 10, 0)
    groups = build_alert_groups([
        _incident("PERFORMANCE_RCA:x", "RESOLVED", now - timedelta(hours=2), ident="old"),
        _incident("PERFORMANCE_RCA:x", "NEW", now, ident="new"),
        _incident("OSD_DOWN", "RESOLVED", now - timedelta(minutes=1), ident="other"),
    ])

    assert [group["ceph_code"] for group in groups] == ["PERFORMANCE_RCA:x", "OSD_DOWN"]
    assert groups[0]["occurrence_count"] == 2
    assert groups[0]["merged_count"] == 1
    assert groups[0]["active_count"] == 1
    assert groups[0]["representative"].id == "new"


def test_rca_evidence_is_compact_and_deduplicates_hosts():
    evidence = _rca_evidence('''{
        "source": "performance_rca", "hypothesis": "latency_candidate",
        "confidence": 0.848, "current_latency_ms": 11.8,
        "baseline_latency_ms": 7.9, "topology": {"pgid": "8.3a"},
        "host_evidence": [
            {"host": "ceph1", "cpu_percent": 69.5},
            {"host": "ceph1", "cpu_percent": 69.5},
            {"host": "ceph2", "mem_percent": 85.9}
        ], "api_token": "must not be copied"
    }''')

    assert evidence["hypothesis"] == "latency_candidate"
    assert [row["host"] for row in evidence["hosts"]] == ["ceph1", "ceph2"]
    assert "api_token" not in evidence


def test_alert_center_paginates_without_dropping_groups():
    groups = [{"id": index} for index in range(41)]
    first = paginate_alert_groups(groups, page=1, page_size=20)
    last = paginate_alert_groups(groups, page=99, page_size=20)

    assert first["total_groups"] == 41
    assert first["total_pages"] == 3
    assert [row["id"] for row in first["items"]] == list(range(20))
    assert last["page"] == 3
    assert [row["id"] for row in last["items"]] == list(range(40, 41))


def test_alert_center_lifecycle_state_is_derived_from_latest_representative():
    now = datetime(2026, 8, 28, 10, 0)
    acknowledged = _incident("OSD_DOWN", "NEW", now, ident="new")
    acknowledged.acknowledged_at = now
    acknowledged.acknowledged_by = "admin"
    acknowledged.muted_until = now + timedelta(hours=1)
    groups = build_alert_groups([acknowledged])

    assert groups[0]["is_acknowledged"] is True
    assert groups[0]["acknowledged_by"] == "admin"
    assert groups[0]["is_muted"] is True


def test_rca_evidence_rejects_non_numeric_confidence():
    evidence = _rca_evidence('{"source":"performance_rca", "confidence":"not-a-number"}')

    assert evidence["confidence"] is None


def test_new_incident_inherits_active_same_code_mute(session_factory):
    now = datetime(2026, 8, 28, 10, 0)
    with session_factory() as session:
        source = Incident(
            id="muted-source", ceph_code="OSD_DOWN", status="NEW",
            detected_at=now - timedelta(minutes=1), muted_until=now + timedelta(hours=1),
            muted_by="admin",
        )
        fresh = Incident(
            id="muted-fresh", ceph_code="OSD_DOWN", status="NEW", detected_at=now,
        )
        session.add_all([source, fresh])
        session.flush()

        assert inherit_active_mute(session, fresh, now=now) is True
        assert fresh.muted_until == source.muted_until
        assert fresh.muted_by == "admin"


def test_acknowledge_alert_updates_whole_group_and_audits(session_factory, monkeypatch):
    now = datetime(2026, 8, 28, 10, 0)
    with session_factory() as session:
        session.add_all([
            Incident(id="ack-old", ceph_code="OSD_DOWN", status="NEW", detected_at=now - timedelta(minutes=1)),
            Incident(id="ack-new", ceph_code="OSD_DOWN", status="NEW", detected_at=now),
        ])
        session.commit()
    monkeypatch.setattr(incidents_route.db, "SessionLocal", session_factory)
    cluster = SimpleNamespace(id="default-cluster", is_default=True)
    monkeypatch.setattr(incidents_route, "_resolve_selected_cluster", lambda *_args: ([], cluster))

    response = asyncio.run(incidents_route.acknowledge_alert(SimpleNamespace(session={}), "ack-new", "admin"))

    assert response.status_code == 303
    with session_factory() as session:
        rows = session.query(Incident).order_by(Incident.id).all()
        assert all(row.acknowledged_by == "admin" for row in rows)
        assert session.query(incidents_route.AuditEntry).count() == 1


def test_mute_alert_rejects_unsupported_duration(session_factory, monkeypatch):
    now = datetime(2026, 8, 28, 10, 0)
    with session_factory() as session:
        session.add(Incident(id="mute-inc", ceph_code="OSD_DOWN", status="NEW", detected_at=now))
        session.commit()
    monkeypatch.setattr(incidents_route.db, "SessionLocal", session_factory)
    cluster = SimpleNamespace(id="default-cluster", is_default=True)
    monkeypatch.setattr(incidents_route, "_resolve_selected_cluster", lambda *_args: ([], cluster))

    with pytest.raises(incidents_route.HTTPException) as error:
        asyncio.run(incidents_route.mute_alert(SimpleNamespace(session={}), "mute-inc", 2, "admin"))

    assert error.value.status_code == 400
