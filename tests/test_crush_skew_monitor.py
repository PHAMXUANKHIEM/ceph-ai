import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.crush_skew_monitor as csk
from shared import db as db_module
from shared.db import Base
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    AuditEntry,
    CrushOsdDistribution,
    CrushStructureSnapshot,
    Incident,
    IncidentStatus,
)


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


@pytest.fixture(autouse=True)
def clear_module_state():
    # Same "process-lifetime module state must not leak across tests"
    # reasoning as tests/test_osd_latency_monitor.py's own fixture.
    csk._consecutive_use_skew_scans.clear()
    csk._consecutive_pg_skew_scans.clear()
    yield
    csk._consecutive_use_skew_scans.clear()
    csk._consecutive_pg_skew_scans.clear()


# --- _skew_ratio() (FR-8 formula) -------------------------------------------


def test_skew_ratio_returns_none_when_no_actual_data():
    assert csk._skew_ratio(None, 100, 50, 100) is None


def test_skew_ratio_zero_weight_but_positive_actual_is_max_skew():
    # PRD FR-8 AC #2: Weight kỳ vọng = 0 nhưng actual > 0 (e.g. draining).
    assert csk._skew_ratio(10, 10, 0, 100) == 1.0


def test_skew_ratio_zero_weight_and_zero_actual_is_no_skew():
    assert csk._skew_ratio(0, 10, 0, 100) == 0.0


def test_skew_ratio_zero_sibling_weight_sum_is_max_skew_when_actual_positive():
    assert csk._skew_ratio(5, 5, 0, 0) == 1.0


def test_skew_ratio_matches_hand_computed_example():
    # own_weight=25, siblings_weight_sum=50 -> expected=0.5
    # own_actual=900, siblings_actual_sum=1000 -> actual=0.9
    # skew = (0.9 - 0.5) / 0.5 = 0.8
    assert csk._skew_ratio(900, 1000, 25, 50) == pytest.approx(0.8)


def test_skew_ratio_negative_when_under_expected_share():
    # own_actual=100, siblings_actual_sum=1000 -> actual=0.1, expected=0.5
    assert csk._skew_ratio(100, 1000, 25, 50) == pytest.approx(-0.8)


def test_skew_ratio_lone_child_has_no_skew():
    # A sole child of a Bucket: expected_ratio == actual_ratio == 1.0.
    assert csk._skew_ratio(1000, 1000, 50, 50) == pytest.approx(0.0)


# --- check_crush_skew() -------------------------------------------------
#
# Fixture cluster: 1 Root(weight=100) -> hostA(weight=50)/hostB(weight=50).
# hostA has 2 OSDs (osd0/osd1, weight 25 each — siblings of each other).
# hostB has 1 OSD (osd2, weight 50 — no siblings, always skew 0 alone).
#
# bytes_used chosen so hostA (USE) is 90/10 split internally, and hostB
# carries 4x hostA's total (host-level USE skew too). pgs chosen
# proportional to Weight everywhere (PG signal stays unflagged throughout)
# so the 2 signals are demonstrably independent.


def _tree():
    return {
        "roots": [
            {
                "id": -1,
                "name": "default",
                "type": "root",
                "weight": 100,
                "children": [
                    {
                        "id": -3,
                        "name": "hostA",
                        "type": "host",
                        "weight": 50,
                        "children": [
                            {"id": 0, "name": "osd.0", "type": "osd", "weight": 25, "children": []},
                            {"id": 1, "name": "osd.1", "type": "osd", "weight": 25, "children": []},
                        ],
                    },
                    {
                        "id": -4,
                        "name": "hostB",
                        "type": "host",
                        "weight": 50,
                        "children": [
                            {"id": 2, "name": "osd.2", "type": "osd", "weight": 50, "children": []},
                        ],
                    },
                ],
            }
        ]
    }


def _seed(session, distribution: dict[int, dict]):
    session.add(CrushStructureSnapshot(tree_json=json.dumps(_tree()), diff_json=None))
    for osd_id, values in distribution.items():
        session.add(CrushOsdDistribution(osd_id=osd_id, **values))
    session.commit()


_SKEWED_DISTRIBUTION = {
    0: {"host": "hostA", "bytes_used": 900, "bytes_total": 10_000, "pgs": 125},
    1: {"host": "hostA", "bytes_used": 100, "bytes_total": 10_000, "pgs": 125},
    2: {"host": "hostB", "bytes_used": 4000, "bytes_total": 10_000, "pgs": 250},
}


def test_check_returns_empty_when_no_snapshot_exists(isolated_db):
    assert csk.check_crush_skew() == {}


def test_check_flags_nothing_before_required_consecutive_scans(isolated_db):
    with db_module.SessionLocal() as session:
        _seed(session, _SKEWED_DISTRIBUTION)

    for _ in range(csk.CONSECUTIVE_USE_SCANS_REQUIRED - 1):
        result = csk.check_crush_skew()
        assert result == {}


def test_check_flags_osd_and_host_level_use_skew_after_required_scans(isolated_db):
    with db_module.SessionLocal() as session:
        _seed(session, _SKEWED_DISTRIBUTION)

    for _ in range(csk.CONSECUTIVE_USE_SCANS_REQUIRED - 1):
        csk.check_crush_skew()
    result = csk.check_crush_skew()

    # OSD-level: osd0 over-share, osd1 under-share, both |skew| >= threshold.
    assert "CRUSH_SKEW_USE:0" in result
    assert result["CRUSH_SKEW_USE:0"]["skew"] == pytest.approx(0.8)
    assert "CRUSH_SKEW_USE:1" in result
    assert result["CRUSH_SKEW_USE:1"]["skew"] == pytest.approx(-0.8)
    # osd2 has no siblings under hostB -> never flagged.
    assert "CRUSH_SKEW_USE:2" not in result

    # Host-level: hostA under-share, hostB over-share.
    assert "CRUSH_SKEW_USE:hostA" in result
    assert result["CRUSH_SKEW_USE:hostA"]["entity_type"] == "host"
    assert "CRUSH_SKEW_USE:hostB" in result

    # PG signal was deliberately proportional to Weight everywhere -> never flagged.
    assert not any(code.startswith(csk.CRUSH_SKEW_PG_PREFIX) for code in result)


def test_check_resets_streak_when_a_scan_is_balanced(isolated_db):
    balanced = {
        0: {"host": "hostA", "bytes_used": 500, "bytes_total": 10_000, "pgs": 125},
        1: {"host": "hostA", "bytes_used": 500, "bytes_total": 10_000, "pgs": 125},
        2: {"host": "hostB", "bytes_used": 1000, "bytes_total": 10_000, "pgs": 250},
    }

    with db_module.SessionLocal() as session:
        _seed(session, _SKEWED_DISTRIBUTION)
    for _ in range(csk.CONSECUTIVE_USE_SCANS_REQUIRED - 1):
        csk.check_crush_skew()

    # One balanced scan in between must reset the streak, not just pause it.
    with db_module.SessionLocal() as session:
        session.query(CrushOsdDistribution).delete()
        session.commit()
        _seed(session, balanced)
    result = csk.check_crush_skew()
    assert "CRUSH_SKEW_USE:0" not in result

    with db_module.SessionLocal() as session:
        session.query(CrushOsdDistribution).delete()
        session.commit()
        _seed(session, _SKEWED_DISTRIBUTION)
    for _ in range(csk.CONSECUTIVE_USE_SCANS_REQUIRED - 1):
        assert "CRUSH_SKEW_USE:0" not in csk.check_crush_skew()
    assert "CRUSH_SKEW_USE:0" in csk.check_crush_skew()


def test_check_excludes_osd_missing_from_distribution(isolated_db):
    # osd1 present in the Structure tree but ALREADY removed from
    # CrushOsdDistribution (Story 12.1 deletes it once confirmed gone) —
    # must not be treated as a 0-actual entity, and must not appear in
    # osd0's sibling denominator either.
    partial = {
        0: {"host": "hostA", "bytes_used": 900, "bytes_total": 10_000, "pgs": 125},
        2: {"host": "hostB", "bytes_used": 4000, "bytes_total": 10_000, "pgs": 250},
    }
    with db_module.SessionLocal() as session:
        _seed(session, partial)

    for _ in range(csk.CONSECUTIVE_USE_SCANS_REQUIRED - 1):
        csk.check_crush_skew()
    result = csk.check_crush_skew()

    assert "CRUSH_SKEW_USE:1" not in result
    # osd0 is now the ONLY sibling with data under hostA -> its own actual
    # share of the (data-having) sibling group is 100%, matching its
    # expected share among data-having siblings only (itself) -> skew 0,
    # not flagged, even though its own bytes are unchanged.
    assert "CRUSH_SKEW_USE:0" not in result


# --- create_or_resolve_crush_skew_incidents() -------------------------------


def _detail(entity_type="osd", entity_id=3, signal="USE", skew=0.8, consecutive_scans=3):
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "signal": signal,
        "skew": skew,
        "own_actual": 900,
        "siblings_actual_sum": 1000,
        "own_weight": 25,
        "siblings_weight_sum": 50,
        "consecutive_scans": consecutive_scans,
    }


def test_create_or_resolve_creates_incident_and_investigate_manually_action(isolated_db):
    current = {"CRUSH_SKEW_USE:3": _detail()}

    csk.create_or_resolve_crush_skew_incidents(current)

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="CRUSH_SKEW_USE:3").one()
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        assert "osd.3" in incident.log_excerpt

        action = session.query(Action).filter_by(incident_id=incident.id).one()
        assert action.action_id == "investigate_manually"
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value

        audit_entry = session.query(AuditEntry).filter_by(incident_id=incident.id).one()
        assert audit_entry.event_type == "risky_action_pending_approval"
        assert audit_entry.actor == "system"


def test_create_or_resolve_does_not_duplicate_an_already_open_incident(isolated_db):
    current = {"CRUSH_SKEW_USE:3": _detail()}

    csk.create_or_resolve_crush_skew_incidents(current)
    csk.create_or_resolve_crush_skew_incidents(current)

    with db_module.SessionLocal() as session:
        count = session.query(Incident).filter_by(ceph_code="CRUSH_SKEW_USE:3").count()
        assert count == 1


def test_create_or_resolve_resolves_when_signal_drops_out_of_current(isolated_db):
    # Covers BOTH AC #6 (back under threshold) and AC #7 (entity removed
    # from the cluster) — both look identical to this function: the
    # ceph_code is simply no longer a key in `current`.
    csk.create_or_resolve_crush_skew_incidents({"CRUSH_SKEW_USE:3": _detail()})

    csk.create_or_resolve_crush_skew_incidents({})

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="CRUSH_SKEW_USE:3").one()
        assert incident.status == IncidentStatus.RESOLVED.value


def test_create_or_resolve_only_touches_its_own_ceph_code_families(isolated_db):
    with db_module.SessionLocal() as session:
        unrelated = Incident(
            ceph_code="OSD_LATENCY_HIGH:9", status=IncidentStatus.FAILED.value, detected_at=datetime.utcnow()
        )
        session.add(unrelated)
        session.commit()
        unrelated_id = unrelated.id

    csk.create_or_resolve_crush_skew_incidents({})

    with db_module.SessionLocal() as session:
        assert session.get(Incident, unrelated_id).status == IncidentStatus.FAILED.value


def test_create_or_resolve_recreates_immediately_after_manual_rejection(isolated_db):
    # AC #8: admin rejects while the signal is still over threshold at the
    # next scan -> a brand-new Incident is created right away (REJECTED is
    # not in _RECOVERABLE_STATUSES, matching the NODE_RESOURCE_HIGH/
    # OSD_LATENCY_HIGH precedent this story's Dev Notes cite).
    current = {"CRUSH_SKEW_USE:3": _detail()}
    csk.create_or_resolve_crush_skew_incidents(current)

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="CRUSH_SKEW_USE:3").one()
        incident.status = IncidentStatus.REJECTED.value
        session.commit()
        old_id = incident.id

    csk.create_or_resolve_crush_skew_incidents(current)  # still flagged next scan

    with db_module.SessionLocal() as session:
        incidents = session.query(Incident).filter_by(ceph_code="CRUSH_SKEW_USE:3").all()
        assert len(incidents) == 2
        new_ones = [i for i in incidents if i.id != old_id]
        assert len(new_ones) == 1
        assert new_ones[0].status == IncidentStatus.PENDING_APPROVAL.value


def test_create_or_resolve_sends_telegram_alert_only_for_a_newly_created_incident(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        csk, "send_crush_skew_alert", lambda signal, entity_label, message: calls.append((signal, entity_label, message))
    )
    current = {"CRUSH_SKEW_USE:3": _detail()}

    csk.create_or_resolve_crush_skew_incidents(current)
    csk.create_or_resolve_crush_skew_incidents(current)  # already open — must NOT alert again

    assert len(calls) == 1
    assert calls[0][0] == "USE"
    assert calls[0][1] == "osd.3"


def test_create_or_resolve_does_not_alert_on_resolve(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        csk, "send_crush_skew_alert", lambda signal, entity_label, message: calls.append((signal, entity_label, message))
    )

    csk.create_or_resolve_crush_skew_incidents({"CRUSH_SKEW_USE:3": _detail()})
    calls.clear()
    csk.create_or_resolve_crush_skew_incidents({})

    assert calls == []


def test_create_or_resolve_host_entity_uses_host_label(isolated_db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        csk, "send_crush_skew_alert", lambda signal, entity_label, message: calls.append((signal, entity_label, message))
    )
    current = {"CRUSH_SKEW_USE:hostA": _detail(entity_type="host", entity_id="hostA")}

    csk.create_or_resolve_crush_skew_incidents(current)

    assert calls[0][1] == "host hostA"
    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code="CRUSH_SKEW_USE:hostA").one()
        assert "host hostA" in incident.log_excerpt
