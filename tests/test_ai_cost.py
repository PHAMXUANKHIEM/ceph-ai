from datetime import datetime, timedelta

from shared import db
from shared.ai_cost import summary
from shared.models import AIInvocation


def test_summary_estimates_tokens_and_groups(dashboard_client, monkeypatch):
    now = datetime(2026, 8, 27, 12, 0)
    with db.SessionLocal() as session:
        session.add_all([
            AIInvocation(id="a", feature="chat", provider="router", model_id="m", status="SUCCESS",
                         latency_ms=1, input_chars=8, output_chars=12, created_at=now),
            AIInvocation(id="b", feature="chat", provider="router", model_id="m", status="ERROR",
                         latency_ms=1, input_chars=4, output_chars=0, created_at=now - timedelta(hours=1)),
        ])
        session.commit()
    monkeypatch.setattr("shared.ai_cost.settings.ai_cost_input_usd_per_million_tokens", 1.0)
    monkeypatch.setattr("shared.ai_cost.settings.ai_cost_output_usd_per_million_tokens", 2.0)
    data = summary(24, now=now)
    assert data["calls"] == 2 and data["errors"] == 1
    assert data["input_tokens"] == 3 and data["output_tokens"] == 3
    assert data["estimated_cost_usd"] == 0.000009
    assert data["groups"][0]["feature"] == "chat"


def test_summary_hides_cost_when_pricing_is_not_configured(dashboard_client, monkeypatch):
    monkeypatch.setattr("shared.ai_cost.settings.ai_cost_input_usd_per_million_tokens", 0.0)
    monkeypatch.setattr("shared.ai_cost.settings.ai_cost_output_usd_per_million_tokens", 0.0)
    data = summary(24)
    assert data["pricing_configured"] is False
    assert data["estimated_cost_usd"] is None


def test_total_cost_is_not_lost_when_each_group_rounds_to_zero(dashboard_client, monkeypatch):
    now = datetime(2026, 8, 27, 12, 0)
    with db.SessionLocal() as session:
        session.add_all([
            AIInvocation(id="tiny-a", feature="tiny-a", provider="router", model_id="m", status="SUCCESS",
                         latency_ms=1, input_chars=4, output_chars=0, created_at=now),
            AIInvocation(id="tiny-b", feature="tiny-b", provider="router", model_id="m", status="SUCCESS",
                         latency_ms=1, input_chars=4, output_chars=0, created_at=now),
        ])
        session.commit()
    monkeypatch.setattr("shared.ai_cost.settings.ai_cost_input_usd_per_million_tokens", 0.5)
    monkeypatch.setattr("shared.ai_cost.settings.ai_cost_output_usd_per_million_tokens", 0.5)
    data = summary(24, now=now)
    assert data["estimated_cost_usd"] == 0.000001
