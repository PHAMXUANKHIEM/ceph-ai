import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.main as worker_main
from shared import db as db_module
from shared.db import Base
from shared.models import Incident, IncidentStatus

ENVELOPE = {
    "schema_version": "1.0",
    "incident_id": "will-be-set-per-test",
    "ceph_code": "MON_CLOCK_SKEW",
    "detected_at": "2026-07-16T10:00:00",
    "nodes": ["10.20.1.249"],
    "log_excerpt": "mon2 clock skew log",
    "cluster_snapshot": {"status": "HEALTH_WARN"},
}


class FakeMessage:
    def __init__(self, body: bytes, headers: dict | None = None):
        self.body = body
        self.headers = headers
        self.ack_calls = 0
        self.reject_calls = []

    async def ack(self, multiple: bool = False) -> None:
        self.ack_calls += 1

    async def reject(self, requeue: bool = False) -> None:
        self.reject_calls.append(requeue)


class FakeExchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key):
        self.published.append((message, routing_key))


class FakeChannel:
    def __init__(self):
        self.default_exchange = FakeExchange()


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


def _make_message(incident_id: str, retry_count: int | None = None) -> FakeMessage:
    envelope = dict(ENVELOPE, incident_id=incident_id)
    headers = {worker_main.RETRY_HEADER: retry_count} if retry_count is not None else None
    return FakeMessage(body=json.dumps(envelope).encode(), headers=headers)


def _create_incident(session_local, incident_id: str) -> None:
    from datetime import datetime

    with session_local() as session:
        session.add(
            Incident(
                id=incident_id,
                ceph_code="MON_CLOCK_SKEW",
                status=IncidentStatus.NEW.value,
                detected_at=datetime.utcnow(),
            )
        )
        session.commit()


def test_handle_message_success_acks_and_sets_diagnosing(isolated_db):
    _create_incident(db_module.SessionLocal, "incident-1")
    message = _make_message("incident-1")
    channel = FakeChannel()

    async def ok_process(incident_id, envelope):
        return None

    asyncio.run(worker_main._handle_message(message, channel, ok_process, max_retries=3))

    assert message.ack_calls == 1
    assert message.reject_calls == []
    assert channel.default_exchange.published == []
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-1")
        assert incident.status == IncidentStatus.DIAGNOSING.value


def test_handle_message_failure_below_threshold_republishes_with_incremented_retry(isolated_db):
    _create_incident(db_module.SessionLocal, "incident-2")
    message = _make_message("incident-2", retry_count=0)
    channel = FakeChannel()

    async def failing_process(incident_id, envelope):
        raise RuntimeError("transient failure")

    asyncio.run(worker_main._handle_message(message, channel, failing_process, max_retries=3))

    assert message.ack_calls == 1
    assert message.reject_calls == []
    assert len(channel.default_exchange.published) == 1
    republished_message, routing_key = channel.default_exchange.published[0]
    assert routing_key == worker_main.QUEUE_NAME
    assert republished_message.headers[worker_main.RETRY_HEADER] == 1

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-2")
        assert incident.status == IncidentStatus.DIAGNOSING.value  # not FAILED yet


def test_handle_message_failure_at_threshold_marks_failed_alerts_and_dead_letters(isolated_db, monkeypatch):
    _create_incident(db_module.SessionLocal, "incident-3")
    alerts = []
    monkeypatch.setattr(worker_main, "_notify_ai_diagnosis_failed", alerts.append)
    # retry_count=2 -> this would be the 3rd attempt; max_retries=3 -> exhausted.
    message = _make_message("incident-3", retry_count=2)
    channel = FakeChannel()

    async def failing_process(incident_id, envelope):
        raise RuntimeError("transient failure")

    asyncio.run(worker_main._handle_message(message, channel, failing_process, max_retries=3))

    assert message.ack_calls == 0
    assert message.reject_calls == [False]
    assert channel.default_exchange.published == []
    assert alerts == ["incident-3"]

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-3")
        assert incident.status == IncidentStatus.FAILED.value


def test_handle_message_unknown_incident_id_acks_without_crashing(isolated_db):
    message = _make_message("does-not-exist")
    channel = FakeChannel()

    async def unreachable_process(incident_id, envelope):
        raise AssertionError("should never be called for an unknown incident")

    asyncio.run(worker_main._handle_message(message, channel, unreachable_process, max_retries=3))

    assert message.ack_calls == 1
    assert message.reject_calls == []


def test_handle_message_malformed_json_body_dead_letters_without_crashing(isolated_db):
    message = FakeMessage(body=b"not valid json{{{")
    channel = FakeChannel()

    async def unreachable_process(incident_id, envelope):
        raise AssertionError("should never be called for an unparseable body")

    # Must not raise — a poison message must be dead-lettered, not crash the
    # whole consumer loop and get redelivered forever on restart.
    asyncio.run(worker_main._handle_message(message, channel, unreachable_process, max_retries=3))

    assert message.ack_calls == 0
    assert message.reject_calls == [False]
    assert channel.default_exchange.published == []


def test_handle_message_missing_incident_id_key_dead_letters_without_crashing(isolated_db):
    message = FakeMessage(body=json.dumps({"schema_version": "1.0"}).encode())
    channel = FakeChannel()

    async def unreachable_process(incident_id, envelope):
        raise AssertionError("should never be called when incident_id is missing")

    asyncio.run(worker_main._handle_message(message, channel, unreachable_process, max_retries=3))

    assert message.ack_calls == 0
    assert message.reject_calls == [False]


def test_handle_message_ack_failure_does_not_trigger_duplicate_republish(isolated_db):
    _create_incident(db_module.SessionLocal, "incident-4")
    message = _make_message("incident-4")
    channel = FakeChannel()

    async def ack_that_raises(multiple: bool = False) -> None:
        raise RuntimeError("channel closed")

    message.ack = ack_that_raises

    async def ok_process(incident_id, envelope):
        return None

    # ack() raising after a SUCCESSFUL process_incident must not be
    # misclassified as a processing failure — no republish for work already done.
    asyncio.run(worker_main._handle_message(message, channel, ok_process, max_retries=3))

    assert channel.default_exchange.published == []
    assert message.reject_calls == []
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-4")
        assert incident.status == IncidentStatus.DIAGNOSING.value


def test_handle_message_failure_recovery_path_itself_failing_still_dead_letters(isolated_db):
    _create_incident(db_module.SessionLocal, "incident-5")
    # retry_count=2 -> exhausted at max_retries=3 -> takes the FAILED+reject path.
    message = _make_message("incident-5", retry_count=2)
    channel = FakeChannel()

    async def failing_process(incident_id, envelope):
        raise RuntimeError("transient failure")

    original_reject = message.reject
    calls = {"n": 0}

    async def flaky_reject(requeue: bool = False) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("channel dropped mid-reject")
        await original_reject(requeue)

    message.reject = flaky_reject

    # The recovery path (set FAILED + reject) failing on its first attempt
    # must still end in a dead-letter, not an unhandled crash.
    asyncio.run(worker_main._handle_message(message, channel, failing_process, max_retries=3))

    assert calls["n"] == 2
    assert message.reject_calls == [False]


@pytest.mark.parametrize("raw_header", [None, "not-an-int", -5, -1, 3.5, True])
def test_safe_retry_count_rejects_untrusted_header_values(raw_header):
    headers = {} if raw_header is None else {worker_main.RETRY_HEADER: raw_header}
    assert worker_main._safe_retry_count(headers) == 0


def test_safe_retry_count_accepts_valid_non_negative_int():
    assert worker_main._safe_retry_count({worker_main.RETRY_HEADER: 2}) == 2


def test_effective_max_retries_clamps_non_positive_to_one():
    assert worker_main._effective_max_retries(0) == 1
    assert worker_main._effective_max_retries(-3) == 1
    assert worker_main._effective_max_retries(5) == 5


def test_run_with_max_messages_zero_processes_nothing():
    async def unreachable_process(incident_id, envelope):
        raise AssertionError("should never be called when max_messages=0")

    # No RabbitMQ connection should even be attempted — max_messages=0 must
    # short-circuit before any I/O.
    asyncio.run(worker_main.run(process_incident=unreachable_process, max_messages=0))


# --- Multi-cluster observability Phase 1: cluster-scope safety guard -------
# CRITICAL: Worker's SSH creds/command-building are only ever correct for
# the default cluster (see worker/main.py::_handle_message's own comment) —
# an Incident from any OTHER cluster must NEVER reach process_incident.


def _make_message_for_cluster(incident_id: str, cluster_id) -> FakeMessage:
    envelope = dict(ENVELOPE, incident_id=incident_id, cluster_id=cluster_id)
    return FakeMessage(body=json.dumps(envelope).encode())


def test_handle_message_processes_non_default_cluster_envelope_too(isolated_db):
    """2026-08-10 (multi-tenant remediation Phase 1): the guard that used to
    skip process_incident entirely for a non-default cluster's Incident is
    gone — every cluster-aware caller downstream (worker/llm/router_client.py)
    now resolves THAT cluster's own creds from the envelope/Incident.cluster_id
    instead of the default cluster's global settings, so it's safe to process
    normally here, same as the default cluster's own envelope."""
    from shared.clusters import ensure_default_cluster

    _create_incident(db_module.SessionLocal, "incident-other-cluster")
    with db_module.SessionLocal() as session:
        default_cluster = ensure_default_cluster(session)
    other_cluster_id = "not-" + default_cluster.id  # guaranteed not to match

    message = _make_message_for_cluster("incident-other-cluster", other_cluster_id)
    channel = FakeChannel()

    calls = []

    async def ok_process(incident_id, envelope):
        calls.append(incident_id)

    asyncio.run(worker_main._handle_message(message, channel, ok_process, max_retries=3))

    assert calls == ["incident-other-cluster"]
    assert message.ack_calls == 1
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-other-cluster")
        assert incident.status == IncidentStatus.DIAGNOSING.value


def test_handle_message_processes_default_cluster_envelope_normally(isolated_db):
    from shared.clusters import ensure_default_cluster

    _create_incident(db_module.SessionLocal, "incident-default-cluster")
    with db_module.SessionLocal() as session:
        default_cluster = ensure_default_cluster(session)

    message = _make_message_for_cluster("incident-default-cluster", default_cluster.id)
    channel = FakeChannel()

    calls = []

    async def ok_process(incident_id, envelope):
        calls.append(incident_id)

    asyncio.run(worker_main._handle_message(message, channel, ok_process, max_retries=3))

    assert calls == ["incident-default-cluster"]
    assert message.ack_calls == 1
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-default-cluster")
        assert incident.status == IncidentStatus.DIAGNOSING.value


def test_handle_message_processes_envelope_with_no_cluster_id_key_normally(isolated_db):
    # Every envelope before this feature existed (and every hand-built
    # ENVELOPE in this test file) has no "cluster_id" key at all — must be
    # treated exactly like the default cluster, not skipped.
    _create_incident(db_module.SessionLocal, "incident-legacy-envelope")
    message = _make_message("incident-legacy-envelope")  # ENVELOPE has no cluster_id key
    channel = FakeChannel()

    calls = []

    async def ok_process(incident_id, envelope):
        calls.append(incident_id)

    asyncio.run(worker_main._handle_message(message, channel, ok_process, max_retries=3))

    assert calls == ["incident-legacy-envelope"]
    assert message.ack_calls == 1
