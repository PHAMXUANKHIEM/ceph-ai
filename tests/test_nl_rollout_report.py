import json
from datetime import datetime, timedelta

from scripts.report_natural_language_rollout import (
    _context,
    _latency_summary,
    _monitoring_window,
    count_samples,
)
from scripts.close_nl_rollout_monitoring import gate_status, record_gate


def test_rollout_report_latency_summary_is_bounded_and_deterministic():
    report = _latency_summary([1.0, 2.0, 3.0, 4.0, 100.0])

    assert report == {
        "count": 5,
        "p50_ms": 3.0,
        "p95_ms": 100.0,
        "max_ms": 100.0,
    }


def test_rollout_report_latency_summary_has_explicit_empty_state():
    assert _latency_summary([]) == {
        "count": 0,
        "p50_ms": None,
        "p95_ms": None,
        "max_ms": None,
    }


def test_rollout_report_context_rejects_malformed_or_non_object_json():
    assert _context("not-json") == {}
    assert _context("[]") == {}
    assert _context('{"rollout":{"scope":"canary"}}')["rollout"]["scope"] == "canary"


def test_rollout_monitoring_gate_requires_24_hours_and_enough_answers(tmp_path):
    state_path = tmp_path / "monitoring-start.json"
    start = datetime(2026, 9, 19, 8, 0, 0)

    first = _monitoring_window(now=start, state_path=state_path, samples=50)
    no_answers = _monitoring_window(now=start + timedelta(hours=497), state_path=state_path, samples=0)
    later = _monitoring_window(now=start + timedelta(hours=24), state_path=state_path, samples=20)

    assert first["ready_for_close"] is False and first["elapsed_hours"] == 0.0
    # 10/2026: the old gate closed the canary here, after 497 hours with 0 answers.
    assert no_answers["ready_for_close"] is False
    assert later["ready_for_close"] is True and later["elapsed_hours"] == 24.0
    assert (later["samples"], later["minimum_samples"]) == (20, 20)


def test_the_gate_result_is_recorded_outside_the_checkout(tmp_path):
    state_path = tmp_path / "monitoring-start.json"
    start = datetime(2026, 9, 19, 8, 0, 0)
    _monitoring_window(now=start, state_path=state_path)

    waiting = gate_status(state_path=state_path, samples=3, now=start + timedelta(hours=48))
    passed = gate_status(state_path=state_path, samples=25, now=start + timedelta(hours=48))
    record_gate(passed, tmp_path / "gate.json")

    assert waiting["status"] == "waiting" and passed["status"] == "passed"
    assert json.loads((tmp_path / "gate.json").read_text(encoding="utf-8"))["status"] == "passed"


def test_only_answers_with_a_context_count_as_samples():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from shared.models import Base, ChatMessage

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    since = datetime(2026, 10, 1)
    with sessionmaker(bind=engine)() as session:
        session.add_all([
            ChatMessage(session_id="s", role="assistant", content="ok", nl_context_json="{}",
                        created_at=since + timedelta(hours=1)),
            ChatMessage(session_id="s", role="user", content="q", nl_context_json='{"intent": {}}',
                        created_at=since + timedelta(hours=1)),
            ChatMessage(session_id="s", role="assistant", content="old", nl_context_json="{}",
                        created_at=since - timedelta(hours=1)),
            ChatMessage(session_id="s", role="assistant", content="no context", created_at=since + timedelta(hours=2)),
        ])
        session.commit()

        assert count_samples(session, since=since) == 1
