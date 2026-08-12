import json
import logging
import re

import httpx
from openai import AsyncOpenAI, APIError, APIConnectionError, AuthenticationError

from config.settings import settings
from shared.cluster_nodes import configured_nodes
from shared.codex_app_server import CodexAppServerError, codex_app_server
from shared.claude_cli import ClaudeCLIError, run_claude_prompt
from shared.router_client import RouterNotConfiguredError, build_router_client, readable_exception_message
from watcher.node_metrics import NodeMetricsError, collect_node_metrics
from watcher.ceph_client import CephQueryError, query_rbd_trash
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.llm.router_client import VALID_ACTION_IDS
from worker.policy.gate import VALID_MANAGEMENT_ACTION_IDS

from dashboard.ceph_tools import (
    FIXED_TOOL_COMMANDS,
    RUN_CEPH_COMMAND_TOOL,
    run_ceph_command_tool,
    run_fixed_tool,
)

logger = logging.getLogger(__name__)

MAX_TOKENS = 2048
ROUTER_TIMEOUT_SECONDS = 60.0
# Read-only tool round trips per user message — bounds worst-case latency of
# one chat turn (an investigate-then-answer loop rarely needs more than 2-3
# calls in practice); hitting this just ends the turn with a note, it never
# errors out.
MAX_TOOL_ITERATIONS = 6
# How many past ChatMessage rows (user+assistant combined) get replayed as
# context for a new turn — bounds token usage and this request's latency; a
# long-lived operational chat doesn't need its entire history re-sent on
# every message.
MAX_HISTORY_MESSAGES = 20

# A tool_result this large fed a well-behaved model into replying with a
# single token and finish_reason="stop" (NOT "length" — it wasn't cut off,
# it just gave up) — verified directly and reproducibly against a real
# 9router run: an unmodified `ceph osd dump` result (7267 chars) triggered
# this every time; capping to 4000 chars made the exact same request
# answer normally. Same truncate-for-a-consumer posture as
# DIAGNOSTIC_OUTPUT_MAX_CHARS elsewhere in this codebase, just tuned
# empirically against a real failure instead of being a round number.
MAX_TOOL_RESULT_CHARS = 4000

TOOL_LIST_NODES = "list_nodes"
TOOL_GET_NODE_METRICS = "get_node_metrics"
TOOL_PROPOSE_ACTION = "propose_action"
TOOL_GET_RBD_TRASH = "get_rbd_trash"
_POOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# Shown as the assistant's message content (never raised as a generic error)
# when the API AI connection isn't configured — dashboard/routes/chat.py
# checks for this exact string to decide whether to render the frontend's
# clickable "[Vào Cài đặt →]" link (dashboard/static/chat_widget.js), since
# the persisted ChatMessage.content itself stays plain text (no HTML/markup
# is ever put in a chat bubble — every bubble is rendered via textContent).
MISSING_AI_CONFIG_MESSAGE = "⚙️ Chưa kết nối AI. Vào Settings để kết nối API, Codex hoặc Claude."
OUT_OF_SCOPE_MESSAGE = "Tôi ko có quyền hạn thao tác trong lĩnh vực này. Xin liên hệ anh Khiêm để mở rộng"

# This is an enforcement boundary, not merely prompt guidance. Keep the
# vocabulary focused on Ceph itself and the infrastructure concepts that
# operators necessarily use while diagnosing a Ceph cluster.
_CEPH_SCOPE_RE = re.compile(
    r"(?i)(?:\bceph\b|\brados\b|\brbd\b|\brgw\b|\bcephfs\b|\bosd(?:\.\d+)?\b|"
    r"\bmon(?:itor)?\b|\bmgr\b|\bmds\b|\bcrush\b|\bbluestore\b|\brocksdb\b|"
    r"\bpool\b|\bplacement\s+group\b|\bpg(?:s|\.\w+)?\b|\bquorum\b|"
    r"\bscrub\b|\bbackfill\b|\brecover(?:y|ing)?\b|\brebalance\b|\bkeyring\b|"
    r"\bceph\.conf\b|\bcluster\b|\bstorage\b|\bvolume\b|\bsnapshot\b|"
    r"\biops\b|\bthroughput\b|\blatency\b|\bdaemons?\b|\bnodes?\b|\bhosts?\b|"
    r"cụm|lưu trữ|ổ đĩa|đĩa|dung lượng|độ trễ|lệch giờ|đồng bộ giờ|ntp|"
    r"sức khoẻ|sức khỏe|phục hồi dữ liệu)"
)
_FOLLOW_UP_RE = re.compile(
    r"(?i)^\s*(?:tại sao|vì sao|giải thích|chi tiết|tiếp tục|làm đi|thực hiện đi|"
    r"sửa đi|khắc phục đi|còn gì nữa|như thế nào|kiểm tra thêm|đúng không|ok|ừ|có)\b"
)


def is_ceph_scoped(user_text: str, history: list[dict] | None = None) -> bool:
    if _CEPH_SCOPE_RE.search(user_text or ""):
        return True
    if not _FOLLOW_UP_RE.search(user_text or ""):
        return False
    for message in reversed((history or [])[-MAX_HISTORY_MESSAGES:]):
        if message.get("role") == "user":
            return bool(_CEPH_SCOPE_RE.search(str(message.get("content", ""))))
    return False

# AD-5's "action_id/command_id đóng từ structured output, không parse free
# text" applies here exactly as it does to worker/llm/router_client.py's
# incident-diagnosis path — with ONE deliberate, explicit exception:
# run_ceph_command (dashboard/ceph_tools.py) is a denylist-gated escape
# hatch for an arbitrary read-only `ceph ...` command, requested and
# accepted as a real (if weaker-than-the-fixed-set) gap — see that module's
# _blocked_reason docstring. list_nodes/get_node_metrics are local
# (dashboard-owned: node inventory + /proc metrics, no `ceph` CLI
# involved). propose_action stages, never executes, one of the same closed
# action_id enum the Incident pipeline uses (VALID_ACTION_IDS) PLUS the
# separate management action_id enum (VALID_MANAGEMENT_ACTION_IDS, 2026-07-23
# — create/delete pool, resize/re-pg pool, mark OSD in/out/down) — its
# description is deliberately explicit that it does not execute anything,
# because a model that thinks it just fixed something will say so, which
# would be a lie the operator could act on. Management action_ids are all
# classified `safe` in action_policy.yaml (operator's explicit choice,
# including delete_pool — see that file's comment), so confirming one in
# the chat widget is a single click straight to Worker execution; the
# resolved command preview (with the real pool_name/osd_id baked in) is
# what the operator actually reviews before that click, not this prompt.
SYSTEM_PROMPT = (
    "Bạn là trợ lý AI quản trị cụm Ceph trong hệ thống CA Ceph AIOps. "
    "Bạn có thể gọi tool để lấy dữ liệu THỰC TẾ từ cụm Ceph đang chạy.\n\n"
    "Quy tắc:\n"
    f"- CHỈ trả lời hoặc thao tác nội dung liên quan Ceph. Nếu ngoài lĩnh vực Ceph, "
    f"chỉ trả đúng nguyên văn: {OUT_OF_SCOPE_MESSAGE}\n"
    "- Trả lời bằng tiếng Việt, ngắn gọn và chính xác\n"
    "- Khi được hỏi về thông tin cụm → GỌI TOOL, không tự đoán\n"
    "- Khi muốn thực hiện lệnh Ceph bất kỳ (read-only) → dùng run_ceph_command\n"
    "- Khi hỏi RBD trash → BẮT BUỘC dùng get_rbd_trash với tên pool; KHÔNG gọi "
    "run_ceph_command và KHÔNG tự dựng lệnh 'ceph rbd ...' (RBD là CLI riêng)\n"
    "- Trình bày số liệu rõ ràng với đơn vị (GB, TB, ops/s, ms, %)\n"
    "- Nếu SSH thất bại → thông báo lỗi và gợi ý kiểm tra kết nối\n"
    "- KHÔNG tự ý thực hiện lệnh xoá/sửa cấu hình cluster — mọi thay đổi PHẢI "
    "đi qua propose_action để operator tự xác nhận\n"
    "- Nếu được yêu cầu lệnh nguy hiểm ngoài danh mục action_id cố định (ví dụ "
    "sửa lệnh ceph trực tiếp) → giải thích và từ chối\n\n"
    "Ngoài các tool tra cứu (list_nodes, get_node_metrics, get_cluster_status, "
    "get_osd_stat, get_osd_tree, get_pool_list, get_pg_stat, get_df, "
    "get_health_detail, get_mon_stat, get_rbd_trash, run_ceph_command), bạn có thể gọi "
    "propose_action để ĐỀ XUẤT một hành động từ danh mục cố định — gồm cả "
    "hành động khắc phục sự cố (restart_osd_daemon, resync_ntp, "
    "pg_repair_force) VÀ hành động quản lý cluster: create_pool (cần "
    "pool_name, pg_num), delete_pool (cần pool_name — KHÔNG THỂ HOÀN TÁC, "
    "hỏi lại operator để chắc chắn đúng tên pool trước khi đề xuất), "
    "set_pool_size (cần pool_name, size), set_pool_pg_num (cần pool_name, "
    "pg_num), mark_osd_out/mark_osd_in/mark_osd_down (cần osd_id), "
    "enable_pool_application (cần pool_name, app_name — dùng để xoá cảnh "
    "báo POOL_APP_NOT_ENABLED, app_name thường là rbd/cephfs/rgw). "
    "propose_action KHÔNG thực thi gì cả — chỉ tạo đề xuất kèm lệnh sẽ chạy, "
    "hiển thị cho operator xem và tự bấm xác nhận. Bạn không bao giờ được tự "
    "nhận là đã chạy/khắc phục/tạo/xoá xong việc gì — bạn chỉ tra cứu và đề "
    "xuất. Chỉ gọi propose_action khi operator rõ ràng đang yêu cầu một hành "
    "động cụ thể, không phải cho câu hỏi thông tin thông thường, và tối đa "
    "một lần mỗi lượt. QUAN TRỌNG: nếu bạn nói với operator rằng bạn 'đã ghi "
    "nhận đề xuất', 'đã tạo đề xuất', hoặc yêu cầu họ 'xác nhận trong giao "
    "diện quản trị', thì bạn BẮT BUỘC phải thực sự gọi tool propose_action "
    "trong CHÍNH lượt trả lời đó — không được chỉ mô tả bằng lời rồi bỏ qua "
    "việc gọi tool, vì operator sẽ không thấy nút xác nhận nào cả và bị lừa "
    "rằng có một đề xuất đang chờ trong khi thực ra không có gì được tạo. "
    "Nếu chưa đủ thông tin để gọi propose_action (thiếu tham số, chưa rõ "
    "pool_name...), hãy hỏi lại operator trước, đừng nói là đã đề xuất."
)


class ChatToolError(Exception):
    """Raised for a tool call the model made with input this server refuses
    to run — caught in run_chat_turn and fed back to the model as a tool
    result error string, never allowed to propagate out and kill the whole
    chat turn over one malformed/hostile tool call."""


class ChatTurnError(Exception):
    """Raised when the router call itself fails (auth, network, timeout,
    truncated response) — the caller (dashboard/routes/chat.py) persists
    this as a plain assistant-role error message rather than a 500."""


def _tool_schemas() -> list[dict]:
    hosts = sorted(n["host"] for n in configured_nodes())
    action_ids = sorted(VALID_ACTION_IDS | VALID_MANAGEMENT_ACTION_IDS)

    def _fn(name: str, description: str, parameters: dict | None = None) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "strict": True,
                "parameters": parameters
                or {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            },
        }

    fixed_query_tools = [
        _fn("get_cluster_status", "Trạng thái tổng quan cụm Ceph (health, MON, OSD, PG)."),
        _fn("get_osd_stat", "Số lượng OSD theo trạng thái up/down/in/out."),
        _fn("get_osd_tree", "Cây CRUSH: host và OSD với trạng thái chi tiết."),
        _fn("get_pool_list", "Danh sách pool và thông số chi tiết."),
        _fn("get_pg_stat", "Trạng thái Placement Groups."),
        _fn("get_df", "Dung lượng cụm: tổng/đã dùng/còn trống."),
        _fn("get_health_detail", "Chi tiết cảnh báo HEALTH_WARN hoặc HEALTH_ERR."),
        _fn("get_mon_stat", "Trạng thái MON nodes và quorum."),
    ]
    return [
        _fn(
            TOOL_LIST_NODES,
            "List every node configured for this cluster, with its role(s) (MON/MGR/OSD/RGW).",
        ),
        _fn(
            TOOL_GET_NODE_METRICS,
            "Sample a node's CPU%, RAM%, and disk IOPS/latency over SSH (~1s sample window).",
            {
                "type": "object",
                "properties": {"host": {"type": "string", "enum": hosts}},
                "required": ["host"],
                "additionalProperties": False,
            },
        ),
        *fixed_query_tools,
        _fn(
            TOOL_GET_RBD_TRASH,
            "Liệt kê RBD images đang nằm trong trash của một pool. Đây là truy vấn read-only.",
            {
                "type": "object",
                "properties": {
                    "pool": {"type": "string", "description": "Tên RBD pool cần kiểm tra."}
                },
                "required": ["pool"],
                "additionalProperties": False,
            },
        ),
        _fn(
            RUN_CEPH_COMMAND_TOOL,
            "Chạy lệnh ceph CLI bất kỳ (read-only, đã qua kiểm tra an toàn). "
            "Lệnh phải bắt đầu bằng 'ceph ', ví dụ: ceph osd dump.",
            {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Lệnh ceph cần chạy, ví dụ: ceph osd dump",
                    }
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        ),
        _fn(
            TOOL_PROPOSE_ACTION,
            "Stage a remediation OR cluster-management action for the operator to "
            "review and explicitly confirm. Does not execute anything by itself. "
            "For management action_ids, target_nodes must contain exactly ONE host "
            "(any configured node — the command is cluster-wide, not per-host) and "
            "the matching parameter(s) must be set: create_pool needs pool_name+"
            "pg_num; delete_pool needs pool_name; set_pool_size needs pool_name+"
            "size; set_pool_pg_num needs pool_name+pg_num; mark_osd_out/"
            "mark_osd_in/mark_osd_down need osd_id; enable_pool_application needs "
            "pool_name+app_name (clears Ceph's POOL_APP_NOT_ENABLED warning — "
            "app_name is usually rbd/cephfs/rgw, but any name is accepted). "
            "Leave unused parameters null.",
            {
                "type": "object",
                "properties": {
                    "action_id": {"type": "string", "enum": action_ids},
                    "target_nodes": {
                        "type": "array",
                        "items": {"type": "string", "enum": hosts},
                        "minItems": 1,
                        "description": "Which node(s) this action targets.",
                    },
                    "rationale": {"type": "string", "description": "Why this action, in plain language."},
                    "pool_name": {
                        "type": ["string", "null"],
                        "description": "Pool name — required for create_pool/delete_pool/set_pool_size/set_pool_pg_num, else null.",
                    },
                    "pg_num": {
                        "type": ["integer", "null"],
                        "description": "Placement group count — required for create_pool/set_pool_pg_num, else null.",
                    },
                    "size": {
                        "type": ["integer", "null"],
                        "description": "Pool replication size — required for set_pool_size, else null.",
                    },
                    "osd_id": {
                        "type": ["integer", "null"],
                        "description": "Numeric OSD id — required for mark_osd_out/mark_osd_in/mark_osd_down, else null.",
                    },
                    "app_name": {
                        "type": ["string", "null"],
                        "description": "Pool application tag (rbd/cephfs/rgw/custom) — required for enable_pool_application, else null.",
                    },
                },
                "required": [
                    "action_id",
                    "target_nodes",
                    "rationale",
                    "pool_name",
                    "pg_num",
                    "size",
                    "osd_id",
                    "app_name",
                ],
                "additionalProperties": False,
            },
        ),
    ]


def _run_list_nodes() -> str:
    return json.dumps(configured_nodes())


def _run_get_node_metrics(args: dict) -> str:
    host = args.get("host")
    allowed_hosts = {n["host"] for n in configured_nodes()}
    if host not in allowed_hosts:
        raise ChatToolError(f"host {host!r} không nằm trong danh sách node đã cấu hình")
    try:
        metrics = collect_node_metrics(host)
    except NodeMetricsError as exc:
        raise ChatToolError(f"Không lấy được metrics từ {host}: {exc}") from exc
    return json.dumps(metrics)


def _run_get_rbd_trash(args: dict) -> str:
    pool = str(args.get("pool") or "").strip()
    if not _POOL_NAME_RE.fullmatch(pool):
        raise ChatToolError("pool RBD không hợp lệ")
    try:
        return json.dumps(query_rbd_trash(pool))
    except CephQueryError as exc:
        raise ChatToolError(f"Không đọc được RBD trash của pool {pool}: {exc}") from exc


def resolve_command_preview(
    action_id: str, target_nodes: list[str], params: dict | None = None
) -> str | None:
    """Best-effort — mirrors worker/llm/router_client.py's own use of
    get_command() for a preview (e.g. _route_risky_to_approval): some
    action_ids have no Command at all (pg_repair_force, investigate_manually
    — see worker/executor/commands.py's own comment on why), which is a
    normal outcome here, not an error to surface. For a management
    action_id, `params` is what makes this preview show the REAL pool_name/
    osd_id the operator is about to confirm — this string is exactly what
    the chat widget renders as "Lệnh sẽ chạy" before the confirm click."""
    try:
        return executor_commands.get_command(
            action_id, target_nodes[0] if target_nodes else None, params
        )
    except ExecutorError:
        return None


# Which propose_action parameters each management action_id requires, and
# their basic JSON type — presence/type only (a friendly, fast Vietnamese
# error for the common "model forgot a field" case). Range/format validation
# (pool name charset, pg_num/size/osd_id bounds) is NOT duplicated here — it
# lives once, authoritatively, in worker/executor/commands.py's builders,
# which this same proposal's command_preview already runs through below.
_MANAGEMENT_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "create_pool": ("pool_name", "pg_num"),
    "delete_pool": ("pool_name",),
    "set_pool_size": ("pool_name", "size"),
    "set_pool_pg_num": ("pool_name", "pg_num"),
    "mark_osd_out": ("osd_id",),
    "mark_osd_in": ("osd_id",),
    "mark_osd_down": ("osd_id",),
    "enable_pool_application": ("pool_name", "app_name"),
}
_MANAGEMENT_PARAM_IS_INT: dict[str, bool] = {
    "pool_name": False,
    "pg_num": True,
    "size": True,
    "osd_id": True,
    "app_name": False,
}


def _validate_proposal(args: dict) -> dict:
    action_id = (args.get("action_id") or "").strip()
    target_nodes = args.get("target_nodes")
    rationale = (args.get("rationale") or "").strip()
    allowed_hosts = {n["host"] for n in configured_nodes()}
    # Re-checked here even though the tool schema's `enum` already constrains
    # this — schema enums shape sampling, they are not a hard server-side
    # guarantee (a model can technically emit anything as tool input; only
    # code that actually validates before acting is a guarantee, same
    # posture as worker/llm/router_client.py's own action_id re-check).
    if action_id not in VALID_ACTION_IDS | VALID_MANAGEMENT_ACTION_IDS:
        raise ChatToolError(f"action_id {action_id!r} không hợp lệ")
    if not isinstance(target_nodes, list) or not target_nodes or not all(
        isinstance(h, str) and h in allowed_hosts for h in target_nodes
    ):
        raise ChatToolError("target_nodes phải là danh sách node đã cấu hình, không được rỗng")
    if not rationale:
        raise ChatToolError("rationale không được để trống")

    params: dict | None = None
    if action_id in VALID_MANAGEMENT_ACTION_IDS:
        # Cluster-wide command (create/delete pool, mark OSD in/out/down) —
        # not per-host like restart_osd_daemon/resync_ntp, so looping it
        # over multiple target_nodes would just run the exact same `ceph
        # ...` command redundantly (harmless for most, but delete_pool's
        # SECOND run fails on an already-gone pool and would misreport this
        # Action as FAILED) — require exactly one.
        if len(target_nodes) != 1:
            raise ChatToolError(
                f"action_id={action_id!r} là lệnh toàn cluster, target_nodes phải có đúng 1 node"
            )
        params = {}
        for key in _MANAGEMENT_REQUIRED_PARAMS[action_id]:
            value = args.get(key)
            if _MANAGEMENT_PARAM_IS_INT[key]:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ChatToolError(
                        f"tham số {key!r} phải là số nguyên cho action_id={action_id!r}"
                    )
            else:
                if not isinstance(value, str) or not value.strip():
                    raise ChatToolError(
                        f"tham số {key!r} không được để trống cho action_id={action_id!r}"
                    )
            params[key] = value

    return {
        "action_id": action_id,
        "target_nodes": target_nodes,
        "rationale": rationale,
        "params": params,
    }


def _get_client() -> AsyncOpenAI:
    return build_router_client(settings.router_api_key, settings.router_base_url)


def _run_tool(name: str, args: dict) -> tuple[str, bool]:
    """Returns (result_text, is_error). Never raises — ChatToolError and any
    unexpected exception are both turned into an error result string, same
    posture as the original MCP-based tool loop this replaces. Truncates to
    MAX_TOOL_RESULT_CHARS regardless of which tool ran — get_osd_tree/
    get_pool_list on a large cluster could plausibly hit the same wall
    run_ceph_command's raw `ceph osd dump` output did."""
    try:
        if name == TOOL_LIST_NODES:
            result_text, is_error = _run_list_nodes(), False
        elif name == TOOL_GET_NODE_METRICS:
            result_text, is_error = _run_get_node_metrics(args), False
        elif name == TOOL_GET_RBD_TRASH:
            result_text, is_error = _run_get_rbd_trash(args), False
        elif name in FIXED_TOOL_COMMANDS:
            result_text, is_error = json.dumps(run_fixed_tool(name)), False
        elif name == RUN_CEPH_COMMAND_TOOL:
            command = (args.get("command") or "").strip()
            result_text, is_error = json.dumps(run_ceph_command_tool(command)), False
        else:
            return f"unknown tool {name!r}", True
    except ChatToolError as exc:
        return str(exc), True
    except Exception:
        logger.exception("run_chat_turn: unexpected error running tool %s", name)
        return "internal error running this tool", True
    return result_text[:MAX_TOOL_RESULT_CHARS], is_error


async def run_chat_turn(history: list[dict], user_text: str, actor: str) -> dict:
    """Runs one chat turn: sends `user_text` (plus prior `history`) to
    9router (OpenAI-compatible /v1/chat/completions), executing any
    read-only tool calls it makes in-process (up to MAX_TOOL_ITERATIONS
    round trips) — reusing watcher/ceph_client.py's SSH infra directly
    (dashboard/ceph_tools.py), no separate MCP server subprocess — and
    stopping as soon as it either answers in plain text or calls
    propose_action.

    `history` must be plain `{"role": ..., "content": ...}` dicts, NOT
    ChatMessage ORM rows (see dashboard/routes/chat.py's extraction comment
    for why — a DetachedInstanceError trap from an earlier version of this
    function).

    Returns {"reply_text": str, "proposal": dict | None, "tools_used":
    list[str]}. `proposal`, when present, is re-validated by
    dashboard/routes/chat.py's confirm endpoint before it ever touches the
    DB. `tools_used` lists every tool actually invoked this turn (query
    tools only — a staged propose_action isn't a "query"), for the
    frontend's "🔧 Đã dùng: ..." badge.
    """
    if not is_ceph_scoped(user_text, history):
        return {"reply_text": OUT_OF_SCOPE_MESSAGE, "proposal": None, "tools_used": []}

    if settings.codex_chat_enabled:
        return await _run_codex_chat_turn(history, user_text)
    if settings.claude_chat_enabled:
        return await _run_claude_chat_turn(history, user_text)

    try:
        client = _get_client()
    except RouterNotConfiguredError as exc:
        raise ChatTurnError(str(exc)) from exc

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history[-MAX_HISTORY_MESSAGES:]:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": user_text})

    tools = _tool_schemas()
    reply_text_parts: list[str] = []
    proposal: dict | None = None
    tools_used: list[str] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        try:
            # 9router (verified live) always responds with an SSE stream
            # regardless of whether streaming was requested —
            # client.chat.completions.stream() + get_final_completion()
            # reassembles the exact same ChatCompletion shape a plain call
            # would return, and works unchanged against a real non-
            # streaming-only OpenAI-compatible endpoint too.
            async with client.chat.completions.stream(
                model=settings.router_model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=MAX_TOKENS,
                # httpx.Timeout(...), NOT a bare float — verified directly
                # against a real running 9router: passing a plain float
                # here silently truncated the streamed response (got back
                # "Hiện" instead of the full sentence, no error raised) on
                # a .stream() call specifically. httpx.Timeout(...) does
                # not have this problem.
                timeout=httpx.Timeout(ROUTER_TIMEOUT_SECONDS),
            ) as stream:
                completion = await stream.get_final_completion()
        except AuthenticationError as exc:
            raise ChatTurnError(f"Model {settings.router_model!r} hoặc API key không hợp lệ trên 9router: {readable_exception_message(exc)}") from exc
        except APIConnectionError as exc:
            raise ChatTurnError(
                f"Không thể kết nối 9router ({settings.router_base_url}). Kiểm tra host/port."
            ) from exc
        except APIError as exc:
            raise ChatTurnError(f"Model {settings.router_model!r} không khả dụng trên 9router: {readable_exception_message(exc)}") from exc
        except Exception as exc:
            raise ChatTurnError(f"Không thể kết nối 9router: {readable_exception_message(exc)}") from exc

        choice = completion.choices[0]
        msg = choice.message

        if msg.content:
            reply_text_parts.append(msg.content)

        if choice.finish_reason == "length":
            reply_text_parts.append(
                "[Phản hồi bị cắt do vượt giới hạn token — hỏi lại ngắn gọn hơn nếu cần.]"
            )
            break

        tool_calls = msg.tool_calls or []
        if not tool_calls:
            break  # plain text answer, turn is done

        messages.append(msg.model_dump(exclude_none=True))

        propose_call = next((c for c in tool_calls if c.function.name == TOOL_PROPOSE_ACTION), None)
        if propose_call is not None:
            try:
                args = json.loads(propose_call.function.arguments or "{}")
                proposal = _validate_proposal(args)
                proposal["command_preview"] = resolve_command_preview(
                    proposal["action_id"], proposal["target_nodes"], proposal["params"]
                )
            except (ChatToolError, TypeError, ValueError) as exc:
                reply_text_parts.append(f"[Đề xuất hành động không hợp lệ, đã bỏ qua: {exc}]")
                proposal = None
            # Never continue the tool loop after a proposal — it needs the
            # operator's explicit confirmation next, not more tool calls in
            # the same turn (and `messages` is never reused past this
            # function, so a tool_call left without its result here never
            # violates the API's pairing requirement on a future call).
            break

        for call in tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except (TypeError, ValueError):
                args = {}
            result_text, is_error = _run_tool(call.function.name, args)
            if not is_error:
                tools_used.append(call.function.name)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result_text}
            )
    else:
        reply_text_parts.append(
            "[Đã dừng sau nhiều bước tra cứu liên tiếp — hỏi lại cụ thể hơn nếu cần thêm thông tin.]"
        )

    reply_text = "\n\n".join(part for part in reply_text_parts if part).strip()
    if not reply_text:
        reply_text = "Đã ghi nhận đề xuất hành động bên dưới." if proposal is not None else "(không có phản hồi)"

    return {"reply_text": reply_text, "proposal": proposal, "tools_used": tools_used}


async def _run_codex_chat_turn(history: list[dict], user_text: str) -> dict:
    """Run chat through Codex while retaining ceph-ai's guarded tools."""
    transcript = []
    for message in history[-MAX_HISTORY_MESSAGES:]:
        role = "Người dùng" if message["role"] == "user" else "Trợ lý"
        transcript.append(f"{role}: {message['content']}")
    prompt = SYSTEM_PROMPT + "\n\nLịch sử hội thoại:\n" + "\n".join(transcript)
    prompt += f"\n\nNgười dùng: {user_text}\nTrợ lý:"
    proposal: dict | None = None
    tools_used: list[str] = []

    async def handle_tool(name: str, args: dict) -> tuple[str, bool]:
        nonlocal proposal
        if name == TOOL_PROPOSE_ACTION:
            try:
                proposal = _validate_proposal(args)
                proposal["command_preview"] = resolve_command_preview(
                    proposal["action_id"], proposal["target_nodes"], proposal["params"]
                )
                return "Đề xuất đã tạo và đang chờ operator xác nhận trên giao diện.", True
            except (ChatToolError, TypeError, ValueError) as exc:
                return f"Đề xuất không hợp lệ: {exc}", False
        text, is_error = _run_tool(name, args)
        if not is_error:
            tools_used.append(name)
        return text, not is_error

    try:
        result = await codex_app_server.run_turn(prompt, _tool_schemas(), handle_tool)
    except CodexAppServerError as exc:
        raise ChatTurnError(f"Codex: {exc}") from exc
    return {"reply_text": result["reply_text"], "proposal": proposal, "tools_used": tools_used}


async def _run_claude_chat_turn(history: list[dict], user_text: str) -> dict:
    """Run a guarded, text-only turn through the authenticated Claude CLI."""
    transcript = []
    for message in history[-MAX_HISTORY_MESSAGES:]:
        role = "Người dùng" if message["role"] == "user" else "Trợ lý"
        transcript.append(f"{role}: {message['content']}")
    prompt = (
        SYSTEM_PROMPT
        + "\n\nBạn đang chạy ở chế độ chỉ đọc và không có tool trong lượt Claude này. "
          "Không được tuyên bố đã kiểm tra hoặc thay đổi cụm nếu không có dữ liệu trong hội thoại. "
          "Không tạo đề xuất hành động có thể thực thi; hãy hướng dẫn operator xác minh khi cần."
        + "\n\nLịch sử hội thoại:\n" + "\n".join(transcript)
        + f"\n\nNgười dùng: {user_text}\nTrợ lý:"
    )
    try:
        reply = await run_claude_prompt(prompt, timeout=ROUTER_TIMEOUT_SECONDS)
    except ClaudeCLIError as exc:
        raise ChatTurnError(f"Claude: {exc}") from exc
    return {"reply_text": reply or "Claude không trả về nội dung", "proposal": None, "tools_used": []}
