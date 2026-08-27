import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared import db
from shared.ai_observability import observe_ai_call
from shared.db import Base
from shared.models import AIInvocation


def _session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_records_success_without_content(monkeypatch):
    sessions = _session_factory()
    monkeypatch.setattr(db, "SessionLocal", sessions)

    @observe_ai_call("test_feature")
    async def call(secret):
        return {"answer": secret}

    assert asyncio.run(call("DO-NOT-STORE")) == {"answer": "DO-NOT-STORE"}
    with sessions() as session:
        row = session.query(AIInvocation).one()
        assert row.status == "SUCCESS"
        assert row.feature == "test_feature"
        assert row.input_chars > 0 and row.output_chars > 0
        assert "DO-NOT-STORE" not in repr(row.__dict__)


def test_records_only_exception_class(monkeypatch):
    sessions = _session_factory()
    monkeypatch.setattr(db, "SessionLocal", sessions)

    @observe_ai_call("failure")
    async def call():
        raise ValueError("SECRET ERROR DETAIL")

    try:
        asyncio.run(call())
    except ValueError:
        pass
    with sessions() as session:
        row = session.query(AIInvocation).one()
        assert row.status == "ERROR" and row.error_type == "ValueError"
        assert "SECRET ERROR DETAIL" not in repr(row.__dict__)


def test_telemetry_failure_does_not_break_call(monkeypatch):
    monkeypatch.setattr(db, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

    @observe_ai_call("resilient")
    async def call():
        return "ok"

    assert asyncio.run(call()) == "ok"


def test_when_false_skips_telemetry(monkeypatch):
    sessions = _session_factory()
    monkeypatch.setattr(db, "SessionLocal", sessions)

    @observe_ai_call("conditional", when=lambda allowed: allowed)
    async def call(allowed):
        return "not an AI call"

    assert asyncio.run(call(False)) == "not an AI call"
    with sessions() as session:
        assert session.query(AIInvocation).count() == 0


def test_forced_router_backend_ignores_chat_switches(monkeypatch):
    sessions = _session_factory()
    monkeypatch.setattr(db, "SessionLocal", sessions)
    monkeypatch.setattr("shared.ai_observability.settings.codex_chat_enabled", True)
    monkeypatch.setattr("shared.ai_observability.settings.router_provider", "9router")
    monkeypatch.setattr("shared.ai_observability.settings.router_model", "router-model")

    @observe_ai_call("router_only", backend="router")
    async def call():
        return "ok"

    asyncio.run(call())
    with sessions() as session:
        row = session.query(AIInvocation).one()
        assert (row.provider, row.model_id) == ("9router", "router-model")


def test_slow_telemetry_does_not_block_event_loop(monkeypatch):
    import time

    monkeypatch.setattr("shared.ai_observability._record", lambda **_values: time.sleep(0.05))

    @observe_ai_call("non_blocking")
    async def call():
        return "ok"

    async def exercise():
        task = asyncio.create_task(call())
        await asyncio.sleep(0.01)
        assert not task.done()
        return await task

    assert asyncio.run(exercise()) == "ok"
