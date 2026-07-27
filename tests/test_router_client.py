import asyncio
import json
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import worker.llm.router_client as router_client
from shared import audit
from shared import db as db_module
from shared.db import Base
from shared.models import (
    Action,
    ActionClassification,
    ActionStatus,
    AuditEntry,
    Incident,
    IncidentStatus,
    SystemFlag,
)

ENVELOPE = {
    "schema_version": "1.0",
    "incident_id": "will-be-set-per-test",
    "ceph_code": "MON_CLOCK_SKEW",
    "detected_at": "2026-07-16T10:00:00",
    "nodes": ["10.20.1.249"],
    "log_excerpt": "mon2 clock skew log",
    "cluster_snapshot": {"status": "HEALTH_WARN"},
}


@pytest.fixture()
def isolated_db(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # Mirror shared/db.py::make_engine()'s PRAGMA foreign_keys=ON — without
    # this, FK-constrained inserts (e.g. AuditEntry via Story 3.3's
    # audit.record()) succeed here even when they'd raise IntegrityError in
    # production, hiding real bugs from this test file (Review Story 3.3).
    event.listen(engine, "connect", lambda dbapi_conn, _: dbapi_conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal", sessionmaker(bind=engine, autoflush=False, autocommit=False)
    )
    # Matches the real migration's seed default (disabled) — tests that
    # specifically want the kill-switch ON set it explicitly.
    with db_module.SessionLocal() as session:
        session.add(SystemFlag(key="kill_switch_enabled", value=False))
        session.commit()
    yield engine


def _create_incident(incident_id: str) -> None:
    with db_module.SessionLocal() as session:
        session.add(
            Incident(
                id=incident_id,
                ceph_code="MON_CLOCK_SKEW",
                status=IncidentStatus.DIAGNOSING.value,
                detected_at=datetime.utcnow(),
            )
        )
        session.commit()


def test_diagnose_incident_saves_diagnosis_text_on_valid_response(isolated_db, monkeypatch):
    redact_calls = []

    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "MON clock is skewed beyond threshold; likely NTP drift.",
            "action_id": "resync_ntp",
            "rationale": "clock skew directly maps to NTP resync.",
        }

    def spying_redact(payload):
        redact_calls.append(payload)
        return payload

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client.default_redactor, "redact", spying_redact)
    # Story 3.2: resync_ntp is SAFE and now auto-executes — this test cares
    # about diagnosis_text/Action creation, not execution, so stub it out.
    monkeypatch.setattr(router_client, "execute_command", lambda host, command: "ok")

    _create_incident("incident-1")
    envelope = dict(ENVELOPE, incident_id="incident-1")

    asyncio.run(router_client.diagnose_incident("incident-1", envelope))

    assert redact_calls == [envelope]  # AC #1: redaction called exactly once, before the call
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-1")
        assert incident.diagnosis_text == "MON clock is skewed beyond threshold; likely NTP drift."
        actions = session.query(Action).filter_by(incident_id="incident-1").all()
        assert len(actions) == 1
        assert actions[0].action_id == "resync_ntp"
        assert actions[0].classification == ActionClassification.SAFE.value
        assert actions[0].status == ActionStatus.AUTO_EXECUTED.value  # auto-executed, Story 3.2


def test_diagnose_incident_called_twice_does_not_create_duplicate_action(isolated_db, monkeypatch):
    # Simulates a redelivered message (e.g. Story 2.1 retry, or an ack()
    # failure after a successful commit) causing diagnose_incident to run
    # twice for the same incident — must not end up with 2 (possibly
    # conflicting) Action rows.
    call_count = {"n": 0}

    async def fake_call_router(user_content):
        call_count["n"] += 1
        # Second call returns a DIFFERENT (risky) action_id — proving the
        # guard prevents a conflicting second Action, not just an identical one.
        if call_count["n"] == 1:
            return {"diagnosis_text": "first diagnosis", "action_id": "resync_ntp", "rationale": "r1"}
        return {"diagnosis_text": "second diagnosis", "action_id": "restart_osd_daemon", "rationale": "r2"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client, "execute_command", lambda host, command: "ok")

    _create_incident("incident-1c")
    envelope = dict(ENVELOPE, incident_id="incident-1c")

    asyncio.run(router_client.diagnose_incident("incident-1c", envelope))
    asyncio.run(router_client.diagnose_incident("incident-1c", envelope))

    with db_module.SessionLocal() as session:
        actions = session.query(Action).filter_by(incident_id="incident-1c").all()
        assert len(actions) == 1
        assert actions[0].action_id == "resync_ntp"  # first classification wins, not overwritten


def test_diagnose_incident_creates_risky_action_for_risky_action_id(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "OSD daemon appears down, likely a transient crash.",
            "action_id": "restart_osd_daemon",
            "rationale": "restarting the daemon typically clears a transient crash.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-1b")
    envelope = dict(ENVELOPE, incident_id="incident-1b")

    asyncio.run(router_client.diagnose_incident("incident-1b", envelope))

    with db_module.SessionLocal() as session:
        actions = session.query(Action).filter_by(incident_id="incident-1b").all()
        assert len(actions) == 1
        assert actions[0].classification == ActionClassification.RISKY.value


def test_diagnose_incident_auto_rejects_risky_action_while_upgrade_in_flight(
    isolated_db, monkeypatch
):
    # 2026-07-24: a cluster upgrade restarting every daemon one host at a
    # time routinely trips a transient OSD_DOWN/MGR_DOWN incident — this
    # must not surface as a new "chờ duyệt" proposal the operator has to
    # reject just to let the upgrade they already approved keep running.
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "OSD daemon appears down, likely a transient crash.",
            "action_id": "restart_osd_daemon",
            "rationale": "restarting the daemon typically clears a transient crash.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    upgrade_incident = Incident(
        id="upgrade-incident-1",
        ceph_code="CLUSTER_UPGRADE",
        status=IncidentStatus.APPROVED.value,
        detected_at=datetime.utcnow(),
    )
    with db_module.SessionLocal() as session:
        session.add(upgrade_incident)
        session.flush()
        session.add(
            Action(
                incident_id=upgrade_incident.id,
                action_id="upgrade_ceph_cluster_package_download",
                classification=ActionClassification.RISKY.value,
                status=ActionStatus.APPROVED.value,
            )
        )
        session.commit()

    _create_incident("incident-suppressed")
    envelope = dict(ENVELOPE, incident_id="incident-suppressed")

    asyncio.run(router_client.diagnose_incident("incident-suppressed", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-suppressed").one()
        assert action.status == ActionStatus.REJECTED.value
        incident = session.get(Incident, "incident-suppressed")
        assert incident.status == IncidentStatus.REJECTED.value
        entries = session.query(AuditEntry).filter_by(incident_id="incident-suppressed").all()
        assert (
            entries[-1].event_type
            == audit.EVENT_RISKY_ACTION_AUTO_REJECTED_CLUSTER_OPERATION_IN_PROGRESS
        )


def test_diagnose_incident_routes_risky_action_normally_once_upgrade_resolved(
    isolated_db, monkeypatch
):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "OSD daemon appears down.",
            "action_id": "restart_osd_daemon",
            "rationale": "restart clears a transient crash.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(router_client.commands, "execute_command", lambda host, command: "")

    upgrade_incident = Incident(
        id="upgrade-incident-2",
        ceph_code="CLUSTER_UPGRADE",
        status=IncidentStatus.RESOLVED.value,
        detected_at=datetime.utcnow(),
    )
    with db_module.SessionLocal() as session:
        session.add(upgrade_incident)
        session.flush()
        session.add(
            Action(
                incident_id=upgrade_incident.id,
                action_id="upgrade_ceph_cluster_package_download",
                classification=ActionClassification.RISKY.value,
                status=ActionStatus.EXECUTED.value,
            )
        )
        session.commit()

    _create_incident("incident-normal-again")
    envelope = dict(ENVELOPE, incident_id="incident-normal-again")

    asyncio.run(router_client.diagnose_incident("incident-normal-again", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-normal-again").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value


def test_diagnose_incident_keeps_restart_osd_daemon_risky(isolated_db, monkeypatch):
    # 2026-07-23: the dashboard-controlled auto-approve-restart-osd override
    # (shared/auto_approve.py) was removed along with its dashboard toggle
    # and the "Chờ duyệt" approval card — restart_osd_daemon now always
    # classifies via the plain policy (action_policy.yaml's `risky:` list),
    # unconditionally, same as any other RISKY action_id.
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "OSD daemon appears down.",
            "action_id": "restart_osd_daemon",
            "rationale": "restart clears a transient crash.",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: pytest.fail("must not execute")
    )

    _create_incident("incident-auto-2")
    envelope = dict(ENVELOPE, incident_id="incident-auto-2")

    asyncio.run(router_client.diagnose_incident("incident-auto-2", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-auto-2").one()
        assert action.classification == ActionClassification.RISKY.value
        assert action.status == ActionStatus.PENDING_APPROVAL.value


def test_diagnose_incident_raises_when_no_tool_use_block(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        raise router_client.RouterDiagnosisError("no report_diagnosis tool call")

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-2")
    envelope = dict(ENVELOPE, incident_id="incident-2")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-2", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-2")
        assert incident.diagnosis_text is None
        assert session.query(Action).filter_by(incident_id="incident-2").count() == 0


def test_diagnose_incident_raises_when_action_id_outside_enum(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "some diagnosis",
            "action_id": "delete_entire_cluster",  # not in VALID_ACTION_IDS
            "rationale": "nonsense",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-3")
    envelope = dict(ENVELOPE, incident_id="incident-3")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-3", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-3")
        assert incident.diagnosis_text is None
        assert session.query(Action).filter_by(incident_id="incident-3").count() == 0


def test_diagnose_incident_raises_when_diagnosis_text_missing(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {"diagnosis_text": "", "action_id": "resync_ntp", "rationale": "x"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-4")
    envelope = dict(ENVELOPE, incident_id="incident-4")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-4", envelope))


def test_diagnose_incident_raises_when_diagnosis_text_whitespace_only(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {"diagnosis_text": "   ", "action_id": "resync_ntp", "rationale": "x"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-4b")
    envelope = dict(ENVELOPE, incident_id="incident-4b")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-4b", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-4b")
        assert incident.diagnosis_text is None


def test_diagnose_incident_raises_when_rationale_missing(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {"diagnosis_text": "some diagnosis", "action_id": "resync_ntp", "rationale": ""}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-4c")
    envelope = dict(ENVELOPE, incident_id="incident-4c")

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client.diagnose_incident("incident-4c", envelope))


def test_diagnose_incident_handles_null_nodes_without_crashing(isolated_db, monkeypatch):
    async def fake_call_router(user_content):
        return {"diagnosis_text": "diagnosis", "action_id": "resync_ntp", "rationale": "x"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-4d")
    envelope = dict(ENVELOPE, incident_id="incident-4d", nodes=None)

    # Must not raise TypeError from ', '.join(None) — this used to crash
    # identically on every retry attempt.
    asyncio.run(router_client.diagnose_incident("incident-4d", envelope))


def test_diagnose_incident_unknown_incident_id_logs_and_returns_without_raising(
    isolated_db, monkeypatch
):
    async def fake_call_router(user_content):
        return {"diagnosis_text": "diagnosis", "action_id": "resync_ntp", "rationale": "x"}

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    envelope = dict(ENVELOPE, incident_id="does-not-exist")

    # Must not raise — matches worker/main.py::_set_incident_status's pattern
    # for a row that's disappeared.
    asyncio.run(router_client.diagnose_incident("does-not-exist", envelope))


class _FakeToolCall:
    def __init__(self, name: str, args: dict):
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))


class _FakeStream:
    """Mimics the openai SDK's `client.chat.completions.stream(...)` async
    context manager just enough for _call_router: `async with ... as
    stream:` then `await stream.get_final_completion()`."""

    def __init__(self, completion):
        self._completion = completion

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_final_completion(self):
        return self._completion


def _completion(*calls: tuple[str, dict], finish_reason: str = "stop"):
    """`calls` is (name, args) pairs — mirrors tests/test_chat_client.py's
    helper of the same shape. Builds a minimal fake mirroring the real
    openai ChatCompletion shape (`.choices[0].finish_reason`,
    `.choices[0].message.tool_calls[i].function.{name,arguments}`)."""
    tool_calls = [_FakeToolCall(name, args) for name, args in calls]
    message = SimpleNamespace(tool_calls=tool_calls, content=None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _install_fake_client(monkeypatch, completion):
    class FakeCompletions:
        def stream(self, **kwargs):
            return _FakeStream(completion)

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(router_client, "_get_client", lambda: FakeClient())


def test_get_client_uses_configured_api_key(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "router_api_key", "sk-test-whatever")
    monkeypatch.setattr(settings, "router_base_url", "http://localhost:20128")

    client = router_client._get_client()

    assert client.api_key == "sk-test-whatever"


def test_call_router_extracts_args_from_matching_tool_call(monkeypatch):
    _install_fake_client(
        monkeypatch,
        _completion(
            (router_client.TOOL_NAME, {"diagnosis_text": "d", "action_id": "resync_ntp", "rationale": "r"})
        ),
    )

    result = asyncio.run(router_client._call_router("some content"))

    assert result == {"diagnosis_text": "d", "action_id": "resync_ntp", "rationale": "r"}


def test_call_router_ignores_tool_call_with_wrong_name(monkeypatch):
    _install_fake_client(monkeypatch, _completion(("some_other_tool", {"x": 1})))

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client._call_router("some content"))


def test_call_router_raises_when_there_is_no_tool_call(monkeypatch):
    _install_fake_client(monkeypatch, _completion())

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client._call_router("some content"))


def test_call_router_returns_first_matching_call_when_multiple_tool_calls(monkeypatch):
    _install_fake_client(
        monkeypatch,
        _completion(
            (router_client.TOOL_NAME, {"first": True}),
            (router_client.TOOL_NAME, {"first": False}),
        ),
    )

    result = asyncio.run(router_client._call_router("some content"))

    assert result == {"first": True}


def test_call_router_raises_when_response_truncated_at_max_tokens(monkeypatch):
    _install_fake_client(
        monkeypatch,
        _completion(
            (router_client.TOOL_NAME, {"diagnosis_text": "cut off"}),
            finish_reason="length",
        ),
    )

    with pytest.raises(router_client.RouterDiagnosisError):
        asyncio.run(router_client._call_router("some content"))


def test_warn_if_missing_api_key_logs_warning_when_empty(caplog):
    with caplog.at_level("WARNING"):
        router_client._warn_if_missing_api_key("")
    assert "ROUTER_API_KEY is not configured" in caplog.text


def test_warn_if_missing_api_key_silent_when_set(caplog):
    with caplog.at_level("WARNING"):
        router_client._warn_if_missing_api_key("sk-fake-key")
    assert "ROUTER_API_KEY is not configured" not in caplog.text


def test_valid_action_ids_loaded_from_policy_yaml_non_empty():
    assert len(router_client.VALID_ACTION_IDS) > 0
    assert "resync_ntp" in router_client.VALID_ACTION_IDS
    assert "restart_osd_daemon" in router_client.VALID_ACTION_IDS
    assert "pg_repair_force" in router_client.VALID_ACTION_IDS
    assert "investigate_manually" in router_client.VALID_ACTION_IDS


# --- Story 3.2: Safe Action execution ---------------------------------------


def _safe_response(nodes_note=""):
    return {
        "diagnosis_text": f"MON clock skewed{nodes_note}",
        "action_id": "resync_ntp",
        "rationale": "clock skew maps to NTP resync",
    }


async def _fake_call_router_safe(user_content):
    return _safe_response()


async def _fake_call_router_risky(user_content):
    return {
        "diagnosis_text": "OSD daemon appears down",
        "action_id": "restart_osd_daemon",
        "rationale": "restart clears transient crash",
    }


def test_diagnose_incident_executes_safe_action_and_marks_auto_fixed(isolated_db, monkeypatch):
    execute_calls = []

    def fake_execute(host, command):
        execute_calls.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(router_client, "execute_command", fake_execute)

    _create_incident("incident-5a")
    envelope = dict(ENVELOPE, incident_id="incident-5a", nodes=["10.20.1.249", "10.20.1.253"])

    asyncio.run(router_client.diagnose_incident("incident-5a", envelope))

    assert [host for host, _cmd in execute_calls] == ["10.20.1.249", "10.20.1.253"]
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5a")
        assert incident.status == IncidentStatus.AUTO_FIXED.value
        action = session.query(Action).filter_by(incident_id="incident-5a").one()
        assert action.status == ActionStatus.AUTO_EXECUTED.value
        assert action.executed_at is not None
        assert "chronyc" in action.proposed_command
        entry = session.query(AuditEntry).filter_by(incident_id="incident-5a").one()
        assert entry.action_id == action.id
        assert entry.event_type == audit.EVENT_SAFE_ACTION_EXECUTED
        assert entry.actor == audit.ACTOR_SYSTEM


def test_diagnose_incident_marks_failed_when_any_node_execution_fails(isolated_db, monkeypatch):
    def fake_execute(host, command):
        if host == "10.20.1.253":
            raise router_client.ExecutorError(f"{host}: command exited 1")
        return "ok"

    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(router_client, "execute_command", fake_execute)

    _create_incident("incident-5b")
    envelope = dict(ENVELOPE, incident_id="incident-5b", nodes=["10.20.1.249", "10.20.1.253"])

    # Must not raise — an execution failure is terminal (AC #3), not retried
    # via worker/main.py's router-failure retry/DLX mechanism.
    asyncio.run(router_client.diagnose_incident("incident-5b", envelope))

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5b")
        assert incident.status == IncidentStatus.FAILED.value
        action = session.query(Action).filter_by(incident_id="incident-5b").one()
        assert action.status == ActionStatus.FAILED.value
        assert action.executed_at is None
        assert action.proposed_command is not None  # recorded even on failure
        entry = session.query(AuditEntry).filter_by(incident_id="incident-5b").one()
        assert entry.event_type == audit.EVENT_SAFE_ACTION_FAILED


def test_diagnose_incident_skips_execution_and_routes_to_approval_when_kill_switch_on(
    isolated_db, monkeypatch
):
    with db_module.SessionLocal() as session:
        flag = session.get(SystemFlag, "kill_switch_enabled")
        flag.value = True
        session.commit()

    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: execute_calls.append(1)
    )

    _create_incident("incident-5c")
    envelope = dict(ENVELOPE, incident_id="incident-5c")

    asyncio.run(router_client.diagnose_incident("incident-5c", envelope))

    assert execute_calls == []  # AD-4: no exceptions — never executes when the switch is on
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5c")
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        action = session.query(Action).filter_by(incident_id="incident-5c").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        entry = session.query(AuditEntry).filter_by(incident_id="incident-5c").one()
        assert entry.event_type == audit.EVENT_SAFE_ACTION_BLOCKED_BY_KILL_SWITCH


# --- Review Story 3.3: audit.record() must not crash when the Action/Incident
# row it would reference doesn't exist (isolated_db now enforces FKs like
# production does, so these would raise IntegrityError without the fix) -----


def test_record_execution_result_missing_action_row_does_not_crash_and_still_updates_incident(
    isolated_db,
):
    _create_incident("incident-missing-action")

    router_client._record_execution_result(
        "incident-missing-action", "nonexistent-action-pk", command="echo ok", succeeded=True
    )

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-missing-action")
        assert incident.status == IncidentStatus.AUTO_FIXED.value
        entry = session.query(AuditEntry).filter_by(incident_id="incident-missing-action").one()
        assert entry.action_id is None
        assert entry.event_type == audit.EVENT_SAFE_ACTION_EXECUTED


def test_record_execution_result_missing_incident_row_does_not_crash_and_skips_audit(
    isolated_db,
):
    # Neither Incident nor Action exists — nothing valid to update or audit.
    router_client._record_execution_result(
        "nonexistent-incident", "nonexistent-action-pk", command="echo ok", succeeded=True
    )

    with db_module.SessionLocal() as session:
        assert session.query(AuditEntry).count() == 0


def test_route_to_manual_approval_missing_action_row_does_not_crash(isolated_db):
    _create_incident("incident-missing-action-2")

    router_client._route_to_manual_approval(
        "incident-missing-action-2", "nonexistent-action-pk", "resync_ntp"
    )

    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-missing-action-2")
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        entry = session.query(AuditEntry).filter_by(incident_id="incident-missing-action-2").one()
        assert entry.action_id is None
        assert entry.event_type == audit.EVENT_SAFE_ACTION_BLOCKED_BY_KILL_SWITCH


def test_diagnose_incident_risky_action_never_executes_regardless_of_kill_switch(
    isolated_db, monkeypatch
):
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "OSD daemon appears down",
            "action_id": "restart_osd_daemon",
            "rationale": "restart clears transient crash",
        }

    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", fake_call_router)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: execute_calls.append(1)
    )

    _create_incident("incident-5d")
    envelope = dict(ENVELOPE, incident_id="incident-5d")

    asyncio.run(router_client.diagnose_incident("incident-5d", envelope))

    assert execute_calls == []
    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-5d").one()
        # Story 4.2: RISKY -> PENDING_APPROVAL, never auto-executed (FR8).
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.rationale == "restart clears transient crash"
        # restart_osd_daemon's command is discovered per-host (systemctl),
        # and _route_risky_to_approval has no specific host yet at
        # classification time — no preview command to show, None rather
        # than a guess.
        assert action.proposed_command is None


def test_diagnose_incident_kill_switch_checked_before_each_node_not_just_once(
    isolated_db, monkeypatch
):
    # Kill-switch flips ON after node 1 has already been checked/executed —
    # node 2 must never run (AD-4: checked before EVERY command).
    from shared.models import SystemFlag

    execute_calls = []

    def fake_execute(host, command):
        execute_calls.append(host)
        if host == "10.20.1.249":
            # Flip the switch ON right after the first node's check passes,
            # simulating an operator hitting the button mid-loop.
            with db_module.SessionLocal() as session:
                flag = session.get(SystemFlag, "kill_switch_enabled")
                flag.value = True
                session.commit()
        return "ok"

    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(router_client, "execute_command", fake_execute)

    _create_incident("incident-5e")
    envelope = dict(ENVELOPE, incident_id="incident-5e", nodes=["10.20.1.249", "10.20.1.253"])

    asyncio.run(router_client.diagnose_incident("incident-5e", envelope))

    assert execute_calls == ["10.20.1.249"]  # second node never touched
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5e")
        # Node 1 already ran for real — PENDING_APPROVAL would misrepresent
        # that nothing happened yet, so this must surface as FAILED.
        assert incident.status == IncidentStatus.FAILED.value
        action = session.query(Action).filter_by(incident_id="incident-5e").one()
        assert action.status == ActionStatus.FAILED.value


def test_diagnose_incident_kill_switch_on_before_any_node_routes_cleanly_to_approval(
    isolated_db, monkeypatch
):
    from shared.models import SystemFlag

    with db_module.SessionLocal() as session:
        flag = session.get(SystemFlag, "kill_switch_enabled")
        flag.value = True
        session.commit()

    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: execute_calls.append(host)
    )

    _create_incident("incident-5f")
    envelope = dict(ENVELOPE, incident_id="incident-5f", nodes=["10.20.1.249", "10.20.1.253"])

    asyncio.run(router_client.diagnose_incident("incident-5f", envelope))

    assert execute_calls == []
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5f")
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        action = session.query(Action).filter_by(incident_id="incident-5f").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value


def test_diagnose_incident_recovers_pending_action_left_by_a_crashed_prior_attempt(
    isolated_db, monkeypatch
):
    # Simulates: a prior diagnose_incident() call created the Action (status
    # PENDING) and committed, then the process died before execution ran.
    # A redelivery must retry execution, not strand it forever.
    _create_incident("incident-5g")
    with db_module.SessionLocal() as session:
        session.add(
            Action(
                incident_id="incident-5g",
                action_id="resync_ntp",
                classification=ActionClassification.SAFE.value,
                status=ActionStatus.PENDING.value,
            )
        )
        session.commit()

    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: execute_calls.append(host) or "ok"
    )

    envelope = dict(ENVELOPE, incident_id="incident-5g")
    asyncio.run(router_client.diagnose_incident("incident-5g", envelope))

    assert execute_calls == ["10.20.1.249"]  # execution actually retried
    with db_module.SessionLocal() as session:
        actions = session.query(Action).filter_by(incident_id="incident-5g").all()
        assert len(actions) == 1  # no duplicate row created
        assert actions[0].status == ActionStatus.AUTO_EXECUTED.value
        incident = session.get(Incident, "incident-5g")
        assert incident.status == IncidentStatus.AUTO_FIXED.value


# --- Story 4.2: RISKY -> PENDING_APPROVAL ----------------------------------


def test_diagnose_incident_risky_action_records_pending_approval_audit_entry(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_risky)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: pytest.fail("must not execute")
    )

    _create_incident("incident-6a")
    envelope = dict(ENVELOPE, incident_id="incident-6a", nodes=["10.20.1.83"])
    asyncio.run(router_client.diagnose_incident("incident-6a", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-6a").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.target_nodes == '["10.20.1.83"]'
        entries = session.query(AuditEntry).filter_by(incident_id="incident-6a").all()
        assert len(entries) == 1
        assert entries[0].event_type == audit.EVENT_RISKY_ACTION_PENDING_APPROVAL
        assert entries[0].action_id == action.id


def test_diagnose_incident_risky_action_with_no_defined_command_leaves_proposed_command_none(
    isolated_db, monkeypatch
):
    # pg_repair_force is RISKY but deliberately has no Command defined
    # (worker/executor/commands.py) — must not crash, just show nothing.
    async def fake_call_router(user_content):
        return {
            "diagnosis_text": "PG stuck inconsistent",
            "action_id": "pg_repair_force",
            "rationale": "matches pg repair criteria",
        }

    monkeypatch.setattr(router_client, "_call_router", fake_call_router)

    _create_incident("incident-6b")
    envelope = dict(ENVELOPE, incident_id="incident-6b")
    asyncio.run(router_client.diagnose_incident("incident-6b", envelope))

    with db_module.SessionLocal() as session:
        action = session.query(Action).filter_by(incident_id="incident-6b").one()
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        assert action.proposed_command is None


# --- Story 4.3: execute an operator-approved RISKY action ------------------


def _approved_action(session, incident_id: str, action_id: str = "restart_osd_daemon", nodes=None) -> Action:
    import json as _json

    action = Action(
        incident_id=incident_id,
        action_id=action_id,
        classification=ActionClassification.RISKY.value,
        status=ActionStatus.APPROVED.value,
        target_nodes=_json.dumps(nodes if nodes is not None else ["10.20.1.83"]),
    )
    session.add(action)
    session.commit()
    return action


def test_execute_approved_action_success_marks_executed_and_resolved(isolated_db, monkeypatch):
    execute_calls = []

    def fake_execute(host, command):
        if command == "systemctl | grep ceph || true":
            return "  ceph-fsid@osd.3.service   loaded active running   x\n"
        execute_calls.append(host)
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-7a")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-7a")
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert execute_calls == ["10.20.1.83"]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        assert action.executed_at is not None
        assert action.proposed_command == "systemctl restart ceph-fsid@osd.3.service"
        incident = session.get(Incident, "incident-7a")
        assert incident.status == IncidentStatus.RESOLVED.value
        entries = session.query(AuditEntry).filter_by(incident_id="incident-7a").all()
        assert entries[-1].event_type == audit.EVENT_RISKY_ACTION_EXECUTED


def test_execute_approved_action_restart_osd_daemon_discovers_via_systemctl_and_restarts(
    isolated_db, monkeypatch
):
    systemctl_outputs = {
        "10.20.1.112": "  ceph-fsid@osd.0.service   loaded active running   x\n",
        "10.20.1.95": "  ceph-fsid@osd.1.service   loaded active running   x\n",
    }
    executed = []

    def fake_execute(host, command):
        if command == "systemctl | grep ceph || true":
            return systemctl_outputs[host]
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    # Discovery (worker/executor/commands.py::_discover_ceph_units) calls
    # execute_command via ITS OWN module-level import binding, a separate
    # reference from router_client's — both must be patched.
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-7z")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session, "incident-7z", nodes=["10.20.1.112", "10.20.1.95"]
        )
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert executed == [
        ("10.20.1.112", "systemctl restart ceph-fsid@osd.0.service"),
        ("10.20.1.95", "systemctl restart ceph-fsid@osd.1.service"),
    ]
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        assert action.proposed_command == "systemctl restart ceph-fsid@osd.1.service"
        incident = session.get(Incident, "incident-7z")
        assert incident.status == IncidentStatus.RESOLVED.value


def test_execute_approved_action_persists_execution_progress_per_host(
    isolated_db, monkeypatch
):
    systemctl_outputs = {
        "10.20.1.112": "  ceph-fsid@osd.0.service   loaded active running   x\n",
        "10.20.1.95": "  ceph-fsid@osd.1.service   loaded active running   x\n",
    }

    def fake_execute(host, command):
        if command == "systemctl | grep ceph || true":
            return systemctl_outputs[host]
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-progress-db")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session, "incident-progress-db", nodes=["10.20.1.112", "10.20.1.95"]
        )
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        progress = json.loads(action.execution_progress)

    # 2026-07-27: progress entries also carry command/started_at/finished_at
    # (used to render a per-step Markdown log on the Upgrade page) — check
    # the fields this test actually cares about rather than exact equality,
    # so it doesn't have to be updated every time a new field is added.
    assert [{"host": p["host"], "status": p["status"]} for p in progress] == [
        {"host": "10.20.1.112", "status": "done"},
        {"host": "10.20.1.95", "status": "done"},
    ]
    for p in progress:
        assert p["command"]
        assert p["started_at"]
        assert p["finished_at"]


def test_execute_approved_action_marks_failed_host_in_progress(isolated_db, monkeypatch):
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command):
        if command == "systemctl | grep ceph || true":
            return "  ceph-fsid@osd.0.service   loaded active running   x\n"
        raise ExecutorError(f"{host}: boom")

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-progress-fail")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-progress-fail", nodes=["10.20.1.83"])
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        progress = json.loads(action.execution_progress)

    assert [{"host": p["host"], "status": p["status"]} for p in progress] == [
        {"host": "10.20.1.83", "status": "failed"}
    ]
    assert progress[0]["error"] == "10.20.1.83: boom"
    assert progress[0]["started_at"]
    assert progress[0]["finished_at"]


def test_execute_approved_action_logs_start_and_completion_per_host(
    isolated_db, monkeypatch, caplog
):
    # 2026-07-24: an operator tailing worker.log during a real (multi-minute)
    # upgrade run had no way to tell which host it was currently on vs.
    # already done — this pins the per-host progress log lines added for
    # exactly that.
    systemctl_outputs = {
        "10.20.1.112": "  ceph-fsid@osd.0.service   loaded active running   x\n",
        "10.20.1.95": "  ceph-fsid@osd.1.service   loaded active running   x\n",
    }

    def fake_execute(host, command):
        if command == "systemctl | grep ceph || true":
            return systemctl_outputs[host]
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-progress")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session, "incident-progress", nodes=["10.20.1.112", "10.20.1.95"]
        )
        action_pk = action.id

    with caplog.at_level(logging.INFO, logger="worker.llm.router_client"):
        router_client._execute_approved_action(action_pk)

    messages = [r.getMessage() for r in caplog.records]
    assert any(
        "bắt đầu" in m and "10.20.1.112" in m and "(1/2)" in m for m in messages
    )
    assert any(
        "hoàn tất" in m and "10.20.1.112" in m and "(1/2)" in m for m in messages
    )
    assert any(
        "bắt đầu" in m and "10.20.1.95" in m and "(2/2)" in m for m in messages
    )
    assert any(
        "hoàn tất" in m and "10.20.1.95" in m and "(2/2)" in m for m in messages
    )


def test_execute_approved_action_ssh_failure_marks_failed(isolated_db, monkeypatch):
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command):
        raise ExecutorError("boom")

    monkeypatch.setattr(router_client, "execute_command", fake_execute)

    _create_incident("incident-7b")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-7b")
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value
        incident = session.get(Incident, "incident-7b")
        assert incident.status == IncidentStatus.FAILED.value
        entries = session.query(AuditEntry).filter_by(incident_id="incident-7b").all()
        assert entries[-1].event_type == audit.EVENT_RISKY_ACTION_FAILED


def test_execute_approved_action_no_command_defined_marks_failed(isolated_db, monkeypatch):
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: pytest.fail("must not execute")
    )

    _create_incident("incident-7c")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-7c", action_id="investigate_manually")
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value
        incident = session.get(Incident, "incident-7c")
        assert incident.status == IncidentStatus.FAILED.value


def test_execute_approved_action_malformed_target_nodes_marks_failed(isolated_db, monkeypatch):
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: pytest.fail("must not execute")
    )

    _create_incident("incident-7d")
    with db_module.SessionLocal() as session:
        action = Action(
            incident_id="incident-7d",
            action_id="restart_osd_daemon",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.APPROVED.value,
            target_nodes=None,
        )
        session.add(action)
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.FAILED.value


def test_execute_approved_action_kill_switch_on_before_start_reverts_to_pending_approval(
    isolated_db, monkeypatch
):
    with db_module.SessionLocal() as session:
        flag = session.get(SystemFlag, "kill_switch_enabled")
        flag.value = True
        session.commit()

    execute_calls = []
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: execute_calls.append(host)
    )

    _create_incident("incident-7e")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-7e")
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert execute_calls == []
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.PENDING_APPROVAL.value
        incident = session.get(Incident, "incident-7e")
        assert incident.status == IncidentStatus.PENDING_APPROVAL.value
        entries = session.query(AuditEntry).filter_by(incident_id="incident-7e").all()
        assert entries[-1].event_type == audit.EVENT_RISKY_ACTION_BLOCKED_BY_KILL_SWITCH


def test_execute_approved_action_kill_switch_mid_execution_marks_failed_not_reverted(
    isolated_db, monkeypatch
):
    execute_calls = []

    def fake_execute(host, command):
        if command == "systemctl | grep ceph || true":
            return "  ceph-fsid@osd.9.service   loaded active running   x\n"
        execute_calls.append(host)
        if host == "10.20.1.83":
            with db_module.SessionLocal() as session:
                flag = session.get(SystemFlag, "kill_switch_enabled")
                flag.value = True
                session.commit()
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-7f")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session, "incident-7f", nodes=["10.20.1.83", "10.20.1.78"]
        )
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert execute_calls == ["10.20.1.83"]  # second node never touched
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        # Node 1 already ran for real — must surface as FAILED, not silently
        # revert to PENDING_APPROVAL (same reasoning as the SAFE-path
        # equivalent, test_diagnose_incident_kill_switch_checked_before_each_node_not_just_once).
        assert action.status == ActionStatus.FAILED.value


def test_execute_approved_action_skips_non_approved_action(isolated_db, monkeypatch):
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: pytest.fail("must not execute")
    )

    _create_incident("incident-7g")
    with db_module.SessionLocal() as session:
        action = Action(
            incident_id="incident-7g",
            action_id="restart_osd_daemon",
            classification=ActionClassification.RISKY.value,
            status=ActionStatus.EXECUTED.value,  # already resolved by a previous tick
            target_nodes='["10.20.1.83"]',
        )
        session.add(action)
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)  # must be a no-op, not re-execute

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.EXECUTED.value


# --- Story 8.1: cluster-deploy action_ids delegate to cluster_deploy.run() ---
# (cluster_deploy.run()'s own phase logic is covered exhaustively in
# tests/test_cluster_deploy.py — these tests only cover the dispatch/
# status-recording wiring in _execute_approved_action itself.)


def _approved_cluster_deploy_action(session, incident_id: str, action_params: dict | None) -> Action:
    import json as _json

    action = Action(
        incident_id=incident_id,
        action_id="deploy_cluster_cephadm",
        classification=ActionClassification.RISKY.value,
        status=ActionStatus.APPROVED.value,
        target_nodes=_json.dumps(["10.20.1.112"]),
        action_params=_json.dumps(action_params) if action_params is not None else None,
    )
    session.add(action)
    session.commit()
    return action


def test_execute_approved_action_delegates_to_cluster_deploy_for_deploy_action_ids(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, cmd: pytest.fail("must not use the generic loop")
    )
    run_calls = []

    def fake_run(action_pk, action_id, action_params, incident_id, write_progress, check_kill_switch):
        run_calls.append((action_pk, action_id, action_params, incident_id))
        return True

    monkeypatch.setattr(router_client.cluster_deploy, "run", fake_run)

    _create_incident("incident-8a")
    with db_module.SessionLocal() as session:
        action = _approved_cluster_deploy_action(session, "incident-8a", {"version": "18.2.8", "nodes": []})
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert len(run_calls) == 1
    assert run_calls[0][0] == action_pk
    assert run_calls[0][1] == "deploy_cluster_cephadm"
    assert run_calls[0][3] == "incident-8a"
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        assert action.executed_at is not None
        incident = session.get(Incident, "incident-8a")
        assert incident.status == IncidentStatus.RESOLVED.value
        entries = session.query(AuditEntry).filter_by(incident_id="incident-8a").all()
        assert entries[-1].event_type == audit.EVENT_RISKY_ACTION_EXECUTED


def test_execute_approved_action_cluster_deploy_failure_marks_failed(isolated_db, monkeypatch):
    monkeypatch.setattr(router_client.cluster_deploy, "run", lambda *a, **kw: False)

    _create_incident("incident-8b")
    with db_module.SessionLocal() as session:
        action = _approved_cluster_deploy_action(session, "incident-8b", {"version": "18.2.8", "nodes": []})
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value
        incident = session.get(Incident, "incident-8b")
        assert incident.status == IncidentStatus.FAILED.value


def test_execute_approved_action_cluster_deploy_malformed_action_params_marks_failed(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(
        router_client.cluster_deploy, "run", lambda *a, **kw: pytest.fail("must not be called")
    )

    _create_incident("incident-8c")
    with db_module.SessionLocal() as session:
        action = _approved_cluster_deploy_action(session, "incident-8c", action_params=None)
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.FAILED.value


def test_poll_approved_actions_processes_pending_approved_rows_then_stops(isolated_db, monkeypatch):
    execute_calls = []

    def fake_execute(host, command):
        if command == "systemctl | grep ceph || true":
            return "  ceph-fsid@osd.4.service   loaded active running   x\n"
        execute_calls.append(host)
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-8a")
    _create_incident("incident-8b")
    with db_module.SessionLocal() as session:
        _approved_action(session, "incident-8a", nodes=["10.20.1.83"])
        _approved_action(session, "incident-8b", nodes=["10.20.1.78"])

    asyncio.run(router_client.poll_approved_actions(poll_interval=0, max_iterations=1))

    assert sorted(execute_calls) == ["10.20.1.78", "10.20.1.83"]
    with db_module.SessionLocal() as session:
        statuses = {a.incident_id: a.status for a in session.query(Action).all()}
        assert statuses["incident-8a"] == ActionStatus.EXECUTED.value
        assert statuses["incident-8b"] == ActionStatus.EXECUTED.value


def test_diagnose_incident_redelivery_of_already_resolved_action_restores_incident_status(
    isolated_db, monkeypatch
):
    # Simulates: incident already fully AUTO_FIXED on a prior attempt, then
    # the message gets redelivered. worker/main.py::_handle_message would
    # have already reset Incident.status to DIAGNOSING before this call.
    _create_incident("incident-5h")
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5h")
        incident.status = IncidentStatus.DIAGNOSING.value  # as _handle_message would set it
        session.add(
            Action(
                incident_id="incident-5h",
                action_id="resync_ntp",
                classification=ActionClassification.SAFE.value,
                status=ActionStatus.AUTO_EXECUTED.value,
            )
        )
        session.commit()

    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: execute_calls.append(host)
    )

    envelope = dict(ENVELOPE, incident_id="incident-5h")
    asyncio.run(router_client.diagnose_incident("incident-5h", envelope))

    assert execute_calls == []  # never re-executed
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5h")
        assert incident.status == IncidentStatus.AUTO_FIXED.value  # restored, not stuck DIAGNOSING
        assert session.query(Action).filter_by(incident_id="incident-5h").count() == 1


def test_diagnose_incident_malformed_nodes_field_marks_failed_instead_of_guessing(
    isolated_db, monkeypatch
):
    execute_calls = []
    monkeypatch.setattr(router_client, "_call_router", _fake_call_router_safe)
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, command: execute_calls.append(host)
    )

    _create_incident("incident-5i")
    # nodes is a bare string, not a list — must not be iterated character by character.
    envelope = dict(ENVELOPE, incident_id="incident-5i", nodes="10.20.1.249")

    asyncio.run(router_client.diagnose_incident("incident-5i", envelope))

    assert execute_calls == []
    with db_module.SessionLocal() as session:
        incident = session.get(Incident, "incident-5i")
        assert incident.status == IncidentStatus.FAILED.value


def test_every_safe_action_id_has_a_command_defined():
    from worker.executor.commands import _MANAGEMENT_COMMAND_BUILDERS, COMMANDS
    from worker.policy.gate import SAFE_ACTION_IDS

    # restart_osd_daemon is get_command()'s other special-cased id (handled
    # via _restart_osd_daemon_command, not the COMMANDS dict) — not
    # currently in `safe:`, but included here so this guard keeps working if
    # that ever changes, same spirit as covering the management builders.
    defined = COMMANDS.keys() | _MANAGEMENT_COMMAND_BUILDERS.keys() | {"restart_osd_daemon"}
    missing = SAFE_ACTION_IDS - defined
    assert not missing, f"SAFE action_ids with no Command defined: {missing}"


def test_warn_if_missing_worker_ssh_key_logs_warning_when_path_does_not_exist(caplog, tmp_path):
    missing_path = str(tmp_path / "does-not-exist")
    with caplog.at_level("WARNING"):
        router_client._warn_if_missing_worker_ssh_key(missing_path)
    assert "does not exist" in caplog.text


def test_warn_if_missing_worker_ssh_key_silent_when_path_exists(caplog, tmp_path):
    real_path = tmp_path / "fake-key"
    real_path.write_text("not a real key")
    with caplog.at_level("WARNING"):
        router_client._warn_if_missing_worker_ssh_key(str(real_path))
    assert "does not exist" not in caplog.text
