from datetime import datetime

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


@pytest.fixture(autouse=True)
def _fast_device_health_monitor_default(monkeypatch):
    """2026-08-01 (Story C): run() now also calls device_health_monitor.
    check_predicted_failing_osds() every poll cycle (gated to once per
    settings.device_health_scan_interval_seconds — see that call site's own
    comment in watcher/main.py). Left unmocked, this hits the real
    ceph_client.run_ceph_json_command, which — against this suite's fake
    conftest.py mon IPs — takes several real seconds (paramiko's own
    connect timeout x 3 configured nodes) to fail, adding real wall-clock
    time to every test in this file whether or not it cares about
    DeviceHealth. Defaults to a fast no-op here; the tests below that
    actually exercise this path override it explicitly within their own
    body, which correctly takes precedence over this fixture's patch."""
    monkeypatch.setattr(watcher_main.device_health_monitor, "check_predicted_failing_osds", lambda: {})
    monkeypatch.setattr(
        watcher_main.device_health_monitor, "create_or_resolve_device_health_incidents", lambda _c: None
    )


@pytest.fixture(autouse=True)
def _fast_node_health_monitor_default(monkeypatch):
    """2026-08-05: run() now also calls node_health_monitor.
    check_node_resources() every poll cycle (gated to once per
    settings.node_health_scan_interval_seconds — see that call site's own
    comment in watcher/main.py). Same "fast default, explicit override
    where the behavior is actually under test" reasoning as
    _fast_device_health_monitor_default above — left unmocked, this hits
    the real shared.cluster_nodes.configured_nodes()/SSH path, adding real
    wall-clock time to every test in this file whether or not it cares
    about node-health monitoring."""
    monkeypatch.setattr(watcher_main.node_health_monitor, "check_node_resources", lambda *_a: {})
    monkeypatch.setattr(
        watcher_main.node_health_monitor, "create_or_resolve_node_health_incidents", lambda *_a: None
    )


@pytest.fixture(autouse=True)
def _fast_volume_monitor_default(monkeypatch):
    """2026-08-01 pre-existing-slowness cleanup: run() calls volume_monitor.
    check_volumes() every poll, unconditionally. Left unmocked (as most
    tests below were, before this fixture existed), this hits the real
    ceph_client.configured_rbd_pools() -> discover_rbd_pools() ->
    run_ceph_json_command(), which — against this suite's fake conftest.py
    mon IPs — takes several real seconds (paramiko's own connect timeout x
    3 configured nodes) to fail, EVERY iteration a test runs, not just
    once (unlike DeviceHealth's own cadence gate above). That was adding
    real minutes to this file's total runtime for tests that don't care
    about volume monitoring at all. Same "fast default, explicit override
    where the behavior is actually under test" pattern as
    _fast_device_health_monitor_default above — the two tests below that
    actually exercise this path already monkeypatch these same names
    explicitly within their own body, which still correctly takes
    precedence over this fixture's patch."""
    monkeypatch.setattr(watcher_main.volume_monitor, "check_volumes", lambda *a, **kw: {})
    monkeypatch.setattr(watcher_main.volume_monitor, "persist_last_poll_metrics", lambda *a, **kw: None)
    monkeypatch.setattr(
        watcher_main.volume_monitor, "create_or_resolve_volume_incidents", lambda *a, **kw: None
    )


@pytest.fixture(autouse=True)
def _fast_trash_capacity_monitor_default(monkeypatch):
    """Keep Watcher-loop tests off the real per-pool Trash SSH path."""
    monkeypatch.setattr(watcher_main.trash_capacity_monitor, "check_and_alert", lambda: {})


@pytest.fixture(autouse=True)
def _fast_bluestore_omap_monitor_default(monkeypatch):
    """2026-08-06: run() now also calls bluestore_omap_monitor.
    create_or_resolve_bluestore_incidents() every poll cycle (gated to once
    per settings.bluestore_omap_scan_interval_seconds). check_legacy_omap_osds()
    itself is pure (reads the already-fetched `health` dict, no SSH) so it's
    left real; only create_or_resolve_bluestore_incidents is mocked here —
    for a NEW code it would call resolve_osd_hosts(), a real SSH probe per
    configured OSD host, same slowness class as the other monitors' fixtures
    above. Same "fast default, explicit override where under test" pattern."""
    monkeypatch.setattr(
        watcher_main.bluestore_omap_monitor, "create_or_resolve_bluestore_incidents", lambda _c: None
    )


@pytest.fixture(autouse=True)
def _fast_osd_latency_monitor_default(monkeypatch):
    """2026-08-07: run() now also calls osd_latency_monitor.
    check_osd_latency_outliers() every poll cycle (gated to once per
    settings.osd_latency_scan_interval_seconds — see that call site's own
    comment in watcher/main.py). Same "fast default, explicit override
    where the behavior is actually under test" reasoning as
    _fast_device_health_monitor_default above — left unmocked, this hits
    the real ceph_client.run_ceph_json_command/list_osds path, adding real
    wall-clock time to every test in this file whether or not it cares
    about OSD latency monitoring."""
    monkeypatch.setattr(watcher_main.osd_latency_monitor, "check_osd_latency_outliers", lambda *_a: {})
    monkeypatch.setattr(
        watcher_main.osd_latency_monitor, "create_or_resolve_osd_latency_incidents", lambda *_a: None
    )


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

    def fake_check_volumes(*_args, **_kwargs):
        check_calls["n"] += 1
        return {"VOLUME_SATURATED:vms/disk-1": {"pool": "vms", "image": "disk-1"}}

    def fake_persist(*_args, **_kwargs):
        persist_calls["n"] += 1

    monkeypatch.setattr(watcher_main.volume_monitor, "check_volumes", fake_check_volumes)
    monkeypatch.setattr(watcher_main.volume_monitor, "persist_last_poll_metrics", fake_persist)
    def fake_resolve(current, **kwargs):
        resolve_calls.append((current, kwargs))

    monkeypatch.setattr(watcher_main.volume_monitor, "create_or_resolve_volume_incidents", fake_resolve)

    watcher_main.run(on_transition=lambda *_: None, max_iterations=3)

    assert check_calls["n"] == 3
    assert persist_calls["n"] == 3
    expected = {"VOLUME_SATURATED:vms/disk-1": {"pool": "vms", "image": "disk-1"}}
    assert [call[0] for call in resolve_calls] == [expected] * 3
    assert all(call[1]["include_legacy_null"] is True for call in resolve_calls)


def test_run_survives_volume_monitor_raising(monkeypatch):
    # A bug in the volume-saturation check must not permanently stop
    # cluster-health monitoring — same posture as
    # test_run_survives_on_transition_callback_raising below for
    # on_transition.
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    def broken_check_volumes(*_args, **_kwargs):
        raise RuntimeError("bug in volume_monitor")

    monkeypatch.setattr(watcher_main.volume_monitor, "check_volumes", broken_check_volumes)

    transitions = []
    watcher_main.run(on_transition=lambda *a: transitions.append(a), max_iterations=3)

    assert len(transitions) == 1  # HEALTH_OK on_transition still fired once, on the first poll


def test_run_calls_device_health_monitor_once_within_default_scan_interval(monkeypatch):
    # settings.device_health_scan_interval_seconds defaults to 1h — across
    # 3 fast poll iterations (real wall-clock time barely advances since
    # time.sleep is mocked away), the scan must fire on the first iteration
    # only, not every iteration the way the health/volume checks do.
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    check_calls = {"n": 0}
    resolve_calls = []

    def fake_check(*_a):
        check_calls["n"] += 1
        return {"DEVICE_HEALTH_EVACUATE:7": {"osd_id": 7}}

    monkeypatch.setattr(watcher_main.device_health_monitor, "check_predicted_failing_osds", fake_check)
    monkeypatch.setattr(
        watcher_main.device_health_monitor,
        "create_or_resolve_device_health_incidents",
        resolve_calls.append,
    )

    watcher_main.run(on_transition=lambda *_: None, max_iterations=3)

    assert check_calls["n"] == 1
    assert resolve_calls == [{"DEVICE_HEALTH_EVACUATE:7": {"osd_id": 7}}]


def test_run_calls_device_health_monitor_every_iteration_when_interval_is_zero(monkeypatch):
    monkeypatch.setattr(watcher_main.settings, "device_health_scan_interval_seconds", 0, raising=False)
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    check_calls = {"n": 0}

    def fake_check(*_a):
        check_calls["n"] += 1
        return {}

    monkeypatch.setattr(watcher_main.device_health_monitor, "check_predicted_failing_osds", fake_check)
    monkeypatch.setattr(
        watcher_main.device_health_monitor, "create_or_resolve_device_health_incidents", lambda _c: None
    )

    watcher_main.run(on_transition=lambda *_: None, max_iterations=3)

    assert check_calls["n"] == 3


def test_run_survives_device_health_monitor_raising(monkeypatch):
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    def broken_check(*_a):
        raise RuntimeError("bug in device_health_monitor")

    monkeypatch.setattr(watcher_main.device_health_monitor, "check_predicted_failing_osds", broken_check)

    transitions = []
    watcher_main.run(on_transition=lambda *a: transitions.append(a), max_iterations=3)

    assert len(transitions) == 1  # HEALTH_OK on_transition still fired once, on the first poll


def test_run_calls_node_health_monitor_once_within_default_scan_interval(monkeypatch):
    # settings.node_health_scan_interval_seconds defaults to 15 minutes —
    # across 3 fast poll iterations (real wall-clock time barely advances
    # since time.sleep is mocked away), the scan must fire on the first
    # iteration only, not every iteration the way the health/volume checks
    # do.
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    check_calls = {"n": 0}
    resolve_calls = []

    def fake_check(*_a):
        check_calls["n"] += 1
        return {"NODE_RESOURCE_HIGH:10.0.0.5": {"host": "10.0.0.5"}}

    monkeypatch.setattr(watcher_main.node_health_monitor, "check_node_resources", fake_check)
    monkeypatch.setattr(
        watcher_main.node_health_monitor,
        "create_or_resolve_node_health_incidents",
        lambda current, *_a: resolve_calls.append(current),
    )

    watcher_main.run(on_transition=lambda *_: None, max_iterations=3)

    assert check_calls["n"] == 1
    assert resolve_calls == [{"NODE_RESOURCE_HIGH:10.0.0.5": {"host": "10.0.0.5"}}]


def test_run_calls_node_health_monitor_every_iteration_when_interval_is_zero(monkeypatch):
    monkeypatch.setattr(watcher_main.settings, "node_health_scan_interval_seconds", 0, raising=False)
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    check_calls = {"n": 0}

    def fake_check(*_a):
        check_calls["n"] += 1
        return {}

    monkeypatch.setattr(watcher_main.node_health_monitor, "check_node_resources", fake_check)
    monkeypatch.setattr(
        watcher_main.node_health_monitor, "create_or_resolve_node_health_incidents", lambda *_a: None
    )

    watcher_main.run(on_transition=lambda *_: None, max_iterations=3)

    assert check_calls["n"] == 3


def test_run_survives_node_health_monitor_raising(monkeypatch):
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    def broken_check(*_a):
        raise RuntimeError("bug in node_health_monitor")

    monkeypatch.setattr(watcher_main.node_health_monitor, "check_node_resources", broken_check)

    transitions = []
    watcher_main.run(on_transition=lambda *a: transitions.append(a), max_iterations=3)

    assert len(transitions) == 1  # HEALTH_OK on_transition still fired once, on the first poll


def test_run_calls_osd_latency_monitor_once_within_default_scan_interval(monkeypatch):
    # settings.osd_latency_scan_interval_seconds defaults to 60s — across 3
    # fast poll iterations (real wall-clock time barely advances since
    # time.sleep is mocked away), the scan must fire on the first iteration
    # only, same "own independent, own cadence" shape as node_health/
    # device_health above.
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    check_calls = {"n": 0}
    resolve_calls = []

    def fake_check(*_a):
        check_calls["n"] += 1
        return {"OSD_LATENCY_HIGH:3": {"osd_id": 3}}

    monkeypatch.setattr(watcher_main.osd_latency_monitor, "check_osd_latency_outliers", fake_check)
    monkeypatch.setattr(
        watcher_main.osd_latency_monitor,
        "create_or_resolve_osd_latency_incidents",
        lambda current, *_a: resolve_calls.append(current),
    )

    watcher_main.run(on_transition=lambda *_: None, max_iterations=3)

    assert check_calls["n"] == 1
    assert resolve_calls == [{"OSD_LATENCY_HIGH:3": {"osd_id": 3}}]


def test_run_calls_osd_latency_monitor_every_iteration_when_interval_is_zero(monkeypatch):
    monkeypatch.setattr(watcher_main.settings, "osd_latency_scan_interval_seconds", 0, raising=False)
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    check_calls = {"n": 0}

    def fake_check(*_a):
        check_calls["n"] += 1
        return {}

    monkeypatch.setattr(watcher_main.osd_latency_monitor, "check_osd_latency_outliers", fake_check)
    monkeypatch.setattr(
        watcher_main.osd_latency_monitor, "create_or_resolve_osd_latency_incidents", lambda *_a: None
    )

    watcher_main.run(on_transition=lambda *_: None, max_iterations=3)

    assert check_calls["n"] == 3


def test_run_survives_osd_latency_monitor_raising(monkeypatch):
    monkeypatch.setattr(watcher_main, "query_cluster_health", lambda: {"status": "HEALTH_OK"})
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    def broken_check(*_a):
        raise RuntimeError("bug in osd_latency_monitor")

    monkeypatch.setattr(watcher_main.osd_latency_monitor, "check_osd_latency_outliers", broken_check)

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
        row = heartbeat.get_latest(session, None)
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
        row = heartbeat.get_latest(session, None)
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
        watcher_main,
        "_resolve_recovered_incidents",
        lambda codes, cluster_id=None, include_legacy_null=True: calls.append(codes),
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
        row = heartbeat.get_latest(session, None)
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
        row = heartbeat.get_latest(session, None)
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


# --- Multi-cluster observability Phase 1: observed-cluster loop ------------


def _make_observed_cluster(session) -> "Cluster":
    from shared.models import Cluster

    cluster = Cluster(
        name="cluster-b",
        ceph_mon_nodes="10.30.1.10",
        ceph_osd_nodes="10.30.1.20",
        ceph_container_name="ceph-mon",
        ssh_user="root",
        ssh_key_path="/root/.ssh/key",
        ceph_exec_mode="docker",
        is_default=False,
        is_active=True,
    )
    session.add(cluster)
    session.commit()
    session.refresh(cluster)
    return cluster


def test_run_observed_cluster_loop_tags_incident_and_heartbeat_with_cluster_id(monkeypatch):
    with db_module.SessionLocal() as session:
        cluster = _make_observed_cluster(session)
        cluster_id = cluster.id

    monkeypatch.setattr(
        watcher_main,
        "query_cluster_health_with",
        lambda *a, **kw: {"status": "HEALTH_WARN", "checks": {"OSD_DOWN": {"severity": "HEALTH_WARN"}}},
    )
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(watcher_main.publisher, "publish_incident", lambda envelope: _noop_coro())
    telegram_calls = []
    monkeypatch.setattr(
        watcher_main.telegram_alerts,
        "send_incident_alert",
        lambda *args, **kwargs: telegram_calls.append((args, kwargs)),
    )

    # 2026-08-10 (multi-tenant remediation Phase 1): log collection now runs
    # for observed clusters too — assert it uses THIS cluster's own SSH
    # creds/host, never the default cluster's, since that's exactly the
    # credential-mixup risk the whole feature exists to avoid.
    collect_calls = []

    def fake_run_command_on_node_with(host, command, ssh_user, ssh_key_path, timeout=None):
        collect_calls.append((host, ssh_user, ssh_key_path))
        return "osd log tail"

    monkeypatch.setattr(watcher_main.collector, "run_command_on_node_with", fake_run_command_on_node_with)

    with db_module.SessionLocal() as session:
        cluster = session.get(watcher_main.Cluster, cluster_id)
        watcher_main.run_observed_cluster_loop(cluster, max_iterations=1)

    with db_module.SessionLocal() as session:
        incidents = session.query(watcher_main.Incident).filter_by(cluster_id=cluster_id).all()
        assert len(incidents) == 1
        assert incidents[0].ceph_code == "OSD_DOWN"
        assert "osd log tail" in incidents[0].log_excerpt

        row = heartbeat.get_latest(session, cluster_id)
        assert row is not None
        assert row.success is True

    assert collect_calls == [("10.30.1.20", "root", "/root/.ssh/key")]
    assert len(telegram_calls) == 1
    args, kwargs = telegram_calls[0]
    assert args[:2] == ("OSD_DOWN", "HEALTH_WARN")
    assert "osd log tail" in args[2]
    assert kwargs == {
        "cluster_name": "cluster-b",
        "bot_token": None,
        "chat_id": None,
        "enabled": None,
    }


async def _noop_coro():
    return None


def test_run_observed_cluster_loop_never_resolves_a_different_clusters_open_incident(monkeypatch):
    from shared.models import Incident, IncidentStatus

    with db_module.SessionLocal() as session:
        observed = _make_observed_cluster(session)
        observed_id = observed.id
        other_cluster = _make_observed_cluster(session)  # a second, unrelated cluster
        other_cluster_id = other_cluster.id
        # An OPEN incident belonging to a totally different (unrelated)
        # cluster — must survive THIS cluster's HEALTH_OK poll untouched
        # (it's not in current_codes, but it's also not THIS cluster's
        # incident).
        session.add(
            Incident(
                ceph_code="MON_CLOCK_SKEW",
                status=IncidentStatus.NEW.value,
                detected_at=datetime.utcnow(),
                cluster_id=other_cluster_id,
            )
        )
        session.commit()

    monkeypatch.setattr(
        watcher_main, "query_cluster_health_with", lambda *a, **kw: {"status": "HEALTH_OK", "checks": {}}
    )
    monkeypatch.setattr(watcher_main.time, "sleep", lambda _seconds: None)

    with db_module.SessionLocal() as session:
        cluster = session.get(watcher_main.Cluster, observed_id)
        watcher_main.run_observed_cluster_loop(cluster, max_iterations=1)

    with db_module.SessionLocal() as session:
        other_incident = session.query(Incident).filter_by(cluster_id=other_cluster_id).one()
        assert other_incident.status == IncidentStatus.NEW.value  # untouched, not wrongly resolved
