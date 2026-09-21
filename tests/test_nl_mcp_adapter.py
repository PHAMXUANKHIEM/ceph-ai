import pytest

from shared.natural_language.mcp_adapter import McpAdapterError, ReadOnlyMcpAdapter


class Runner:
    def __call__(self, tool_name, arguments, cluster_id):
        return {"tool": tool_name, "arguments": arguments, "cluster_id": cluster_id}


def test_mcp_adapter_uses_server_scope_and_only_registered_read_tools():
    result = ReadOnlyMcpAdapter(runner=Runner(), enabled=True).call(
        {"name": "get_cluster_status", "arguments": {}},
        server_cluster_id="cluster-a",
    )
    assert result["read_only"] is True
    assert result["cluster_id"] == "cluster-a"
    assert result["result"]["cluster_id"] == "cluster-a"


def test_mcp_adapter_rejects_scope_mismatch_mutation_and_disabled_mode():
    adapter = ReadOnlyMcpAdapter(runner=Runner(), enabled=True)
    with pytest.raises(McpAdapterError, match="scope mismatch"):
        adapter.call({"name": "get_cluster_status", "arguments": {"cluster_id": "other"}}, server_cluster_id="cluster-a")
    with pytest.raises(McpAdapterError, match="read-only"):
        adapter.call({"name": "propose_action", "arguments": {}}, server_cluster_id="cluster-a")
    with pytest.raises(McpAdapterError, match="disabled"):
        ReadOnlyMcpAdapter(runner=Runner()).call({"name": "get_cluster_status"}, server_cluster_id="cluster-a")
