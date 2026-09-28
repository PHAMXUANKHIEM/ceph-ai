from datetime import datetime, timedelta

import pytest

from config.settings import settings
from shared import db
from shared.ai_telemetry import AIBudgetExceeded, ai_invocation, summary
from shared.models import AIInvocation


def test_ai_invocation_records_content_free_usage(db_session, monkeypatch):
    monkeypatch.setattr(db, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "ai_telemetry_enabled", True)
    monkeypatch.setattr(settings, "ai_budget_hard_limit", False)
    monkeypatch.setattr(settings, "ai_cost_input_usd_per_million_tokens", 1.0)
    monkeypatch.setattr(settings, "ai_cost_output_usd_per_million_tokens", 2.0)

    with ai_invocation(
        feature="test_chat",
        provider="router",
        model="test-model",
        input_text="x" * 40,
        max_output_tokens=20,
        actor="admin",
    ) as call:
        call.set_response(output_text="y" * 20)

    with db.SessionLocal() as session:
        row = session.query(AIInvocation).one()
        assert row.status == "SUCCEEDED"
        assert row.input_tokens == 10
        assert row.output_tokens == 5
        assert row.tokens_estimated is True
        assert row.actor == "admin"
        assert row.estimated_cost_usd > 0
        assert "x" not in row.model

    result = summary(24)
    assert result["calls"] == 1
    assert result["errors"] == 0
    assert result["groups"][0]["feature"] == "test_chat"


def test_ai_invocation_records_failure_without_leaking_exception(db_session, monkeypatch):
    monkeypatch.setattr(db, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "ai_telemetry_enabled", True)
    with pytest.raises(RuntimeError, match="provider down"):
        with ai_invocation(feature="incident", provider="router", model="m", input_text="safe"):
            raise RuntimeError("provider down")

    with db.SessionLocal() as session:
        row = session.query(AIInvocation).one()
        assert row.status == "FAILED"
        assert row.error_code == "RuntimeError"


def test_hard_budget_blocks_before_provider_call(db_session, monkeypatch):
    monkeypatch.setattr(db, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(settings, "ai_telemetry_enabled", True)
    monkeypatch.setattr(settings, "ai_budget_hard_limit", True)
    monkeypatch.setattr(settings, "ai_budget_daily_usd", 0.02)
    monkeypatch.setattr(settings, "ai_budget_monthly_usd", 0.0)
    monkeypatch.setattr(settings, "ai_cost_input_usd_per_million_tokens", 100.0)
    monkeypatch.setattr(settings, "ai_cost_output_usd_per_million_tokens", 100.0)

    with ai_invocation(feature="chat", provider="router", model="m", input_text="x" * 100, max_output_tokens=100) as call:
        call.set_response(output_text="ok")

    monkeypatch.setattr(settings, "ai_budget_daily_usd", 0.001)
    with pytest.raises(AIBudgetExceeded):
        with ai_invocation(feature="chat", provider="router", model="m", input_text="x", max_output_tokens=1):
            pytest.fail("provider must not be called after the budget is full")


def test_budget_lock_is_only_requested_for_postgresql(monkeypatch):
    from shared import ai_telemetry

    calls = []

    class Dialect:
        def __init__(self, name):
            self.name = name

    class Bind:
        def __init__(self, name):
            self.dialect = Dialect(name)

    class Session:
        def __init__(self, name):
            self.name = name

        def get_bind(self):
            return Bind(self.name)

        def execute(self, statement):
            calls.append(str(statement))

    ai_telemetry._lock_budget_reservation(Session("sqlite"))
    ai_telemetry._lock_budget_reservation(Session("postgresql"))

    assert len(calls) == 1
    assert "pg_advisory_xact_lock" in calls[0]
