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
    assert "crash_archive_all" in router_client.VALID_ACTION_IDS


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
        if command == "systemctl --all | grep ceph || true":
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
        if command == "systemctl --all | grep ceph || true":
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
        if command == "systemctl --all | grep ceph || true":
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


# --- Package upgrade "require-osd-release" finalization (2026-07-28) -------
# ceph-deploy/package upgrades (unlike cephadm's `ceph orch upgrade`, which
# does this itself) never bump require_osd_release on their own — left
# alone, the cluster sits in permanent HEALTH_WARN (OSD_UPGRADE_FINISHED)
# even though every node's packages/daemons genuinely finished upgrading.
# See worker/llm/router_client.py's _PACKAGE_UPGRADE_ACTION_IDS comment.


def test_execute_approved_action_package_upgrade_runs_require_osd_release_finalize(
    isolated_db, monkeypatch
):
    executed = []

    def fake_execute(host, command):
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112,10.20.1.95")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-finalize")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-finalize",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=["10.20.1.112"],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert ("10.20.1.112", "ceph osd require-osd-release octopus") in executed

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    finalize_step = progress[-1]
    assert finalize_step["command"] == "ceph osd require-osd-release octopus"
    assert finalize_step["status"] == "done"
    assert finalize_step["started_at"]
    assert finalize_step["finished_at"]


def test_execute_approved_action_package_upgrade_finalize_failure_does_not_fail_action(
    isolated_db, monkeypatch
):
    """The per-node installs are the real work — a hiccup running the
    finalize command afterwards must not retroactively turn an otherwise-
    successful multi-node upgrade into FAILED."""
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command):
        if command.startswith("ceph osd require-osd-release"):
            raise ExecutorError("mon unreachable")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-finalize-fail")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-finalize-fail",
            action_id="upgrade_ceph_cluster_package_local",
            nodes=["10.20.1.112"],
        )
        action.action_params = json.dumps(
            {"target_version": "15.2.17", "package_dir": "/opt/ceph-packages"}
        )
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    finalize_step = progress[-1]
    assert finalize_step["status"] == "failed"
    assert finalize_step["error"] == "mon unreachable"


def test_execute_approved_action_non_package_action_skips_finalize(isolated_db, monkeypatch):
    """restart_osd_daemon (and every other non-upgrade action_id) must not
    trigger the require-osd-release finalization step."""
    executed = []

    def fake_execute(host, command):
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-fsid@osd.3.service   loaded active running   x\n"
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)

    _create_incident("incident-no-finalize")
    with db_module.SessionLocal() as session:
        action = _approved_action(session, "incident-no-finalize", nodes=["10.20.1.112"])
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert not any("require-osd-release" in cmd for _host, cmd in executed)


# --- noout/noscrub/nodeep-scrub/nosnaptrim around package upgrades (2026-08-04) -
# Ceph's own cephadm orchestrator does NOT set/unset these during `ceph orch
# upgrade start` either (verified against src/pybind/mgr/cephadm/upgrade.py —
# it only manages `noautoscale`), and the package-based path has no
# orchestrator behind it at all, so nothing else protects against
# scrub/backfill churn while OSDs bounce one host at a time.


def test_execute_approved_action_package_upgrade_sets_flags_before_hosts_and_unsets_after(
    isolated_db, monkeypatch
):
    executed = []

    def fake_execute(host, command):
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112,10.20.1.95")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-flags")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-flags",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=["10.20.1.83"],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    set_command = "ceph osd set noout && ceph osd set noscrub && ceph osd set nodeep-scrub && ceph osd set nosnaptrim"
    unset_command = "ceph osd unset noout; ceph osd unset noscrub; ceph osd unset nodeep-scrub; ceph osd unset nosnaptrim"
    assert ("10.20.1.112", set_command) in executed
    assert ("10.20.1.112", unset_command) in executed
    # Runs on the first configured MON node, not the (unrelated) host the
    # actual package install ran on.
    assert not any(cmd == set_command and host != "10.20.1.112" for host, cmd in executed)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        progress = json.loads(action.execution_progress)

    # Both appended (not inserted at the front — see _set_upgrade_osd_flags'
    # own comment on why that would corrupt the per-host loop's positional
    # writes into `progress`) — set-flags right after the per-host
    # placeholder(s), unset-flags right before the require-osd-release
    # finalize step at the very end. Story 7.2: some phase-scoped entries
    # (e.g. an OSD-role host with no ACTUAL osd unit discovered) never get
    # a "command" key at all — use .get() so this lookup doesn't choke on
    # those on its way to the flags steps, which always have one.
    set_step = next(p for p in progress if p.get("command") == set_command)
    unset_step = next(p for p in progress if p.get("command") == unset_command)
    assert set_step["status"] == "done"
    assert unset_step["status"] == "done"
    assert progress[-1]["command"] == "ceph osd require-osd-release octopus"
    assert progress.index(set_step) < progress.index(unset_step) < len(progress) - 1


def test_execute_approved_action_package_upgrade_proceeds_when_set_flags_fails(
    isolated_db, monkeypatch
):
    """A failure suppressing scrub/backfill must not block the actual
    (approved, expected) package install — same best-effort posture as
    the require-osd-release finalize step."""
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command):
        if command.startswith("ceph osd set"):
            raise ExecutorError("mon busy")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-set-fail")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-set-fail",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=["10.20.1.83"],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        # The install itself still succeeded — a setup-step hiccup doesn't
        # retroactively fail the whole upgrade.
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    # Story 7.2: .get() guard, same reasoning as the sibling test above —
    # a phase-scoped "nothing to restart here" entry has no "command" key.
    set_step = next(p for p in progress if (p.get("command") or "").startswith("ceph osd set"))
    assert set_step["status"] == "failed"
    assert set_step["error"] == "mon busy"


def test_execute_approved_action_package_upgrade_unset_failure_does_not_fail_action(
    isolated_db, monkeypatch
):
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command):
        if command.startswith("ceph osd unset"):
            raise ExecutorError("mon unreachable")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-unset-fail")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-unset-fail",
            action_id="upgrade_ceph_cluster_package_local",
            nodes=["10.20.1.83"],
        )
        action.action_params = json.dumps(
            {"target_version": "15.2.17", "package_dir": "/opt/ceph-packages"}
        )
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    unset_step = progress[-2]
    assert unset_step["status"] == "failed"
    assert unset_step["error"] == "mon unreachable"


def test_execute_approved_action_package_upgrade_skips_flags_when_no_mon_configured(
    isolated_db, monkeypatch, caplog
):
    executed = []

    def fake_execute(host, command):
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-no-mon")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-no-mon",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=["10.20.1.83"],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    with caplog.at_level("WARNING"):
        router_client._execute_approved_action(action_pk)

    # The install itself still ran on its own target node — only the
    # cluster-wide flags step (which needs a MON) was skipped.
    assert not any("ceph osd set noout" in cmd for _host, cmd in executed)
    assert "skipping" in caplog.text.lower()


# --- Story 7.2 (2026-08-04): phased MON->MGR->OSD->MDS/RGW restart ---------


def _classify_mutating_call(command: str) -> str | None:
    """Buckets a real (non-discovery) executed command into "install" or
    "restart" for the phase-order assertions below — None for anything
    else (the noout/noscrub flags bracket, require-osd-release finalize),
    which those tests don't care about ordering-wise."""
    if command == "systemctl --all | grep ceph || true":
        return None
    if "systemctl restart" in command:
        return "restart"
    if "apt-get install" in command or "dnf install" in command:
        return "install"
    return None


def test_execute_approved_action_package_upgrade_runs_install_then_mon_then_osd_in_order(
    isolated_db, monkeypatch
):
    """I/O matrix row: 3 MON + 2 separate OSD hosts — install runs on all 5
    hosts, then MON restarts on the 3 MON hosts, then OSD restarts on the 2
    OSD hosts, in that order (call-order assertion, not just final state)."""
    from config.settings import settings

    mon_hosts = ["10.20.1.1", "10.20.1.2", "10.20.1.3"]
    osd_hosts = ["10.20.1.4", "10.20.1.5"]
    nodes = mon_hosts + osd_hosts

    discovery_output = {h: f"  ceph-mon@{h}.service   loaded active running   x\n" for h in mon_hosts}
    discovery_output.update(
        {h: f"  ceph-osd@{h}.service   loaded active running   x\n" for h in osd_hosts}
    )

    calls = []

    def fake_execute(host, command):
        calls.append((host, command))
        if command == "systemctl --all | grep ceph || true":
            return discovery_output.get(host, "")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", ",".join(mon_hosts))
    monkeypatch.setattr(settings, "ceph_osd_nodes", ",".join(osd_hosts))
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-phase-order")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-phase-order",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=nodes,
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    sequence = [(host, kind) for host, cmd in calls if (kind := _classify_mutating_call(cmd))]
    expected = (
        [(h, "install") for h in nodes]
        + [(h, "restart") for h in mon_hosts]
        + [(h, "restart") for h in osd_hosts]
    )
    assert sequence == expected

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    # 5 install steps + 3 mon + 2 osd = 10 phase-tagged steps (plus the
    # flags/finalize steps, not phase-tagged — see _set_upgrade_osd_flags).
    phase_counts = {}
    for step in progress:
        phase_counts[step.get("phase")] = phase_counts.get(step.get("phase"), 0) + 1
    assert phase_counts.get("install") == 5
    assert phase_counts.get("mon") == 3
    assert phase_counts.get("osd") == 2
    assert all(step["status"] == "done" for step in progress if step.get("phase"))


def test_execute_approved_action_package_upgrade_colocated_host_not_double_restarted(
    isolated_db, monkeypatch
):
    """I/O matrix row: a MON+OSD colocated host installs once, and its MON
    unit restarts in the MON phase while its OSD unit restarts in the OSD
    phase — two separate progress entries, never a double-restart of the
    same systemd unit."""
    from config.settings import settings

    host = "10.20.1.9"
    discovery_output = "  ceph-mon@a.service   loaded active running   x\n  ceph-osd@0.service   loaded active running   x\n"

    calls = []

    def fake_execute(h, command):
        calls.append((h, command))
        if command == "systemctl --all | grep ceph || true":
            return discovery_output
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", host)
    monkeypatch.setattr(settings, "ceph_osd_nodes", host)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-colocated")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-colocated",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=[host],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    install_calls = [c for h, c in calls if "apt-get install" in c or "dnf install" in c]
    mon_restart_calls = [c for h, c in calls if "systemctl restart ceph-mon@a.service" in c]
    osd_restart_calls = [c for h, c in calls if "systemctl restart ceph-osd@0.service" in c]
    assert len(install_calls) == 1  # installed exactly once, not once per role
    assert len(mon_restart_calls) == 1
    assert len(osd_restart_calls) == 1

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    phase_tagged = [p for p in progress if p.get("phase") in ("install", "mon", "osd")]
    assert [p["phase"] for p in phase_tagged] == ["install", "mon", "osd"]
    assert all(p["host"] == host for p in phase_tagged)


def test_execute_approved_action_package_upgrade_kill_switch_mid_phase_marks_failed_with_skips(
    isolated_db, monkeypatch
):
    """I/O matrix row: kill-switch flips ON after the MON phase is done,
    before the MGR/OSD phases run — remaining steps marked `skipped`
    (existing Vietnamese message), Action ends FAILED (not reverted, since
    the MON restart already ran for real)."""
    from config.settings import settings

    mon_host = "10.20.1.10"
    mgr_host = "10.20.1.11"
    osd_host = "10.20.1.12"
    nodes = [mon_host, mgr_host, osd_host]

    discovery_output = {
        mon_host: "  ceph-mon@a.service   loaded active running   x\n",
        mgr_host: "  ceph-mgr@a.service   loaded active running   x\n",
        osd_host: "  ceph-osd@0.service   loaded active running   x\n",
    }

    def fake_execute(h, command):
        if command == "systemctl --all | grep ceph || true":
            return discovery_output.get(h, "")
        if "systemctl restart ceph-mon" in command:
            # Flip the kill-switch right as the MON phase's real restart
            # runs — the fresh check ahead of the NEXT step (MGR phase)
            # must catch it.
            with db_module.SessionLocal() as session:
                flag = session.get(SystemFlag, "kill_switch_enabled")
                flag.value = True
                session.commit()
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", mon_host)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", mgr_host)
    monkeypatch.setattr(settings, "ceph_osd_nodes", osd_host)
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-kill-switch-mid-phase")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-kill-switch-mid-phase",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=nodes,
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value  # not reverted — real work already ran
        incident = session.get(Incident, "incident-kill-switch-mid-phase")
        assert incident.status == IncidentStatus.FAILED.value
        progress = json.loads(action.execution_progress)

    mon_step = next(p for p in progress if p.get("phase") == "mon")
    assert mon_step["status"] == "done"
    mgr_step = next(p for p in progress if p.get("phase") == "mgr")
    osd_step = next(p for p in progress if p.get("phase") == "osd")
    assert mgr_step["status"] == "skipped"
    assert osd_step["status"] == "skipped"
    assert "kill-switch" in mgr_step["error"].lower()
    assert "kill-switch" in osd_step["error"].lower()


def test_execute_approved_action_package_upgrade_restarts_leftover_rgw_host_in_final_phase(
    isolated_db, monkeypatch
):
    """I/O matrix row: a dedicated RGW box (no MON/MGR/OSD role at all) is
    installed in phase 0 and restarted in the final MDS/RGW phase."""
    from config.settings import settings

    rgw_host = "10.20.1.13"

    def fake_execute(h, command):
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-radosgw@rgw.a.service   loaded active running   x\n"
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", rgw_host)
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-rgw-final-phase")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-rgw-final-phase",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=[rgw_host],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    install_step = next(p for p in progress if p.get("phase") == "install")
    assert install_step["status"] == "done"
    rgw_step = next(p for p in progress if p.get("phase") == "mds_rgw")
    assert rgw_step["status"] == "done"
    assert rgw_step["host"] == rgw_host
    assert "ceph-radosgw@rgw.a.service" in rgw_step["command"]
    # No MON/MGR/OSD phase entries at all — this host has none of those roles.
    assert not any(p.get("phase") in ("mon", "mgr", "osd") for p in progress)


def test_execute_approved_action_package_upgrade_host_with_no_leftover_units_gets_no_mds_rgw_step(
    isolated_db, monkeypatch
):
    """A host with nothing left to restart in the final phase gets no
    progress entry for it at all — same silent no-op posture the pre-7.2
    single command already had when _restart_discovered_units_snippet
    found nothing."""
    from config.settings import settings

    host = "10.20.1.14"

    def fake_execute(h, command):
        if command == "systemctl --all | grep ceph || true":
            return ""  # nothing discovered at all
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", host)
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-no-leftover-units")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-no-leftover-units",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=[host],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        progress = json.loads(action.execution_progress)

    assert not any(p.get("phase") == "mds_rgw" for p in progress)


# --- Code review fixes (2026-08-04) on top of Story 7.2's phased executor --


def test_execute_approved_action_package_upgrade_skips_restart_for_host_with_failed_install(
    isolated_db, monkeypatch
):
    """Fix 1: a host whose install step failed must not have its later
    MON/MGR/OSD restart command issued — restarting a daemon against a
    possibly broken/partial package install is worse than leaving it
    alone. The other (successful) host's restart must still proceed
    normally."""
    from worker.executor.ssh_executor import ExecutorError
    from config.settings import settings

    good_host = "10.20.1.30"
    bad_host = "10.20.1.31"
    nodes = [good_host, bad_host]

    executed = []

    def fake_execute(host, command):
        executed.append((host, command))
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-mon@a.service   loaded active running   x\n"
        if host == bad_host and ("apt-get install" in command or "dnf install" in command):
            raise ExecutorError("package conflict")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", ",".join(nodes))
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-install-fail-gates-restart")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-install-fail-gates-restart",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=nodes,
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    restart_calls = [(h, c) for h, c in executed if "systemctl restart" in c]
    assert not any(h == bad_host for h, _c in restart_calls)
    assert any(h == good_host for h, _c in restart_calls)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        # bad_host's failed install means overall progress is not a clean
        # success — this is expected and orthogonal to the fix being tested.
        assert action.status == ActionStatus.FAILED.value
        progress = json.loads(action.execution_progress)

    bad_mon_step = next(p for p in progress if p.get("phase") == "mon" and p["host"] == bad_host)
    assert bad_mon_step["status"] == "skipped"
    assert "cài đặt gói thất bại" in bad_mon_step["error"]

    good_mon_step = next(p for p in progress if p.get("phase") == "mon" and p["host"] == good_host)
    assert good_mon_step["status"] == "done"


def test_execute_approved_action_package_upgrade_kill_switch_mid_sequence_skips_finalize(
    isolated_db, monkeypatch
):
    """Fix 2: a kill-switch trip mid-sequence (stopped_mid_sequence=True)
    must not run the require-osd-release finalize step, even though real
    work already executed (executed_any=True) — `ceph osd require-osd-
    release <codename>` must not run on the MON after the operator hit the
    emergency kill-switch."""
    from config.settings import settings

    mon_host = "10.20.1.40"
    mgr_host = "10.20.1.41"
    nodes = [mon_host, mgr_host]

    discovery_output = {
        mon_host: "  ceph-mon@a.service   loaded active running   x\n",
        mgr_host: "  ceph-mgr@a.service   loaded active running   x\n",
    }

    def fake_execute(h, command):
        if command == "systemctl --all | grep ceph || true":
            return discovery_output.get(h, "")
        if "systemctl restart ceph-mon" in command:
            with db_module.SessionLocal() as session:
                flag = session.get(SystemFlag, "kill_switch_enabled")
                flag.value = True
                session.commit()
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", mon_host)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", mgr_host)
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    finalize_calls = []
    monkeypatch.setattr(
        router_client,
        "_finalize_package_upgrade_osd_release",
        lambda *a, **k: finalize_calls.append((a, k)),
    )

    _create_incident("incident-kill-switch-skips-finalize")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-kill-switch-skips-finalize",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=nodes,
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert finalize_calls == []

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value


def test_execute_approved_action_package_upgrade_mds_rgw_phase_kill_switch_records_skipped_not_silent(
    isolated_db, monkeypatch
):
    """Fix 3: the MDS/RGW phase appends progress entries dynamically (only
    for a host confirmed to have something to restart), unlike the
    pre-populated MON/MGR/OSD phases — a mid-phase kill-switch trip must
    still leave a `skipped` entry for every host it never reached, not
    silently drop them from the audit trail."""
    from config.settings import settings

    rgw1 = "10.20.1.50"
    rgw2 = "10.20.1.51"
    nodes = [rgw1, rgw2]

    def fake_execute(h, command):
        if command == "systemctl --all | grep ceph || true":
            return "  ceph-radosgw@rgw.a.service   loaded active running   x\n"
        if "systemctl restart" in command and h == rgw1:
            with db_module.SessionLocal() as session:
                flag = session.get(SystemFlag, "kill_switch_enabled")
                flag.value = True
                session.commit()
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", "")
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", ",".join(nodes))
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-mds-rgw-kill-switch-skip")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-mds-rgw-kill-switch-skip",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=nodes,
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value
        progress = json.loads(action.execution_progress)

    rgw_steps = {p["host"]: p for p in progress if p.get("phase") == "mds_rgw"}
    assert rgw_steps[rgw1]["status"] == "done"
    # rgw2 must still get a recorded entry — not silently missing.
    assert rgw2 in rgw_steps
    assert rgw_steps[rgw2]["status"] == "skipped"
    assert "kill-switch" in rgw_steps[rgw2]["error"].lower()


def test_execute_approved_action_package_upgrade_unexpected_exception_still_unsets_flags(
    isolated_db, monkeypatch
):
    """Fix 4: an exception OTHER than ExecutorError propagating out of a
    phase (e.g. an unwrapped network/OS error) must still guarantee
    _unset_upgrade_osd_flags runs — otherwise noout/noscrub/nodeep-scrub/
    nosnaptrim are left set on the live cluster indefinitely."""
    from config.settings import settings

    mon_host = "10.20.1.60"
    executed = []

    def fake_execute(host, command):
        executed.append((host, command))
        if command == "systemctl --all | grep ceph || true":
            raise RuntimeError("ssh transport blew up")
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    monkeypatch.setattr(settings, "ceph_mon_nodes", mon_host)
    monkeypatch.setattr(settings, "ceph_mgr_nodes", "")
    monkeypatch.setattr(settings, "ceph_osd_nodes", "")
    monkeypatch.setattr(settings, "ceph_rgw_nodes", "")
    monkeypatch.setattr(settings, "ceph_exec_mode", "none")

    _create_incident("incident-pkg-upgrade-unexpected-exc")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-pkg-upgrade-unexpected-exc",
            action_id="upgrade_ceph_cluster_package_download",
            nodes=[mon_host],
        )
        action.action_params = json.dumps({"target_version": "15.2.17"})
        session.commit()
        action_pk = action.id

    with pytest.raises(RuntimeError):
        router_client._execute_approved_action(action_pk)

    unset_command = (
        "ceph osd unset noout; ceph osd unset noscrub; ceph osd unset nodeep-scrub; "
        "ceph osd unset nosnaptrim"
    )
    assert (mon_host, unset_command) in executed


def test_execute_approved_action_cephadm_upgrade_skips_router_clients_own_flags_handling(
    isolated_db, monkeypatch
):
    """cephadm's own `upgrade_ceph_cluster` action_id (ceph orch upgrade
    start) is a DIFFERENT action_id from the 2 package-based ones — its
    noout/noscrub/nodeep-scrub/nosnaptrim handling is baked directly into
    worker/executor/commands.py::_upgrade_ceph_cluster_command's own single
    command string instead (see that function's docstring for why: this
    app's own set/unset step-pair, scoped to _PACKAGE_UPGRADE_ACTION_IDS
    only, would be meaningless here — a SEPARATE `ceph osd set` SSH round
    trip achieves nothing `bash -c '... && ceph orch upgrade start ...'`
    doesn't already do in one, and there's no unset counterpart at all on
    this path — see dashboard/routes/upgrade.py's manual "Bỏ noout/
    noscrub..." button instead). This only asserts THIS app's own
    _set_upgrade_osd_flags/_unset_upgrade_osd_flags never ran as a
    SEPARATE progress step — the single upgrade command itself legitimately
    contains "ceph osd set noout" as a substring within its own chain."""
    executed = []

    def fake_execute(host, command):
        executed.append((host, command))
        return "ok"

    monkeypatch.setattr(router_client, "execute_command", fake_execute)
    monkeypatch.setattr(router_client.commands, "execute_command", fake_execute)
    from config.settings import settings

    monkeypatch.setattr(settings, "ceph_mon_nodes", "10.20.1.112")
    monkeypatch.setattr(settings, "ceph_exec_mode", "cephadm")

    _create_incident("incident-cephadm-upgrade-no-flags")
    with db_module.SessionLocal() as session:
        action = _approved_action(
            session,
            "incident-cephadm-upgrade-no-flags",
            action_id="upgrade_ceph_cluster",
            nodes=["10.20.1.112"],
        )
        action.action_params = json.dumps({"target_version": "16.2.15"})
        session.commit()
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    # Exactly one SSH round trip — the single chained upgrade command.
    # A dedicated router_client.py flags step would show up as an
    # ADDITIONAL, separate command here.
    assert len(executed) == 1
    assert executed[0][1].startswith("cephadm shell -- bash -c")
    assert "ceph orch upgrade start" in executed[0][1]
    assert not any("ceph osd unset" in cmd for _host, cmd in executed)


def test_execute_approved_action_marks_failed_host_in_progress(isolated_db, monkeypatch):
    from worker.executor.ssh_executor import ExecutorError

    def fake_execute(host, command):
        if command == "systemctl --all | grep ceph || true":
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
        if command == "systemctl --all | grep ceph || true":
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
        if command == "systemctl --all | grep ceph || true":
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


# --- Volumes "Đo hiệu năng tối đa" delegates to volume_perf.run() --------
# (volume_perf.run()'s own sweep/knee-detection logic is covered
# exhaustively in tests/test_volume_perf.py — these tests only cover the
# dispatch/status-recording wiring in _execute_approved_action itself,
# same split as the cluster-deploy family above.)


def _approved_volume_perf_action(session, incident_id: str, action_params: dict | None) -> Action:
    import json as _json

    action = Action(
        incident_id=incident_id,
        action_id="volume_perf_sweep",
        classification=ActionClassification.RISKY.value,
        status=ActionStatus.APPROVED.value,
        target_nodes=_json.dumps(["10.20.1.112"]),
        action_params=_json.dumps(action_params) if action_params is not None else None,
    )
    session.add(action)
    session.commit()
    return action


def test_execute_approved_action_delegates_to_volume_perf_for_volume_perf_sweep(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(
        router_client, "execute_command", lambda host, cmd: pytest.fail("must not use the generic loop")
    )
    run_calls = []

    def fake_run(action_pk, action_params, incident_id, write_progress, check_kill_switch):
        run_calls.append((action_pk, action_params, incident_id))
        return True

    monkeypatch.setattr(router_client.volume_perf, "run", fake_run)

    _create_incident("incident-9a")
    with db_module.SessionLocal() as session:
        action = _approved_volume_perf_action(session, "incident-9a", {"pool": "vms", "mon_ip": "10.20.1.112"})
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    assert len(run_calls) == 1
    assert run_calls[0][0] == action_pk
    assert run_calls[0][1] == {"pool": "vms", "mon_ip": "10.20.1.112"}
    assert run_calls[0][2] == "incident-9a"
    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.EXECUTED.value
        incident = session.get(Incident, "incident-9a")
        assert incident.status == IncidentStatus.RESOLVED.value


def test_execute_approved_action_volume_perf_failure_marks_failed(isolated_db, monkeypatch):
    monkeypatch.setattr(router_client.volume_perf, "run", lambda *a, **kw: False)

    _create_incident("incident-9b")
    with db_module.SessionLocal() as session:
        action = _approved_volume_perf_action(session, "incident-9b", {"pool": "vms", "mon_ip": "10.20.1.112"})
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        action = session.get(Action, action_pk)
        assert action.status == ActionStatus.FAILED.value
        incident = session.get(Incident, "incident-9b")
        assert incident.status == IncidentStatus.FAILED.value


def test_execute_approved_action_volume_perf_malformed_action_params_marks_failed(
    isolated_db, monkeypatch
):
    monkeypatch.setattr(
        router_client.volume_perf, "run", lambda *a, **kw: pytest.fail("must not be called")
    )

    _create_incident("incident-9c")
    with db_module.SessionLocal() as session:
        action = _approved_volume_perf_action(session, "incident-9c", action_params=None)
        action_pk = action.id

    router_client._execute_approved_action(action_pk)

    with db_module.SessionLocal() as session:
        assert session.get(Action, action_pk).status == ActionStatus.FAILED.value


def test_poll_approved_actions_processes_pending_approved_rows_then_stops(isolated_db, monkeypatch):
    execute_calls = []

    def fake_execute(host, command):
        if command == "systemctl --all | grep ceph || true":
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
    from worker.backup.engine import BACKUP_ACTION_IDS
    from worker.executor.commands import _MANAGEMENT_COMMAND_BUILDERS, COMMANDS
    from worker.policy.gate import SAFE_ACTION_IDS

    # restart_osd_daemon is get_command()'s other special-cased id (handled
    # via _restart_osd_daemon_command, not the COMMANDS dict) — not
    # currently in `safe:`, but included here so this guard keeps working if
    # that ever changes, same spirit as covering the management builders.
    #
    # 2026-07-30 (Epic 9, Story 9.1): rbd_backup_run/retention_sweep_delete/
    # restore_drill_execute are the FIRST Safe action_ids dispatched via a
    # bespoke orchestrator branch (worker/backup/engine.py) instead of the
    # generic per-host commands.py lookup this guard checks — same
    # "own multi-step orchestrator, not a single Command" reasoning
    # cluster_deploy_action_ids/volume_perf_sweep already have, but those
    # two families happen to be entirely Risky (never auto-executed), so
    # this guard never had to account for a bespoke-dispatched SAFE id
    # before now.
    defined = COMMANDS.keys() | _MANAGEMENT_COMMAND_BUILDERS.keys() | {"restart_osd_daemon"} | BACKUP_ACTION_IDS
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
