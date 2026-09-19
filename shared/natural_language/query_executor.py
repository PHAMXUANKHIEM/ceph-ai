"""Bounded async executor for a natural-language read-only query plan."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable

from .query_planner import PlannedToolCall, QueryPlan


@dataclass(frozen=True)
class ToolEvidence:
    tool_name: str
    status: str
    started_at: str
    finished_at: str
    duration_ms: int
    result: Any = None
    error: str | None = None
    truncated: bool = False
    metadata: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "result": self.result,
            "error": self.error,
            "truncated": self.truncated,
            "metadata": self.metadata,
        }


@dataclass(frozen=True)
class QueryExecutionResult:
    plan_version: str
    status: str
    cluster_id: str | None
    evidence: tuple[ToolEvidence, ...] = ()
    errors: tuple[dict[str, str], ...] = ()
    duration_ms: int = 0
    partial: bool = False
    stale: bool | None = None
    refreshing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "status": self.status,
            "cluster_id": self.cluster_id,
            "evidence": [item.to_dict() for item in self.evidence],
            "errors": [dict(item) for item in self.errors],
            "duration_ms": self.duration_ms,
            "partial": self.partial,
            "stale": self.stale,
            "refreshing": self.refreshing,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_result(value: Any, max_bytes: int) -> tuple[Any, bool]:
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = json.dumps(str(value), ensure_ascii=False)
    if len(encoded.encode("utf-8")) <= max_bytes:
        return value, False
    preview = encoded.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
    return {"truncated": True, "preview": preview}, True


def _serialized_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8"))


def _extract_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("meta"), dict):
        return None
    return dict(value["meta"])


async def _invoke_runner(
    runner: Callable[[str, dict[str, Any], str], Any],
    call: PlannedToolCall,
    cluster_id: str,
) -> Any:
    # Fixed Ceph runners are blocking; run them outside the event loop.  An
    # async fake runner is also accepted for deterministic unit tests.
    # Pass cluster scope explicitly to the runner. A closure-selected cluster
    # is not sufficient as a safety boundary when this executor is reused by
    # another API or background worker.
    value = await asyncio.to_thread(runner, call.tool_name, dict(call.arguments), cluster_id)
    if inspect.isawaitable(value):
        return await value
    return value


async def execute_query_plan(
    plan: QueryPlan,
    runner: Callable[[str, dict[str, Any], str], Any],
    *,
    max_concurrency: int = 4,
) -> QueryExecutionResult:
    """Execute only a ready plan and preserve partial results on failures."""

    started = time.monotonic()
    if not plan.executable:
        return QueryExecutionResult(
            plan_version=plan.plan_version,
            status=plan.status,
            cluster_id=plan.cluster_id,
            errors=(
                {"stage": "planning", "kind": plan.status, "message":
                 plan.clarification_question or plan.unsupported_reason or "Plan is not executable"},
            ),
        )

    semaphore = asyncio.Semaphore(max(1, min(max_concurrency, len(plan.calls))))
    results: dict[str, ToolEvidence] = {}

    async def run_call(call: PlannedToolCall) -> None:
        async with semaphore:
            call_started = time.monotonic()
            started_at = _now_iso()
            try:
                value = await asyncio.wait_for(
                    _invoke_runner(runner, call, plan.cluster_id), timeout=call.spec.timeout_seconds
                )
            except asyncio.TimeoutError:
                results[call.tool_name] = ToolEvidence(
                    tool_name=call.tool_name, status="timeout", started_at=started_at,
                    finished_at=_now_iso(), duration_ms=int((time.monotonic() - call_started) * 1000),
                    error=f"Tool exceeded {call.spec.timeout_seconds:g} seconds",
                )
            except Exception as exc:  # one tool must not cancel sibling tools
                results[call.tool_name] = ToolEvidence(
                    tool_name=call.tool_name, status="error", started_at=started_at,
                    finished_at=_now_iso(), duration_ms=int((time.monotonic() - call_started) * 1000),
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                result, truncated = _bounded_result(value, plan.max_output_bytes)
                results[call.tool_name] = ToolEvidence(
                    tool_name=call.tool_name, status="ok", started_at=started_at,
                    finished_at=_now_iso(), duration_ms=int((time.monotonic() - call_started) * 1000),
                    result=result, truncated=truncated, metadata=_extract_metadata(value),
                )

    tasks = [asyncio.create_task(run_call(call)) for call in plan.calls]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=plan.max_duration_seconds)
    except asyncio.TimeoutError:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for call in plan.calls:
            if call.tool_name not in results:
                results[call.tool_name] = ToolEvidence(
                    tool_name=call.tool_name, status="timeout", started_at=_now_iso(),
                    finished_at=_now_iso(), duration_ms=int(plan.max_duration_seconds * 1000),
                    error=f"Query plan exceeded {plan.max_duration_seconds:g} seconds",
                )

    ordered_raw = [results[call.tool_name] for call in plan.calls if call.tool_name in results]
    # Bound the aggregate evidence payload, not only each individual tool
    # result. This protects the provider context when several tools return
    # large inventories in the same plan.
    remaining_bytes = max(0, plan.max_output_bytes)
    ordered_list: list[ToolEvidence] = []
    for item in ordered_raw:
        if item.status != "ok":
            ordered_list.append(item)
            continue
        bounded, truncated = _bounded_result(item.result, remaining_bytes)
        consumed = min(remaining_bytes, _serialized_size(bounded))
        remaining_bytes -= consumed
        ordered_list.append(replace(item, result=bounded, truncated=item.truncated or truncated))
    ordered = tuple(ordered_list)
    errors = tuple(
        {"tool": item.tool_name, "kind": item.status, "message": item.error or item.status}
        for item in ordered if item.status != "ok"
    )
    metadata = [item.metadata for item in ordered if item.metadata]
    stale = any(bool(item.get("stale")) for item in metadata)
    refreshing = any(bool(item.get("refreshing")) for item in metadata)
    metadata_errors = tuple(
        {"tool": item.tool_name, "kind": "snapshot_partial", "message": str(error)}
        for item in ordered
        for error in ((item.metadata or {}).get("partial_errors", {}) or {}).values()
        if item.status == "ok"
    )
    ok_count = sum(item.status == "ok" for item in ordered)
    if ok_count == len(plan.calls):
        status = "ok"
    elif ok_count:
        status = "partial"
    else:
        status = "failed"
    return QueryExecutionResult(
        plan_version=plan.plan_version, status=status, cluster_id=plan.cluster_id,
        evidence=ordered, errors=errors + metadata_errors,
        duration_ms=int((time.monotonic() - started) * 1000), partial=status == "partial" or bool(metadata_errors),
        stale=stale if metadata else None, refreshing=refreshing,
    )
