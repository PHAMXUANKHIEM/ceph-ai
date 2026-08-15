"""In-process Ceph query tools for the Chat-with-AI feature
(dashboard/chat_client.py) — reuses watcher/ceph_client.py's existing SSH
infra (multi-MON fallback, per-exec-mode command wrapping) directly,
in-process, rather than a separate MCP server subprocess (the earlier
mcp_ceph_server.py/dashboard/mcp_client.py approach this replaces).

Two kinds of tools:
- FIXED_TOOL_COMMANDS: a closed set of read-only `ceph ...` subcommands,
  the same AD-5 "no free-form command string" posture the rest of this
  codebase already uses (DIAGNOSTIC_COMMANDS, action_id).
- run_ceph_command: a deliberate, explicitly-requested escape hatch for
  ANY read-only ceph command the AI generates, gated by a denylist (see
  _blocked_reason's docstring for why this is weaker than the fixed set
  above, by design, not an oversight).
"""

import re

from shared.cluster_nodes import resolve_ssh_creds
from watcher.ceph_client import (
    CephQueryError,
    query_rbd_trash,
    query_rbd_trash_with,
    run_ceph_json_command,
    run_ceph_json_command_with,
)

FIXED_TOOL_COMMANDS: dict[str, str] = {
    "get_cluster_status": "ceph status",
    "get_osd_stat": "ceph osd stat",
    "get_osd_tree": "ceph osd tree",
    "get_pool_list": "ceph osd pool ls detail",
    "get_pg_stat": "ceph pg stat",
    "get_df": "ceph df",
    "get_health_detail": "ceph health detail",
    "get_mon_stat": "ceph mon stat",
}

RUN_CEPH_COMMAND_TOOL = "run_ceph_command"
_LEGACY_RBD_TRASH_RE = re.compile(
    r"^ceph\s+rbd\s+trash\s+(?:ls|list)\s+(?:--pool\s+)?([A-Za-z0-9_.-]+)\s*$",
    re.IGNORECASE,
)

# Substring match, case-insensitive — deliberately over-inclusive (a false
# positive just makes a legitimate read-only command get rejected with a
# clear reason; a false negative lets something destructive through, which
# is the actually dangerous direction). "auth rm"/"osd purge"/"pool delete"/
# "config set"/"osd pool create"/"mon add" are listed separately from the
# single-word blocks below because they're otherwise-innocuous words in
# isolation ("rm" alone already blocks "auth rm" as a substring, and
# "create" alone already blocks "osd pool create"/"mon add", but both are
# kept explicit per spec for clarity) — "rm"/"delete"/"destroy"/"purge"/
# "create" alone already cover most of Ceph's real destructive/mutating
# vocabulary.
_BLOCKED_KEYWORDS = [
    "rm",
    "delete",
    "destroy",
    "purge",
    "create",
    "auth rm",
    "osd purge",
    "pool delete",
    "config set",
    "osd pool create",
    "mon add",
]
# Shell metacharacters that would let a single "run_ceph_command" call
# chain in a second, unvalidated command — ">"/"|"/";"/"&&"/"||" are the
# ones the operator explicitly listed; backtick and "$(" are the same class
# of risk (command substitution) and cost nothing extra to block too, since
# no legitimate read-only `ceph ...` invocation needs them.
_BLOCKED_CHARACTERS = [">", "|", ";", "&", "`", "$("]


def _blocked_reason(command: str) -> str | None:
    """Returns a human-readable reason `command` is refused, or None if
    it's allowed to run. Denylist-based, by explicit request — unlike
    FIXED_TOOL_COMMANDS' closed enum (a fixed set of known-safe, known-
    working subcommands), this can only ever block what it's told to block.
    A command this list doesn't anticipate (e.g. `ceph osd out 0`, `ceph
    tell mon.* injectargs ...`, `ceph mgr module disable ...` — all
    mutating, none matching a keyword below) gets through. This is a
    real, accepted gap, not a bug — the fixed tools above remain the
    safer default; this exists as a deliberate, narrower escape hatch.
    """
    normalized = command.strip()
    if not normalized.lower().startswith("ceph "):
        return "Lệnh phải bắt đầu bằng 'ceph '"
    lowered = normalized.lower()
    for keyword in _BLOCKED_KEYWORDS:
        if keyword in lowered:
            return f"Lệnh không được phép (chứa từ khoá bị chặn: {keyword!r})"
    for char in _BLOCKED_CHARACTERS:
        if char in normalized:
            return f"Lệnh không được phép (chứa ký tự bị chặn: {char!r})"
    return None


def _connection(cluster):
    nodes = [node.strip() for node in cluster.ceph_mon_nodes.split(",") if node.strip()]
    ssh_user, ssh_key_path, exec_mode, container_name = resolve_ssh_creds(cluster)
    return nodes, container_name, ssh_user, ssh_key_path, exec_mode


def _run(inner_command: str, cluster=None) -> dict | list:
    try:
        if cluster is None:
            _host, parsed = run_ceph_json_command(inner_command)
        else:
            _host, parsed = run_ceph_json_command_with(*_connection(cluster), inner_command)
        return parsed
    except CephQueryError as exc:
        return {"error": str(exc)}


def run_fixed_tool(tool_name: str, cluster=None) -> dict | list:
    """`tool_name` must be a FIXED_TOOL_COMMANDS key — raises ValueError
    otherwise, same fail-loud posture as watcher/ceph_client.py's own
    run_diagnostic_command for an unknown command_id."""
    if tool_name not in FIXED_TOOL_COMMANDS:
        raise ValueError(f"unknown fixed tool: {tool_name!r}")
    return _run(FIXED_TOOL_COMMANDS[tool_name], cluster)


def run_ceph_command_tool(command: str, cluster=None) -> dict:
    """Runs an arbitrary AI-generated `ceph ...` command after denylist
    validation. Returns {"blocked": True, "reason": ...} instead of running
    anything when refused — never raises for a blocked command, since a
    hostile/malformed command from the model is an expected, handleable
    outcome, not a bug."""
    # Some models incorrectly prefix the standalone RBD CLI with `ceph`.
    # Translate only this exact read-only query to the dedicated helper;
    # arbitrary RBD commands remain unavailable.
    legacy_rbd_match = _LEGACY_RBD_TRASH_RE.fullmatch(command.strip())
    if legacy_rbd_match:
        try:
            pool = legacy_rbd_match.group(1)
            return {"result": query_rbd_trash(pool) if cluster is None else query_rbd_trash_with(pool, *_connection(cluster))}
        except CephQueryError as exc:
            return {"error": str(exc)}

    reason = _blocked_reason(command)
    if reason is not None:
        return {"blocked": True, "reason": reason}
    result = _run(command.strip(), cluster)
    return result if isinstance(result, dict) else {"result": result}
