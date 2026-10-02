import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.bluestore_slow_ops as bso
import watcher.main as watcher_main
from config.settings import settings
from shared import db as db_module
from shared.db import Base
from shared.models import BluestoreSlowOpSample, Incident

T0 = datetime(2026, 10, 2, 7, 0, 0)
HOSTS = {0: "10.20.1.39", 1: "10.20.1.153", 2: "10.20.1.195", 3: "10.20.1.39", 4: "10.20.1.153", 5: "10.20.1.195"}


def _detail(*osd_ids):
    return {
        "severity": "HEALTH_WARN",
        "summary": {"message": f"{len(osd_ids)} OSD(s) experiencing slow operations in BlueStore"},
        "detail": [{"message": f" osd.{osd} observed slow operation indications in BlueStore"} for osd in osd_ids],
    }


def _resolver(osd_ids, cluster=None):
    return {osd: HOSTS[osd] for osd in osd_ids}


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
    monkeypatch.setattr(watcher_main.settings, "telegram_ai_humanize_enabled", False)
    yield engine


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(settings, "bluestore_slow_op_open_after_seconds", 90000)
    monkeypatch.setattr(settings, "bluestore_slow_op_sample_interval_seconds", 300)
    monkeypatch.setattr(settings, "bluestore_slow_op_spike_factor", 1.5)
    monkeypatch.setattr(settings, "bluestore_slow_op_min_baseline_samples", 12)
    bso.reset_state()
    yield
    bso.reset_state()


def _samples(cluster_id="c1"):
    with db_module.SessionLocal() as session:
        return [
            (row.sampled_at, json.loads(row.osd_ids_json))
            for row in session.query(BluestoreSlowOpSample)
            .filter(BluestoreSlowOpSample.cluster_id == cluster_id)
            .order_by(BluestoreSlowOpSample.sampled_at)
        ]


def _seed(cluster_id, start, count, osd_ids, step_seconds=300):
    with db_module.SessionLocal() as session:
        for index in range(count):
            session.add(BluestoreSlowOpSample(
                cluster_id=cluster_id,
                sampled_at=start + timedelta(seconds=step_seconds * index),
                osd_count=len(osd_ids),
                osd_ids_json=json.dumps(list(osd_ids)),
                osd_hosts_json="{}",
            ))
        session.commit()


def test_a_single_isolated_osd_is_only_sampled(isolated_db):
    decision = bso.observe("c1", _detail(2), now=T0, resolver=_resolver)

    assert decision.open is False
    assert decision.reasons == ()
    assert _samples() == [(T0, [2])]


def test_two_osds_on_one_host_open_immediately_with_runbook_context(isolated_db):
    decision = bso.observe("c1", _detail(0, 3), now=T0, resolver=_resolver)

    assert decision.open is True
    assert decision.reasons == (bso.REASON_SAME_HOST,)
    evidence = decision.evidence()
    assert evidence["osd_id"] == "0"
    assert evidence["host"] == "10.20.1.39"
    assert evidence["bluestore_slow_ops"]["osd_hosts"] == {"0": "10.20.1.39", "3": "10.20.1.39"}


def test_unresolved_hosts_never_count_as_same_host(isolated_db):
    decision = bso.observe("c1", _detail(0, 3), now=T0, resolver=lambda ids, cluster=None: {})

    assert decision.open is False
    assert "host" not in decision.evidence()


def test_spike_against_the_baseline_opens(isolated_db):
    _seed("c1", T0 - timedelta(days=2), 20, [1])

    decision = bso.observe("c1", _detail(0, 1, 2), now=T0, resolver=_resolver)

    assert decision.baseline_p95 == 1.0
    assert decision.reasons == (bso.REASON_SPIKE,)


def test_no_spike_without_enough_baseline(isolated_db):
    _seed("c1", T0 - timedelta(days=2), 5, [1])

    decision = bso.observe("c1", _detail(0, 1, 2), now=T0, resolver=_resolver)

    assert decision.baseline_p95 is None
    assert decision.open is False


def test_a_usual_number_of_osds_is_not_a_spike(isolated_db):
    _seed("c1", T0 - timedelta(days=2), 20, [0, 1])

    decision = bso.observe("c1", _detail(1, 2), now=T0, resolver=_resolver)

    assert decision.open is False


def test_episode_outliving_one_warning_lifetime_opens_after_a_restart(isolated_db):
    # 25 h of continuous samples written by a previous process.
    _seed("c1", T0 - timedelta(hours=25), 300, [4])
    bso.reset_state()

    decision = bso.observe("c1", _detail(4), now=T0, resolver=_resolver)

    assert decision.reasons == (bso.REASON_PERSISTED,)
    assert decision.episode_started_at == T0 - timedelta(hours=25)


def test_a_gap_starts_a_new_episode(isolated_db):
    _seed("c1", T0 - timedelta(hours=30), 290, [4])
    # Last seeded sample is ~5.8 h before T0: far beyond three intervals.

    decision = bso.observe("c1", _detail(4), now=T0, resolver=_resolver)

    assert decision.episode_started_at == T0
    assert decision.open is False


def test_samples_are_rate_limited_unless_the_osd_set_changes(isolated_db):
    bso.observe("c1", _detail(2), now=T0, resolver=_resolver)
    bso.observe("c1", _detail(2), now=T0 + timedelta(seconds=10), resolver=_resolver)
    bso.observe("c1", _detail(2, 5), now=T0 + timedelta(seconds=20), resolver=_resolver)
    bso.observe("c1", _detail(2, 5), now=T0 + timedelta(seconds=330), resolver=_resolver)

    assert [ids for _at, ids in _samples()] == [[2], [2, 5], [2, 5]]


def test_clusters_are_tracked_separately(isolated_db):
    _seed("c1", T0 - timedelta(hours=25), 300, [4])

    decision = bso.observe("c2", _detail(4), now=T0, resolver=_resolver)

    assert decision.open is False
    assert _samples("c2") == [(T0, [4])]


def test_gate_passes_other_codes_and_fails_open(isolated_db, monkeypatch):
    assert bso.gate_incident("OSD_DOWN", {}, cluster_id="c1") == (False, None)

    def broken(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(bso, "observe", broken)
    skip, evidence = bso.gate_incident(bso.BLUESTORE_SLOW_OP_CODE, _detail(1), cluster_id="c1")

    assert skip is False
    assert evidence["bluestore_slow_ops"]["open_reasons"] == [bso.REASON_GATE_ERROR]


def test_merge_evidence_keeps_existing_signal_keys():
    merged = json.loads(bso.merge_evidence('{"source": "capacity", "osd_id": "9"}', {"osd_id": "1", "x": 2}))

    assert merged == {"source": "capacity", "osd_id": "9", "x": 2}
    assert bso.merge_evidence(None, None) is None
    assert json.loads(bso.merge_evidence("not json", {"a": 1})) == {"a": 1}


def _record_async(published):
    async def _publish(envelope, **_kwargs):
        published.append(envelope)
    return _publish


def _build(monkeypatch, osd_ids):
    published = []
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", _record_async(published))
    monkeypatch.setattr(watcher_main.collector, "collect_relevant_logs", lambda *a, **k: ([], "log"))
    monkeypatch.setattr(bso, "resolve_osd_hosts", _resolver)
    watcher_main.build_and_publish_incident(None, {
        "status": "HEALTH_WARN",
        "checks": {
            bso.BLUESTORE_SLOW_OP_CODE: _detail(*osd_ids),
            "MON_DISK_LOW": {"severity": "HEALTH_WARN", "detail": []},
        },
    })
    return published


def test_watcher_skips_an_isolated_slow_op_but_keeps_other_checks(isolated_db, monkeypatch):
    published = _build(monkeypatch, [2])

    with db_module.SessionLocal() as session:
        assert [row.ceph_code for row in session.query(Incident)] == ["MON_DISK_LOW"]
    assert len(published) == 1
    assert len(_samples(None)) == 1


def test_watcher_opens_a_same_host_slow_op_with_gate_evidence(isolated_db, monkeypatch):
    _build(monkeypatch, [0, 3])

    with db_module.SessionLocal() as session:
        incident = session.query(Incident).filter_by(ceph_code=bso.BLUESTORE_SLOW_OP_CODE).one()
        evidence = json.loads(incident.signal_evidence_json)
    assert evidence["host"] == "10.20.1.39"
    assert evidence["bluestore_slow_ops"]["open_reasons"] == [bso.REASON_SAME_HOST]
