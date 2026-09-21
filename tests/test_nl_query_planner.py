import asyncio
import time
from dataclasses import replace

from dashboard.ceph_tools import FIXED_TOOL_COMMANDS
from shared.natural_language import (
    FIXED_READ_ONLY_TOOL_REGISTRY,
    execute_query_plan,
    plan_query,
    route_natural_language,
)


def test_registry_matches_existing_fixed_ceph_tool_allowlist():
    assert set(FIXED_READ_ONLY_TOOL_REGISTRY) == set(FIXED_TOOL_COMMANDS)
    assert all(spec.read_only and spec.risk_level == "read_only" for spec in FIXED_READ_ONLY_TOOL_REGISTRY.values())


def test_cluster_health_plan_uses_summary_before_detail():
    intent = route_natural_language("Cụm đang HEALTH_WARN vì sao?", cluster_id="prod")
    plan = plan_query(intent)

    assert plan.status == "ready"
    assert plan.executable is True
    assert [call.tool_name for call in plan.calls] == [
        "get_cluster_status", "get_health_detail", "get_mon_stat"
    ]
    assert plan.calls[0].priority < plan.calls[1].priority
    assert all(call.spec.read_only for call in plan.calls)


def test_plan_requires_cluster_scope_before_any_tool_call():
    intent = route_natural_language("Kiểm tra OSD đang down")
    plan = plan_query(intent)

    assert plan.status == "clarification_required"
    assert plan.calls == ()
    assert "cluster" in (plan.clarification_question or "").lower()


def test_unsupported_intent_fails_closed_without_generic_shell_tool():
    intent = route_natural_language("Kiểm tra RGW", cluster_id="prod")
    plan = plan_query(intent)

    assert plan.status == "unsupported"
    assert plan.calls == ()
    assert "run_ceph_command" not in FIXED_READ_ONLY_TOOL_REGISTRY


def test_low_confidence_intent_is_not_executed():
    intent = route_natural_language("Giúp tôi với", cluster_id="prod")
    plan = plan_query(intent)

    assert plan.status == "clarification_required"
    assert plan.executable is False


def test_executor_runs_independent_tools_in_parallel():
    plan = plan_query(route_natural_language("Kiểm tra dung lượng pool", cluster_id="prod"))
    active = 0
    max_active = 0

    def runner(tool_name, _args, cluster_id):
        assert cluster_id == "prod"
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.05)
        active -= 1
        return {"tool": tool_name}

    result = asyncio.run(execute_query_plan(plan, runner, max_concurrency=2))

    assert result.status == "ok"
    assert result.partial is False
    assert len(result.evidence) == 2
    assert max_active == 2


def test_executor_keeps_partial_result_when_one_tool_fails():
    plan = plan_query(route_natural_language("Kiểm tra PG degraded", cluster_id="prod"))

    def runner(tool_name, _args, cluster_id):
        assert cluster_id == "prod"
        if tool_name == "get_health_detail":
            raise RuntimeError("health command failed")
        return {"pgs": 32}

    result = asyncio.run(execute_query_plan(plan, runner))

    assert result.status == "partial"
    assert result.partial is True
    assert {item.status for item in result.evidence} == {"ok", "error"}
    assert result.errors[0]["tool"] == "get_health_detail"


def test_executor_adds_deterministic_findings_to_snapshot_evidence():
    plan = plan_query(route_natural_language("Cụm đang HEALTH_WARN", cluster_id="prod"))

    def runner(tool_name, _args, _cluster_id):
        if tool_name == "get_cluster_status":
            return {
                "data": {"health": {"status": "HEALTH_WARN", "checks": {"OSD_DOWN": {}}}},
                "meta": {"available": True, "stale": False, "partial": False},
            }
        return {
            "data": {},
            "meta": {"available": False, "stale": False, "partial": True},
        }

    result = asyncio.run(execute_query_plan(plan, runner))

    assert any(item["code"] == "HEALTH_WARN" for item in result.findings)
    assert result.to_dict()["findings"]


def test_executor_marks_per_tool_timeout_without_cancelling_sibling():
    plan = plan_query(route_natural_language("Kiểm tra OSD", cluster_id="prod"))
    timed_call = replace(
        plan.calls[0], spec=replace(plan.calls[0].spec, timeout_seconds=0.05)
    )
    plan = replace(plan, calls=(timed_call, *plan.calls[1:]))

    def runner(tool_name, _args, cluster_id):
        assert cluster_id == "prod"
        if tool_name == "get_osd_stat":
            time.sleep(0.2)
        return {"tool": tool_name}

    result = asyncio.run(execute_query_plan(plan, runner))

    assert result.status == "partial"
    assert any(item.tool_name == "get_osd_stat" and item.status == "timeout" for item in result.evidence)
    assert any(item.tool_name == "get_osd_tree" and item.status == "ok" for item in result.evidence)


def test_executor_bounds_aggregate_evidence_output():
    plan = plan_query(
        route_natural_language("Kiểm tra dung lượng pool", cluster_id="prod"),
        max_output_bytes=120,
    )

    def runner(_tool_name, _args, cluster_id):
        assert cluster_id == "prod"
        return {"items": ["x" * 200]}

    result = asyncio.run(execute_query_plan(plan, runner))

    assert result.status == "ok"
    assert any(item.truncated for item in result.evidence)
    assert result.evidence[1].result["preview"] == ""


def test_executor_summarizes_large_collections_before_serialization():
    plan = plan_query(
        route_natural_language("Kiểm tra OSD", cluster_id="prod"),
        max_output_bytes=4000,
    )

    def runner(_tool_name, _args, cluster_id):
        assert cluster_id == "prod"
        return {"osds": [{"status": "up", "host": "node-a"} for _ in range(50)]}

    result = asyncio.run(execute_query_plan(plan, runner))

    summarized = [item for item in result.evidence if item.status == "ok" and item.truncated]
    assert summarized
    assert summarized[0].result["osds"]["summary"]["count"] == 50
