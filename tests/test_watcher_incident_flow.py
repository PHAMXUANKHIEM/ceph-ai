import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.main as watcher_main
from shared import db as db_module
from shared.db import Base
from shared.models import Incident, IncidentStatus

HEALTH_WARN_PAYLOAD = {
    "status": "HEALTH_WARN",
    "checks": {
        "MON_CLOCK_SKEW": {
            "severity": "HEALTH_WARN",
            "summary": {"message": "clock skew detected", "count": 1},
            "detail": [{"message": "mon.khiempx-mon2 clock skew 0.1s"}],
        }
    },
}

HEALTH_OK_PAYLOAD = {"status": "HEALTH_OK", "checks": {}}

# Two simultaneous checks — exercises the multi-fault path: one Incident row
# per check, all envelopes batch-published in a single asyncio.run() call.
MULTI_CHECK_PAYLOAD = {
    "status": "HEALTH_ERR",
    "checks": {
        "MON_CLOCK_SKEW": {
            "severity": "HEALTH_WARN",
            "summary": {"message": "clock skew detected", "count": 1},
            "detail": [{"message": "mon.khiempx-mon2 clock skew 0.1s"}],
        },
        "OSD_DOWN": {
            "severity": "HEALTH_ERR",
            "summary": {"message": "1 osds down", "count": 1},
            "detail": [{"message": "osd.3 (root=default,host=khiempx-data-b2) is down"}],
        },
    },
}


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


def test_no_incident_created_on_recovery_to_health_ok(isolated_db, monkeypatch):
    published = []
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async(published))
    monkeypatch.setattr(
        watcher_main.collector, "collect_relevant_logs", lambda code, detail: (["x"], "log")
    )

    watcher_main.build_and_publish_incident("HEALTH_WARN", HEALTH_OK_PAYLOAD)

    with db_module.SessionLocal() as session:
        assert session.query(Incident).count() == 0
    assert published == []


def _seed_incident(ceph_code: str, status: str) -> str:
    from datetime import datetime

    with db_module.SessionLocal() as session:
        incident = Incident(ceph_code=ceph_code, status=status, detected_at=datetime.utcnow())
        session.add(incident)
        session.commit()
        session.refresh(incident)
        return incident.id


def test_full_recovery_to_health_ok_resolves_stuck_open_incidents(isolated_db):
    # Regression: a real Ceph problem going away (e.g. the router-credentials
    # outage that had left Incidents permanently FAILED, or Worker draining
    # a backlog and writing PENDING_APPROVAL/FAILED long after Watcher last
    # saw a transition) must not leave the Dashboard's aggregate cluster
    # status stuck at ERR forever — Watcher never had a path to close an old
    # Incident back out before this. Calls _resolve_recovered_incidents
    # directly — this is now called every poll from run(), not from
    # build_and_publish_incident (see that function's docstring for why).
    failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)
    pending_id = _seed_incident("PG_DEGRADED", IncidentStatus.PENDING_APPROVAL.value)

    watcher_main._resolve_recovered_incidents(set())  # HEALTH_OK -> no checks reported

    with db_module.SessionLocal() as session:
        assert session.get(Incident, failed_id).status == IncidentStatus.RESOLVED.value
        assert session.get(Incident, pending_id).status == IncidentStatus.RESOLVED.value


def test_partial_recovery_only_resolves_the_code_that_disappeared(isolated_db):
    # A different, still-active check must not have its own Incident
    # touched just because an UNRELATED code recovered.
    recovered_id = _seed_incident("MON_CLOCK_SKEW", IncidentStatus.FAILED.value)
    still_active_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    # Only OSD_DOWN is still being reported — MON_CLOCK_SKEW disappeared.
    watcher_main._resolve_recovered_incidents({"OSD_DOWN"})

    with db_module.SessionLocal() as session:
        assert session.get(Incident, recovered_id).status == IncidentStatus.RESOLVED.value
        assert session.get(Incident, still_active_id).status == IncidentStatus.FAILED.value


def test_chat_request_incident_is_never_auto_resolved_by_recovery(isolated_db):
    # 2026-07-23 regression: a chat-confirmed action's synthetic Incident
    # (ceph_code="CHAT_REQUEST") never matches any real `ceph health
    # detail` check code, so without the guard in
    # _resolve_recovered_incidents, it would ALWAYS look "recovered" on
    # Watcher's very next poll — silently overwriting a real FAILED outcome
    # with RESOLVED, regardless of what current_codes actually is.
    failed_chat_id = _seed_incident("CHAT_REQUEST", IncidentStatus.FAILED.value)
    real_failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, failed_chat_id).status == IncidentStatus.FAILED.value
        assert session.get(Incident, real_failed_id).status == IncidentStatus.RESOLVED.value


def test_cluster_upgrade_incident_is_never_auto_resolved_by_recovery(isolated_db):
    # 2026-07-23 regression (found live): dashboard/routes/upgrade.py's
    # synthetic Incident (ceph_code="CLUSTER_UPGRADE") never matches any
    # real `ceph health detail` check code either — same bug class as
    # CHAT_REQUEST above. Without this guard, a real upgrade Action that had
    # just failed (e.g. a stale Worker process not yet loaded with the
    # upgrade_ceph_cluster command) got silently overwritten from FAILED to
    # RESOLVED on Watcher's very next poll (~watcher_poll_interval_seconds
    # later), making the failure invisible on the Upgrade page.
    failed_upgrade_id = _seed_incident("CLUSTER_UPGRADE", IncidentStatus.FAILED.value)
    pending_upgrade_id = _seed_incident("CLUSTER_UPGRADE", IncidentStatus.PENDING_APPROVAL.value)
    real_failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, failed_upgrade_id).status == IncidentStatus.FAILED.value
        assert (
            session.get(Incident, pending_upgrade_id).status == IncidentStatus.PENDING_APPROVAL.value
        )
        assert session.get(Incident, real_failed_id).status == IncidentStatus.RESOLVED.value


def test_volume_saturated_incident_is_never_auto_resolved_by_recovery(isolated_db):
    # 2026-07-28: same bug class as CHAT_REQUEST/CLUSTER_UPGRADE above —
    # watcher/volume_monitor.py's synthetic Incidents (ceph_code prefixed
    # "VOLUME_SATURATED:") never match any real `ceph health detail` check
    # code either. That module owns this ceph_code family's own create/
    # resolve lifecycle (its own rolling-window saturated-set); without this
    # guard, _resolve_recovered_incidents would incorrectly "recover" every
    # open volume Incident on every single poll regardless of whether the
    # volume is actually still saturated.
    failed_volume_id = _seed_incident("VOLUME_SATURATED:vms/disk-1", IncidentStatus.FAILED.value)
    pending_volume_id = _seed_incident(
        "VOLUME_SATURATED:vms/disk-2", IncidentStatus.PENDING_APPROVAL.value
    )
    real_failed_id = _seed_incident("OSD_DOWN", IncidentStatus.FAILED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, failed_volume_id).status == IncidentStatus.FAILED.value
        assert (
            session.get(Incident, pending_volume_id).status == IncidentStatus.PENDING_APPROVAL.value
        )
        assert session.get(Incident, real_failed_id).status == IncidentStatus.RESOLVED.value


def test_already_terminal_incidents_are_never_touched_by_recovery(isolated_db):
    resolved_id = _seed_incident("OSD_DOWN", IncidentStatus.RESOLVED.value)
    auto_fixed_id = _seed_incident("MON_CLOCK_SKEW", IncidentStatus.AUTO_FIXED.value)
    rejected_id = _seed_incident("PG_DEGRADED", IncidentStatus.REJECTED.value)

    watcher_main._resolve_recovered_incidents(set())

    with db_module.SessionLocal() as session:
        assert session.get(Incident, resolved_id).status == IncidentStatus.RESOLVED.value
        assert session.get(Incident, auto_fixed_id).status == IncidentStatus.AUTO_FIXED.value
        assert session.get(Incident, rejected_id).status == IncidentStatus.REJECTED.value




def test_incident_created_and_published_on_transition_to_warn(isolated_db, monkeypatch):
    published = []
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async(published))
    monkeypatch.setattr(
        watcher_main.collector,
        "collect_relevant_logs",
        lambda code, detail: (["10.20.1.249"], "mon2 log excerpt"),
    )

    watcher_main.build_and_publish_incident(None, HEALTH_WARN_PAYLOAD)

    with db_module.SessionLocal() as session:
        rows = session.query(Incident).all()
        assert len(rows) == 1
        assert rows[0].ceph_code == "MON_CLOCK_SKEW"
        assert rows[0].status == IncidentStatus.NEW.value
        assert rows[0].log_excerpt == "mon2 log excerpt"
        assert rows[0].severity == "HEALTH_WARN"
        db_incident_id = rows[0].id

    assert len(published) == 1
    envelope = published[0]
    assert envelope["incident_id"] == db_incident_id  # AC #3: DB row <-> message linkage
    assert envelope["ceph_code"] == "MON_CLOCK_SKEW"
    assert envelope["nodes"] == ["10.20.1.249"]
    assert envelope["log_excerpt"] == "mon2 log excerpt"


def test_multiple_simultaneous_checks_create_one_incident_each_and_publish_all(
    isolated_db, monkeypatch
):
    published = []
    collected_codes = []

    def fake_collect(code, detail):
        collected_codes.append(code)
        return ([f"node-for-{code}"], f"log for {code}")

    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async(published))
    monkeypatch.setattr(watcher_main.collector, "collect_relevant_logs", fake_collect)

    watcher_main.build_and_publish_incident("HEALTH_WARN", MULTI_CHECK_PAYLOAD)

    with db_module.SessionLocal() as session:
        rows = session.query(Incident).all()
        assert len(rows) == 2
        db_codes = {row.ceph_code for row in rows}
        assert db_codes == {"MON_CLOCK_SKEW", "OSD_DOWN"}
        assert all(row.status == IncidentStatus.NEW.value for row in rows)
        severity_by_code = {row.ceph_code: row.severity for row in rows}
        assert severity_by_code == {"MON_CLOCK_SKEW": "HEALTH_WARN", "OSD_DOWN": "HEALTH_ERR"}

    # One collect_relevant_logs call per check, and both checks collected.
    assert set(collected_codes) == {"MON_CLOCK_SKEW", "OSD_DOWN"}

    # Batch-published exactly once per incident, in a single asyncio.run() —
    # not zero, not merged into one message.
    assert len(published) == 2
    published_codes = {envelope["ceph_code"] for envelope in published}
    assert published_codes == {"MON_CLOCK_SKEW", "OSD_DOWN"}
    for envelope in published:
        assert envelope["nodes"] == [f"node-for-{envelope['ceph_code']}"]
        assert envelope["log_excerpt"] == f"log for {envelope['ceph_code']}"


def _record_async(sink: list):
    async def _fake_publish(envelope: dict) -> None:
        sink.append(envelope)

    return _fake_publish
