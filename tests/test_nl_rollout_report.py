from datetime import datetime, timedelta

from scripts.report_natural_language_rollout import (
    _context,
    _latency_summary,
    _monitoring_window,
)
from scripts.close_nl_rollout_monitoring import close_plan_if_ready


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


def test_rollout_monitoring_gate_requires_24_hours(tmp_path):
    state_path = tmp_path / "monitoring-start.json"
    start = datetime(2026, 9, 19, 8, 0, 0)

    first = _monitoring_window(now=start, state_path=state_path)
    later = _monitoring_window(
        now=start + timedelta(hours=24), state_path=state_path
    )

    assert first["ready_for_close"] is False
    assert first["elapsed_hours"] == 0.0
    assert later["ready_for_close"] is True
    assert later["elapsed_hours"] == 24.0


def test_rollout_closeout_waits_until_gate_and_then_updates_only_checkbox(tmp_path):
    state_path = tmp_path / "monitoring-start.json"
    plan_path = tmp_path / "plan.md"
    marker = "- [ ] Theo dõi latency, cost, rejection và approval trong 24–72 giờ."
    plan_path.write_text("before\n" + marker + "\nafter\n", encoding="utf-8")
    start = datetime(2026, 9, 19, 8, 0, 0)

    waiting = close_plan_if_ready(
        plan_path=plan_path, state_path=state_path, now=start
    )
    assert waiting["status"] == "waiting"
    assert marker in plan_path.read_text(encoding="utf-8")

    closed = close_plan_if_ready(
        plan_path=plan_path,
        state_path=state_path,
        now=start + timedelta(hours=24),
    )
    assert closed["status"] == "closed"
    updated = plan_path.read_text(encoding="utf-8")
    assert marker not in updated
    assert "- [x] Theo dõi latency" in updated
