"""Intent-to-tool planning for bounded, read-only Ceph queries."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .schema import NaturalLanguageIntent
from .tool_registry import FIXED_READ_ONLY_TOOL_REGISTRY, ToolSpec


@dataclass(frozen=True)
class PlannedToolCall:
    tool_name: str
    arguments: dict[str, Any]
    purpose: str
    priority: int
    spec: ToolSpec = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "purpose": self.purpose,
            "priority": self.priority,
            "group": self.spec.group,
        }


@dataclass(frozen=True)
class QueryPlan:
    plan_version: str
    status: str
    intent: str
    cluster_id: str | None
    calls: tuple[PlannedToolCall, ...] = ()
    requested_filters: dict[str, Any] = field(default_factory=dict)
    time_range: dict[str, Any] = field(default_factory=dict)
    clarification_question: str | None = None
    unsupported_reason: str | None = None
    max_duration_seconds: float = 12.0
    max_output_bytes: int = 12000
    max_tool_calls: int = 4

    @property
    def executable(self) -> bool:
        return self.status == "ready" and bool(self.calls)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["calls"] = [call.to_dict() for call in self.calls]
        value["executable"] = self.executable
        return value


_INTENT_TO_TOOLS: dict[str, tuple[tuple[str, str], ...]] = {
    "cluster_health": (
        ("get_cluster_status", "Tổng quan health trước tiên"),
        ("get_health_detail", "Chi tiết các health check"),
        ("get_mon_stat", "Kiểm tra MON quorum"),
    ),
    "osd_health": (
        ("get_osd_stat", "Tóm tắt trạng thái OSD"),
        ("get_osd_tree", "Đối chiếu host và OSD chi tiết"),
    ),
    "pg_health": (
        ("get_pg_stat", "Tóm tắt trạng thái PG"),
        ("get_health_detail", "Chi tiết health check liên quan PG"),
    ),
    "pool_capacity": (
        ("get_df", "Tóm tắt dung lượng"),
        ("get_pool_list", "Chi tiết pool và PG"),
    ),
    "crush_analysis": (
        ("get_osd_tree", "Cây CRUSH và phân bố host/OSD"),
    ),
}

_UNSUPPORTED_INTENT_MESSAGES = {
    "node_metrics": "Node metrics hiện đi qua collector riêng, chưa có fixed snapshot tool cho planner.",
    "rgw_diagnosis": "RGW/S3 cần tool inventory riêng để tránh truy vấn raw ngoài planner.",
    "volume_insight": "Volume/RBD cần tool inventory riêng với pagination và cluster scope.",
    "log_search": "Log search cần query tool có giới hạn cursor và kích thước kết quả.",
    "backup_status": "Backup cần snapshot/status tool riêng để phân biệt stale và failed.",
}


def _time_range_dict(intent: NaturalLanguageIntent) -> dict[str, Any]:
    return intent.time_range.to_dict()


def plan_query(
    intent: NaturalLanguageIntent,
    *,
    max_tool_calls: int = 4,
    max_duration_seconds: float = 12.0,
    max_output_bytes: int = 12000,
) -> QueryPlan:
    """Create a validated plan without performing I/O."""

    if not intent.cluster_id:
        return QueryPlan(
            plan_version="query-v1", status="clarification_required", intent=intent.intent,
            cluster_id=None, requested_filters=dict(intent.filters),
            time_range=_time_range_dict(intent),
            clarification_question="Bạn muốn truy vấn cluster nào? Hãy chọn cluster trước khi tiếp tục.",
            max_duration_seconds=max_duration_seconds, max_output_bytes=max_output_bytes,
            max_tool_calls=max_tool_calls,
        )
    if intent.needs_clarification or intent.confidence < 0.75:
        return QueryPlan(
            plan_version="query-v1", status="clarification_required", intent=intent.intent,
            cluster_id=intent.cluster_id, requested_filters=dict(intent.filters),
            time_range=_time_range_dict(intent),
            clarification_question=intent.clarification_question
            or "Bạn muốn kiểm tra thành phần nào của cluster?",
            max_duration_seconds=max_duration_seconds, max_output_bytes=max_output_bytes,
            max_tool_calls=max_tool_calls,
        )
    if intent.mode != "read_only":
        return QueryPlan(
            plan_version="query-v1", status="clarification_required", intent=intent.intent,
            cluster_id=intent.cluster_id, requested_filters=dict(intent.filters),
            time_range=_time_range_dict(intent),
            clarification_question="Planner hiện chỉ hỗ trợ truy vấn read-only.",
            max_duration_seconds=max_duration_seconds, max_output_bytes=max_output_bytes,
            max_tool_calls=max_tool_calls,
        )

    template = _INTENT_TO_TOOLS.get(intent.intent)
    if template is None:
        return QueryPlan(
            plan_version="query-v1", status="unsupported", intent=intent.intent,
            cluster_id=intent.cluster_id, requested_filters=dict(intent.filters),
            time_range=_time_range_dict(intent),
            unsupported_reason=_UNSUPPORTED_INTENT_MESSAGES.get(
                intent.intent, "Intent này chưa có read-only tool mapping."
            ),
            max_duration_seconds=max_duration_seconds, max_output_bytes=max_output_bytes,
            max_tool_calls=max_tool_calls,
        )

    calls: list[PlannedToolCall] = []
    seen: set[str] = set()
    for priority, (tool_name, purpose) in enumerate(template, start=1):
        if tool_name in seen:
            continue
        spec = FIXED_READ_ONLY_TOOL_REGISTRY.get(tool_name)
        if spec is None or not spec.read_only or spec.risk_level != "read_only":
            continue
        seen.add(tool_name)
        calls.append(
            PlannedToolCall(
                tool_name=tool_name,
                arguments={},
                purpose=purpose,
                priority=priority,
                spec=spec,
            )
        )
        if len(calls) >= max(1, max_tool_calls):
            break

    if not calls:
        return QueryPlan(
            plan_version="query-v1", status="unsupported", intent=intent.intent,
            cluster_id=intent.cluster_id,
            unsupported_reason="Không có fixed read-only tool hợp lệ cho intent này.",
            max_duration_seconds=max_duration_seconds, max_output_bytes=max_output_bytes,
            max_tool_calls=max_tool_calls,
        )
    return QueryPlan(
        plan_version="query-v1", status="ready", intent=intent.intent,
        cluster_id=intent.cluster_id, calls=tuple(calls),
        requested_filters=dict(intent.filters), time_range=_time_range_dict(intent),
        max_duration_seconds=max_duration_seconds, max_output_bytes=max_output_bytes,
        max_tool_calls=max_tool_calls,
    )

