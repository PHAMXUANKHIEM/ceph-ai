import asyncio

from sqlalchemy.orm import sessionmaker

from shared import db
from shared.ai_observability import observe_ai_call
from shared.db import Base, make_engine
from shared.models import AIInvocation


def _session_factory():
    engine = make_engine("sqlite:///:memory:")
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
