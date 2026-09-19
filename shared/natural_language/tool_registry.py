"""Metadata registry for the fixed, read-only Ceph query tools.

The registry is intentionally separate from the provider tool schema.  It is
used by the natural-language planner and does not execute commands itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    tool_name: str
    description_vi: str
    command: str
    group: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    read_only: bool = True
    risk_level: str = "read_only"
    required_role: str = "viewer"
    supported_deployment_modes: tuple[str, ...] = ("docker", "podman", "cephadm", "none")
    supported_ceph_versions: tuple[str, ...] = ("reef", "squid", "tentacle", "unknown")
    max_output_items: int = 500
    timeout_seconds: float = 8.0
    cache_ttl_seconds: int = 5
    evidence_fields: tuple[str, ...] = ()
    redaction_policy: str = "redact_secrets_before_model"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["supported_deployment_modes"] = list(self.supported_deployment_modes)
        value["supported_ceph_versions"] = list(self.supported_ceph_versions)
        value["evidence_fields"] = list(self.evidence_fields)
        return value


_EMPTY_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def _spec(
    tool_name: str,
    description_vi: str,
    command: str,
    *,
    group: str,
    timeout_seconds: float,
    cache_ttl_seconds: int,
    evidence_fields: tuple[str, ...],
    max_output_items: int = 500,
) -> ToolSpec:
    return ToolSpec(
        tool_name=tool_name,
        description_vi=description_vi,
        command=command,
        group=group,
        input_schema=dict(_EMPTY_SCHEMA),
        output_schema={"type": ["object", "array"]},
        timeout_seconds=timeout_seconds,
        cache_ttl_seconds=cache_ttl_seconds,
        evidence_fields=evidence_fields,
        max_output_items=max_output_items,
    )


FIXED_READ_ONLY_TOOL_REGISTRY: dict[str, ToolSpec] = {
    "get_cluster_status": _spec(
        "get_cluster_status", "Trạng thái tổng quan cụm Ceph.", "ceph status",
        group="health_summary", timeout_seconds=8, cache_ttl_seconds=5,
        evidence_fields=("health", "osd", "pg", "mon"),
    ),
    "get_osd_stat": _spec(
        "get_osd_stat", "Số lượng OSD theo trạng thái.", "ceph osd stat",
        group="inventory_summary", timeout_seconds=8, cache_ttl_seconds=5,
        evidence_fields=("num_osds", "num_up_osds", "num_in_osds"),
    ),
    "get_osd_tree": _spec(
        "get_osd_tree", "Cây CRUSH và trạng thái host/OSD.", "ceph osd tree",
        group="resource_detail", timeout_seconds=12, cache_ttl_seconds=15,
        evidence_fields=("nodes", "status", "weight", "utilization"),
    ),
    "get_pool_list": _spec(
        "get_pool_list", "Danh sách pool và thông số chi tiết.", "ceph osd pool ls detail",
        group="resource_detail", timeout_seconds=12, cache_ttl_seconds=15,
        evidence_fields=("pool_name", "size", "pg_num", "application"),
    ),
    "get_pg_stat": _spec(
        "get_pg_stat", "Trạng thái Placement Groups.", "ceph pg stat",
        group="health_summary", timeout_seconds=8, cache_ttl_seconds=5,
        evidence_fields=("pgs", "degraded", "undersized", "inactive"),
    ),
    "get_df": _spec(
        "get_df", "Dung lượng tổng, đã dùng và còn trống.", "ceph df",
        group="inventory_summary", timeout_seconds=8, cache_ttl_seconds=10,
        evidence_fields=("total_bytes", "used_bytes", "available_bytes", "pools"),
    ),
    "get_health_detail": _spec(
        "get_health_detail", "Chi tiết cảnh báo HEALTH_WARN/HEALTH_ERR.", "ceph health detail",
        group="resource_detail", timeout_seconds=10, cache_ttl_seconds=5,
        evidence_fields=("checks", "severity", "summary"), max_output_items=200,
    ),
    "get_mon_stat": _spec(
        "get_mon_stat", "Trạng thái MON và quorum.", "ceph mon stat",
        group="health_summary", timeout_seconds=8, cache_ttl_seconds=5,
        evidence_fields=("mons", "quorum", "election_epoch"),
    ),
}


def get_tool_spec(tool_name: str) -> ToolSpec:
    try:
        return FIXED_READ_ONLY_TOOL_REGISTRY[tool_name]
    except KeyError as exc:
        raise ValueError(f"unknown natural-language tool: {tool_name!r}") from exc

