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


def test_summary_uses_model_specific_price_table(dashboard_client, monkeypatch):
    now = datetime(2026, 8, 28, 12, 0)
    monkeypatch.setattr("shared.ai_cost.settings.ai_cost_input_usd_per_million_tokens", 0.0)
    monkeypatch.setattr("shared.ai_cost.settings.ai_cost_output_usd_per_million_tokens", 0.0)
    with db.SessionLocal() as session:
        session.add(AIInvocation(
            id="model-priced", feature="log_rca", provider="codex", model_id="gpt-5.6-sol",
            status="SUCCESS", latency_ms=1, input_chars=4, output_chars=4, created_at=now,
        ))
        session.commit()
    data = summary(24, now=now)
    row = next(item for item in data["groups"] if item["model_id"] == "gpt-5.6-sol")
    assert row["input_usd_per_million_tokens"] == 4.0
    assert row["output_usd_per_million_tokens"] == 20.0
    assert row["estimated_cost_usd"] == 0.000024
    assert data["pricing_complete"] is True


def test_summary_converts_cost_to_vnd_and_exposes_totals(dashboard_client, monkeypatch):
    now = datetime(2026, 8, 28, 12, 0)
    monkeypatch.setattr("shared.ai_cost.settings.ai_cost_usd_to_vnd", 26290.0)
    with db.SessionLocal() as session:
        session.add(AIInvocation(
            id="vnd-priced", feature="ceph_chat", provider="codex", model_id="gpt-5.6-sol",
            status="SUCCESS", latency_ms=1, input_chars=4, output_chars=4, created_at=now,
        ))
        session.commit()
    data = summary(24, now=now)
    row = next(item for item in data["groups"] if item["model_id"] == "gpt-5.6-sol")
    assert data["usd_to_vnd"] == 26290.0
    assert row["estimated_cost_vnd"] == 1
    assert data["estimated_cost_vnd"] == 1
