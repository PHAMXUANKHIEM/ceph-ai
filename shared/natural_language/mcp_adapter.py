"""MCP-shaped, read-only adapter over the stable internal tool registry.

This is deliberately an adapter boundary, not a second executor. It uses the
server-selected cluster scope and the existing snapshot runner; mutation tools
are impossible because only ``FIXED_READ_ONLY_TOOL_REGISTRY`` is addressable.
The feature remains disabled until an operator explicitly enables it.
"""

from __future__ import annotations

from typing import Any

from .snapshot_runner import SnapshotQueryRunner
from .tool_registry import FIXED_READ_ONLY_TOOL_REGISTRY


class McpAdapterError(ValueError):
    pass


class ReadOnlyMcpAdapter:
    protocol_version = "mcp-readonly-v1"

    def __init__(self, *, runner=None, enabled: bool = False) -> None:
        self.runner = runner or SnapshotQueryRunner()
        self.enabled = bool(enabled)

    def call(self, request: dict[str, Any], *, server_cluster_id: str) -> dict[str, Any]:
        if not self.enabled:
            raise McpAdapterError("natural-language MCP adapter is disabled")
        if not isinstance(request, dict):
            raise McpAdapterError("MCP request must be an object")
        tool_name = str(request.get("name") or "").strip()
        if tool_name not in FIXED_READ_ONLY_TOOL_REGISTRY:
            raise McpAdapterError("MCP v1 only exposes registered read-only tools")
        cluster_id = str(server_cluster_id or "").strip()
        if not cluster_id:
            raise McpAdapterError("server cluster scope is required")
        arguments = request.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise McpAdapterError("MCP arguments must be an object")
        requested_cluster = str(arguments.get("cluster_id") or "").strip()
        if requested_cluster and requested_cluster != cluster_id:
            raise McpAdapterError("MCP cluster scope mismatch")
        safe_arguments = {key: value for key, value in arguments.items() if key != "cluster_id"}
        result = self.runner(tool_name, safe_arguments, cluster_id)
        return {
            "protocol_version": self.protocol_version,
            "tool_name": tool_name,
            "cluster_id": cluster_id,
            "read_only": True,
            "result": result,
        }
