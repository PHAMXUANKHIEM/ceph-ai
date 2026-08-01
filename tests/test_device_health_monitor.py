from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.device_health_monitor as dhm
from shared import db as db_module
from shared.db import Base
from shared.models import Action, ActionClassification, ActionStatus, AuditEntry, Incident, IncidentStatus
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


def _life_expectancy(days_from_now: float) -> str:
    when = datetime.utcnow() + timedelta(days=days_from_now)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+00:00"


def _device(devid="SEAGATE_ABC", daemons=None, life_min_days=3.0, life_max_days=10.0):
    return {
        "devid": devid,
        "daemons": daemons if daemons is not None else ["osd.7"],
        "location": [{"host": "khiempx-data-b2", "dev": "sda"}],
        "life_expectancy_min": _life_expectancy(life_min_days),
        "life_expectancy_max": _life_expectancy(life_max_days),
    }


def _fake_run_ceph_json_command(device_ls_result, osd_dump_osds):
    def fake(inner_command):
        if inner_command == "ceph device ls":
            return "10.20.1.150", device_ls_result
        if inner_command == "ceph osd dump":
            return "10.20.1.150", {"osds": osd_dump_osds}
        raise AssertionError(f"unexpected inner_command: {inner_command!r}")

    return fake


# --- check_predicted_failing_osds() -------------------------------------


def test_check_predicted_failing_osds_returns_empty_when_no_devices(monkeypatch):
    monkeypatch.setattr(
        dhm.ceph_client, "run_ceph_json_command", _fake_run_ceph_json_command([], [])
    )

    assert dhm.check_predicted_failing_osds() == {}


def test_check_predicted_failing_osds_flags_device_within_threshold(monkeypatch):
    monkeypatch.setattr(dhm.settings, "device_health_evacuate_threshold_days", 7, raising=False)
    device = _device(life_min_days=3.0)
    monkeypatch.setattr(
        dhm.ceph_client,
        "run_ceph_json_command",
        _fake_run_ceph_json_command([device], [{"osd": 7, "in": 1}]),
    )

    result = dhm.check_predicted_failing_osds()

    assert "DEVICE_HEALTH_EVACUATE:7" in result
    detail = result["DEVICE_HEALTH_EVACUATE:7"]
    assert detail["osd_id"] == 7
    assert detail["devid"] == "SEAGATE_ABC"
    assert detail["mon_host"] == "10.20.1.150"


def test_check_predicted_failing_osds_ignores_device_beyond_threshold(monkeypatch):
    monkeypatch.setattr(dhm.settings, "device_health_evacuate_threshold_days", 7, raising=False)
    device = _device(life_min_days=30.0)  # far beyond the 7-day threshold
    monkeypatch.setattr(
        dhm.ceph_client,
        "run_ceph_json_command",
        _fake_run_ceph_json_command([device], [{"osd": 7, "in": 1}]),
    )

    assert dhm.check_predicted_failing_osds() == {}


def test_check_predicted_failing_osds_ignores_device_with_no_prediction_set(monkeypatch):
    device = _device()
    device["life_expectancy_min"] = None
    device["life_expectancy_max"] = "0.000000"
    monkeypatch.setattr(
        dhm.ceph_client,
        "run_ceph_json_command",
        _fake_run_ceph_json_command([device], [{"osd": 7, "in": 1}]),
    )

    assert dhm.check_predicted_failing_osds() == {}


def test_check_predicted_failing_osds_skips_osd_already_marked_out(monkeypatch):
    # Already evacuated (by a prior approved proposal, or manually) — must
    # not keep re-proposing it.
    device = _device(life_min_days=3.0)
    monkeypatch.setattr(
        dhm.ceph_client,
        "run_ceph_json_command",
        _fake_run_ceph_json_command([device], [{"osd": 7, "in": 0}]),
    )

    assert dhm.check_predicted_failing_osds() == {}


def test_check_predicted_failing_osds_survives_query_failure(monkeypatch):
    def raising(inner_command):
        raise CephQueryError("all MON nodes failed")

    monkeypatch.setattr(dhm.ceph_client, "run_ceph_json_command", raising)

    assert dhm.check_predicted_failing_osds() == {}  # must not raise


def test_check_predicted_failing_osds_handles_multiple_daemons_on_one_device(monkeypatch):
    # A shared WAL/journal device backing more than one OSD — one proposal
    # per osd_id, not one for the whole device.
    device = _device(daemons=["osd.7", "osd.8"], life_min_days=3.0)
    monkeypatch.setattr(
        dhm.ceph_client,
        "run_ceph_json_command",
        _fake_run_ceph_json_command([device], [{"osd": 7, "in": 1}, {"osd": 8, "in": 1}]),
    )

    result = dhm.check_predicted_failing_osds()

    assert set(result.keys()) == {"DEVICE_HEALTH_EVACUATE:7", "DEVICE_HEALTH_EVACUATE:8"}


# --- create_or_resolve_device_health_incidents() -------------------------


def _detail(osd_id=7, devid="SEAGATE_ABC", mon_host="10.20.1.150"):
    return {
        "osd_id": osd_id,
        "devid": devid,
        "life_expectancy_min": _life_expectancy(3.0),
        "life_expectancy_max": _life_expectancy(10.0),
        "mon_host": mon_host,
    }


def test_create_or_resolve_creates_incident_and_evacuate_action(isolated_db):
    current = {"DEVICE_HEALTH_EVACUATE:7": _detail()}

    dhm.create_or_resolve_device_health_incidents(current)

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="DEVICE_HEALTH_EVACUATE:7").one()
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert "osd.7" in incident.log_excerpt

        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert action.action_id == "evacuate_predicted_failing_osd"
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.target_nodes == '["10.20.1.150"]'
        assert action.action_params == '{"osd_id": 7}'

        audit_entry = session.query(AuditEntry).filter_by(incident_id=incident.id).one()
        assert audit_entry.event_type == "risky_action_pending_approval"
        assert audit_entry.actor == "system"


def test_create_or_resolve_does_not_duplicate_an_already_open_incident(isolated_db):
    current = {"DEVICE_HEALTH_EVACUATE:7": _detail()}

    dhm.create_or_resolve_device_health_incidents(current)
    dhm.create_or_resolve_device_health_incidents(current)  # still a candidate next scan

    with db_module.SessionLocal() as session:
        count = session.query(Incident).filter_by(ceph_code="DEVICE_HEALTH_EVACUATE:7").count()
        assert count == 1


def test_create_or_resolve_resolves_when_no_longer_a_candidate(isolated_db):
    dhm.create_or_resolve_device_health_incidents({"DEVICE_HEALTH_EVACUATE:7": _detail()})

    dhm.create_or_resolve_device_health_incidents({})  # prediction cleared, or osd already out

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="DEVICE_HEALTH_EVACUATE:7").one()
        assert incident.status == IncidentStatus.RESOLVED.value


def test_create_or_resolve_only_touches_its_own_ceph_code_family(isolated_db):
    with db_module.SessionLocal() as session:
        unrelated = Incident(
            ceph_code="OSD_DOWN", status=IncidentStatus.FAILED.value, detected_at=datetime.utcnow()
        )
        session.add(unrelated)
        session.commit()
        unrelated_id = unrelated.id

    dhm.create_or_resolve_device_health_incidents({})

    with db_module.SessionLocal() as session:
        assert session.get(Incident, unrelated_id).status == IncidentStatus.FAILED.value
