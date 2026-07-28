import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import watcher.main as watcher_main
from shared import db as db_module
from shared import heartbeat
from shared.db import Base
from shared.models import WatcherHeartbeat


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch):
    # Story 5.2: run() now writes a heartbeat row on every poll — every test
    # in this file needs an isolated DB, or it would hit the real
    # ceph_aiops.db. Mirrors tests/test_claude_client.py's isolated_db
    # fixture (PRAGMA foreign_keys=ON, matching production's shared/db.py::
    # make_engine() — WatcherHeartbeat has no FKs, but kept for consistency).
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


def test_run_calls_on_transition_only_when_status_changes(monkeypatch):
    statuses = [
        {"status": "HEALTH_OK"},
        {"status": "HEALTH_OK"},
        {"status": "HEALTH_WARN"},
        {"status": "HEALTH_WARN"},
        {"status": "HEALTH_ERR"},
        {"status": "HEALTH_OK"},
    ]
    call_index = {"i": 0}

    def fake_query():
        result = statuses[call_index["i"]]
        call_index["i"] += 1
        return result

    monkeypatch.setattr(watcher_main, "query_cluster_health", fake_query)
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    transitions = []

    def record(previous_status, current):
        transitions.append((previous_status, current["status"]))

    watcher_main.run(on_transition=record, max_iterations=len(statuses))

    assert transitions == [
        (None, "HEALTH_OK"),
        ("HEALTH_OK", "HEALTH_WARN"),
        ("HEALTH_WARN", "HEALTH_ERR"),
        ("HEALTH_ERR", "HEALTH_OK"),
    ]


def test_run_fires_on_transition_when_checks_change_but_status_stays_same(monkeypatch):
    # Regression test: status stays HEALTH_WARN the whole time, but the
    # underlying check changes (MON_CLOCK_SKEW resolves, OSD_DOWN appears).
    # A status-only comparison would miss this entirely.
    payloads = [
        {"status": "HEALTH_WARN", "checks": {"MON_CLOCK_SKEW": {}}},
        {"status": "HEALTH_WARN", "checks": {"MON_CLOCK_SKEW": {}}},  # unchanged, no fire
        {"status": "HEALTH_WARN", "checks": {"OSD_DOWN": {}}},  # checks changed, status didn't
    ]
    call_index = {"i": 0}

    def fake_query():
        result = payloads[call_index["i"]]
        call_index["i"] += 1
        return result

    monkeypatch.setattr(watcher_main, "query_cluster_health", fake_query)
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    transitions = []
    watcher_main.run(
        on_transition=lambda prev, cur: transitions.append(set(cur["checks"].keys())),
        max_iterations=len(payloads),
    )

    assert transitions == [{"MON_CLOCK_SKEW"}, {"OSD_DOWN"}]


def test_run_survives_query_failures_without_crashing(monkeypatch):
    from watcher.ceph_client import CephQueryError

    call_count = {"n": 0}

    def fake_query():
        call_count["n"] += 1
        raise CephQueryError("all MON nodes unreachable")

    monkeypatch.setattr(watcher_main, "query_cluster_health", fake_query)
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    watcher_main.run(on_transition=lambda *_: None, max_iterations=3)

    assert call_count["n"] == 3


def test_run_logs_unconfigured_cluster_quietly_not_as_an_error(monkeypatch, caplog):
    # Regression, 2026-07-28 (found on a real first-time install): a fresh
    # install with no CEPH_MON_NODES configured yet logged a full
    # ERROR + traceback every single poll for what is actually expected,
    # harmless "nothing configured yet" state — confusing enough that an
    # operator following the README Ctrl-C'd out of Watcher, mistaking it
    # for something broken. Must log at INFO with no traceback for THIS
    # specific reason, while a real connectivity failure (a different
    # CephQueryError message) must still log loudly.
    from watcher.ceph_client import CephQueryError

    monkeypatch.setattr(
        watcher_main,
        "query_cluster_health",
        lambda: (_ for _ in ()).throw(
            CephQueryError("no MON nodes configured (settings.ceph_mon_nodes is empty)")
        ),
    )
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    with caplog.at_level("INFO"):
        watcher_main.run(on_transition=lambda *_: None, max_iterations=1)

    assert not any(r.levelname == "ERROR" for r in caplog.records)
    assert any(
        r.levelname == "INFO" and "CEPH_MON_NODES" in r.getMessage() for r in caplog.records
    )


def test_run_still_logs_a_real_connectivity_failure_loudly(monkeypatch, caplog):
    from watcher.ceph_client import CephQueryError

    monkeypatch.setattr(
        watcher_main,
        "query_cluster_health",
        lambda: (_ for _ in ()).throw(CephQueryError("all MON nodes unreachable")),
    )
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    with caplog.at_level("INFO"):
        watcher_main.run(on_transition=lambda *_: None, max_iterations=1)

    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_run_calls_volume_monitor_every_poll_iteration(monkeypatch):
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    check_calls = {"n": 0}
    resolve_calls = []
    persist_calls = {"n": 0}

    def fake_check_volumes():
        check_calls["n"] += 1
        return {"VOLUME_SATURATED:vms/disk-1": {"pool": "vms", "image": "disk-1"}}

    def fake_persist():
        persist_calls["n"] += 1

    monkeypatch.setattr(watcher_main.volume_monitor, "check_volumes", fake_check_volumes)
    monkeypatch.setattr(watcher_main.volume_monitor, "persist_last_poll_metrics", fake_persist)
    monkeypatch.setattr(
        watcher_main.volume_monitor, "create_or_resolve_volume_incidents", resolve_calls.append
    )

    watcher_main.run(on_transition=lambda *_: None, max_iterations=3)

    assert check_calls["n"] == 3
    assert persist_calls["n"] == 3
    assert resolve_calls == [{"VOLUME_SATURATED:vms/disk-1": {"pool": "vms", "image": "disk-1"}}] * 3


def test_run_survives_volume_monitor_raising(monkeypatch):
    # A bug in the volume-saturation check must not permanently stop
    # cluster-health monitoring — same posture as
    # test_run_survives_on_transition_callback_raising below for
    # on_transition.
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    def broken_check_volumes():
        raise RuntimeError("bug in volume_monitor")

    monkeypatch.setattr(watcher_main.volume_monitor, "check_volumes", broken_check_volumes)

    transitions = []
    watcher_main.run(on_transition=lambda *a: transitions.append(a), max_iterations=3)

    assert len(transitions) == 1  # HEALTH_OK on_transition still fired once, on the first poll


def test_poll_interval_is_within_ac1_bound():
    # NOTE: this only checks the static config *default* — it does NOT verify
    # actual worst-case end-to-end detection latency under a degraded
    # cluster. Query-side worst-case latency is bounded separately by the
    # per-node timeouts in watcher/ceph_client.py.
    from config.settings import settings

    assert settings.watcher_poll_interval_seconds <= 30


def test_run_survives_on_transition_callback_raising(monkeypatch):
    monkeypatch.setattr(
        watcher_main,
        "query_cluster_health",
        lambda: {"status": "HEALTH_WARN"},
    )
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    def broken_callback(*_args):
        raise RuntimeError("bug in downstream callback")

    # Must not raise/crash the loop — a callback bug shouldn't permanently
    # stop monitoring.
    watcher_main.run(on_transition=broken_callback, max_iterations=3)


def test_run_clamps_negative_poll_interval(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "watcher_poll_interval_seconds", -5)
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})

    sleep_calls = []
    monkeypatch.setattr(watcher_main.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    watcher_main.run(on_transition=lambda *_: None, max_iterations=2)

    assert sleep_calls == [0, 0]


# --- Story 5.2: heartbeat wiring --------------------------------------------


def test_run_records_heartbeat_on_successful_poll(monkeypatch):
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.ceph_client, "last_successful_mon_node", "10.20.1.150")
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    watcher_main.run(on_transition=lambda *_: None, max_iterations=1)

    with db_module.SessionLocal() as session:
        row = heartbeat.get_latest(session)
        assert row is not None
        assert row.success is True
        assert row.mon_node == "10.20.1.150"
        assert row.error_message is None


def test_run_records_heartbeat_on_failed_poll(monkeypatch):
    from watcher.ceph_client import CephQueryError

    def fake_query():
        raise CephQueryError("all MON nodes unreachable")

    monkeypatch.setattr(watcher_main, "query_cluster_health", fake_query)
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    watcher_main.run(on_transition=lambda *_: None, max_iterations=1)

    with db_module.SessionLocal() as session:
        row = heartbeat.get_latest(session)
        assert row is not None
        assert row.success is False
        assert row.mon_node is None
        assert "all MON nodes unreachable" in row.error_message


def test_run_records_heartbeat_even_when_on_transition_does_not_fire(monkeypatch):
    # Core distinction from Incident/on_transition logic: heartbeat must be
    # written on EVERY poll, even when status/checks are unchanged from the
    # previous iteration (on_transition only fires on a real change). Since
    # WatcherHeartbeat is a singleton (upsert-in-place), asserting only the
    # FINAL row state can't distinguish "written once" from "written 3
    # times" — a call-count spy on heartbeat.record is what actually proves
    # this (Review Story 5.2).
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.ceph_client, "last_successful_mon_node", "10.20.1.150")
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    record_calls = []
    original_record = heartbeat.record

    def spy_record(session, **kwargs):
        record_calls.append(kwargs)
        return original_record(session, **kwargs)

    monkeypatch.setattr(watcher_main.heartbeat, "record", spy_record)

    transition_calls = []
    watcher_main.run(on_transition=lambda *a: transition_calls.append(a), max_iterations=3)

    # on_transition only fired once (status never changed after the first poll)...
    assert len(transition_calls) == 1
    # ...but heartbeat.record() was called on EVERY iteration, not just once.
    assert len(record_calls) == 3
    assert all(call["success"] is True for call in record_calls)


def test_run_resolves_recovered_incidents_on_every_poll_not_just_on_transition(monkeypatch):
    # Same distinction as the heartbeat test above, for the OTHER thing that
    # must not be gated behind "did the fingerprint change": Worker can
    # flip an Incident to PENDING_APPROVAL/FAILED well after Watcher last
    # observed a real transition (e.g. draining a backlog) — a health
    # snapshot that stays IDENTICAL across polls must still re-run
    # _resolve_recovered_incidents every time, or such an Incident could be
    # missed forever.
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK", "checks": {}})
    monkeypatch.setattr(watcher_main.ceph_client, "last_successful_mon_node", "10.20.1.150")
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    calls = []
    monkeypatch.setattr(
        watcher_main, "_resolve_recovered_incidents", lambda codes: calls.append(codes)
    )

    transition_calls = []
    watcher_main.run(on_transition=lambda *a: transition_calls.append(a), max_iterations=3)

    assert len(transition_calls) == 1  # status never changed after the first poll
    assert calls == [set(), set(), set()]  # resolve step still ran all 3 times


def test_run_records_failed_heartbeat_when_query_raises_unexpected_exception(monkeypatch):
    # Not a CephQueryError — some other bug/failure inside query_cluster_health()
    # itself. AC #1: every poll iteration gets a heartbeat, success or not.
    def broken_query():
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(watcher_main, "query_cluster_health", broken_query)
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    watcher_main.run(on_transition=lambda *_: None, max_iterations=1)

    with db_module.SessionLocal() as session:
        row = heartbeat.get_latest(session)
        assert row is not None
        assert row.success is False
        assert "unexpected bug" in row.error_message


def test_run_does_not_overwrite_successful_heartbeat_when_on_transition_raises(monkeypatch):
    # The poll itself succeeded (heartbeat already recorded True) — a bug in
    # the downstream on_transition callback must not retroactively mark the
    # heartbeat as failed; that would misrepresent a real, successful poll.
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_WARN"})
    monkeypatch.setattr(watcher_main.ceph_client, "last_successful_mon_node", "10.20.1.150")
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    def broken_callback(*_args):
        raise RuntimeError("bug in downstream callback")

    watcher_main.run(on_transition=broken_callback, max_iterations=1)

    with db_module.SessionLocal() as session:
        row = heartbeat.get_latest(session)
        assert row is not None
        assert row.success is True
        assert row.mon_node == "10.20.1.150"


def test_run_survives_heartbeat_recording_failure(monkeypatch):
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    def broken_session_local():
        raise RuntimeError("DB is down")

    monkeypatch.setattr(db_module, "SessionLocal", broken_session_local)

    # Must not raise — a heartbeat-write failure must not kill the poll loop.
    watcher_main.run(on_transition=lambda *_: None, max_iterations=2)
