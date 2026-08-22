import asyncio
import json

import httpx
import openai
import pytest

import dashboard.chat_client as chat_client

# Matches tests/conftest.py's TEST_CEPH_MON_NODES/TEST_CEPH_OSD_NODES —
# _pin_cluster_settings (autouse) is what makes configured_nodes() return
# these regardless of what a real .env says.
A_MON_HOST = "10.20.1.150"
AN_OSD_HOST = "10.20.1.83"
UNCONFIGURED_HOST = "9.9.9.9"


class _FakeFunctionCall:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, args: dict):
        self.id = call_id
        self.function = _FakeFunctionCall(name, json.dumps(args))


class _FakeMessage:
    """Mimics the openai SDK's ChatCompletionMessage just enough for
    run_chat_turn: `.content`, `.tool_calls`, and `.model_dump(exclude_none=
    True)` (appended verbatim to `messages` when the model made tool
    calls)."""

    def __init__(self, content: str | None = None, tool_calls: list | None = None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self, exclude_none: bool = False) -> dict:
        d = {
            "role": "assistant",
            "content": self.content,
            "tool_calls": (
                [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in self.tool_calls
                ]
                if self.tool_calls
                else None
            ),
        }
        if exclude_none:
            d = {k: v for k, v in d.items() if v is not None}
        return d


class _FakeChoice:
    def __init__(self, message: _FakeMessage, finish_reason: str = "stop"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeCompletion:
    def __init__(self, choices: list[_FakeChoice]):
        self.choices = choices


def _text_completion(text: str, finish_reason: str = "stop") -> _FakeCompletion:
    return _FakeCompletion([_FakeChoice(_FakeMessage(content=text), finish_reason=finish_reason)])


def _tool_call_completion(*calls: tuple[str, dict], content: str | None = None) -> _FakeCompletion:
    """`calls` is (name, args) pairs — builds a completion whose message has
    one tool_call per pair, matching a real turn where the model calls one
    or more tools instead of (or alongside) answering in text."""
    tool_calls = [_FakeToolCall(f"call_{i}", name, args) for i, (name, args) in enumerate(calls)]
    return _FakeCompletion(
        [_FakeChoice(_FakeMessage(content=content, tool_calls=tool_calls), finish_reason="tool_calls")]
    )


def _length_truncated_completion(text: str) -> _FakeCompletion:
    return _FakeCompletion([_FakeChoice(_FakeMessage(content=text), finish_reason="length")])


class _FakeStream:
    """Mimics `client.chat.completions.stream(...)`'s async context manager
    just enough for run_chat_turn: `async with ... as stream:` then `await
    stream.get_final_completion()`. A queued BaseException is raised from
    `__aenter__`, matching where a real connection/auth failure would
    surface."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        if isinstance(self._value, BaseException):
            raise self._value
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_final_completion(self):
        return self._value


def _install_fake_client(monkeypatch, responses):
    queue = list(responses)

    class FakeCompletions:
        def stream(self, **kwargs):
            assert queue, "run_chat_turn made more router calls than the test queued"
            return _FakeStream(queue.pop(0))

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(chat_client, "_get_client", lambda: FakeClient())


# --- _validate_proposal -----------------------------------------------------


def test_validate_proposal_accepts_known_action_id_and_configured_host():
    result = chat_client._validate_proposal(
        {"action_id": "resync_ntp", "target_nodes": [A_MON_HOST], "rationale": "clock drift"}
    )
    assert result == {
        "action_id": "resync_ntp",
        "target_nodes": [A_MON_HOST],
        "rationale": "clock drift",
        "params": None,
    }


def test_validate_proposal_rejects_unknown_action_id():
    with pytest.raises(chat_client.ChatToolError):
        chat_client._validate_proposal(
            {"action_id": "rm_dash_rf", "target_nodes": [A_MON_HOST], "rationale": "x"}
        )


def test_validate_proposal_rejects_unconfigured_host():
    with pytest.raises(chat_client.ChatToolError):
        chat_client._validate_proposal(
            {"action_id": "resync_ntp", "target_nodes": [UNCONFIGURED_HOST], "rationale": "x"}
        )


def test_validate_proposal_rejects_empty_target_nodes():
    with pytest.raises(chat_client.ChatToolError):
        chat_client._validate_proposal({"action_id": "resync_ntp", "target_nodes": [], "rationale": "x"})


def test_validate_proposal_rejects_blank_rationale():
    with pytest.raises(chat_client.ChatToolError):
        chat_client._validate_proposal(
            {"action_id": "resync_ntp", "target_nodes": [A_MON_HOST], "rationale": "  "}
        )


def test_validate_proposal_accepts_management_action_id_with_required_params():
    result = chat_client._validate_proposal(
        {
            "action_id": "create_pool",
            "target_nodes": [A_MON_HOST],
            "rationale": "operator requested a new pool",
            "pool_name": "my_pool",
            "pg_num": 32,
        }
    )
    assert result == {
        "action_id": "create_pool",
        "target_nodes": [A_MON_HOST],
        "rationale": "operator requested a new pool",
        "params": {"pool_name": "my_pool", "pg_num": 32},
    }


def test_validate_proposal_accepts_enable_pool_application_with_required_params():
    result = chat_client._validate_proposal(
        {
            "action_id": "enable_pool_application",
            "target_nodes": [A_MON_HOST],
            "rationale": "clear POOL_APP_NOT_ENABLED warning",
            "pool_name": "test",
            "app_name": "rbd",
        }
    )
    assert result["params"] == {"pool_name": "test", "app_name": "rbd"}


def test_validate_proposal_accepts_finalize_pacific_release_without_params():
    result = chat_client._validate_proposal(
        {
            "action_id": "finalize_pacific_osd_release",
            "target_nodes": [A_MON_HOST],
            "rationale": "all OSDs are Pacific or newer",
        }
    )
    assert result["params"] == {}


def test_validate_proposal_accepts_bluestore_quick_fix_with_osd_id():
    result = chat_client._validate_proposal(
        {
            "action_id": "bluestore_omap_quick_fix",
            "target_nodes": [AN_OSD_HOST],
            "rationale": "legacy omap stats on osd.3",
            "osd_id": 3,
        }
    )
    assert result["params"] == {"osd_id": 3}


def test_validate_proposal_rejects_bluestore_quick_fix_without_osd_id():
    with pytest.raises(chat_client.ChatToolError, match="osd_id"):
        chat_client._validate_proposal(
            {
                "action_id": "bluestore_omap_quick_fix",
                "target_nodes": [AN_OSD_HOST],
                "rationale": "legacy omap stats",
            }
        )


def test_resolve_command_preview_bakes_in_real_params_for_enable_pool_application():
    preview = chat_client.resolve_command_preview(
        "enable_pool_application", [A_MON_HOST], {"pool_name": "test", "app_name": "rbd"}
    )
    assert preview == "ceph osd pool application enable test rbd --yes-i-really-mean-it"


def test_validate_proposal_rejects_management_action_missing_required_param():
    with pytest.raises(chat_client.ChatToolError, match="pg_num"):
        chat_client._validate_proposal(
            {
                "action_id": "create_pool",
                "target_nodes": [A_MON_HOST],
                "rationale": "x",
                "pool_name": "my_pool",
            }
        )


def test_validate_proposal_rejects_management_action_wrong_param_type():
    with pytest.raises(chat_client.ChatToolError, match="pg_num"):
        chat_client._validate_proposal(
            {
                "action_id": "create_pool",
                "target_nodes": [A_MON_HOST],
                "rationale": "x",
                "pool_name": "my_pool",
                "pg_num": "32",
            }
        )


def test_validate_proposal_rejects_management_action_with_multiple_target_nodes():
    with pytest.raises(chat_client.ChatToolError, match="đúng 1 node"):
        chat_client._validate_proposal(
            {
                "action_id": "mark_osd_out",
                "target_nodes": [A_MON_HOST, AN_OSD_HOST],
                "rationale": "x",
                "osd_id": 3,
            }
        )


def test_validate_proposal_non_management_action_has_no_params():
    result = chat_client._validate_proposal(
        {"action_id": "restart_osd_daemon", "target_nodes": [A_MON_HOST], "rationale": "x"}
    )
    assert result["params"] is None


# --- resolve_command_preview -------------------------------------------------


def test_resolve_command_preview_returns_command_for_resync_ntp():
    preview = chat_client.resolve_command_preview("resync_ntp", [A_MON_HOST])
    assert preview is not None
    assert "chronyc" in preview or "ntpdate" in preview or "timedatectl" in preview


def test_resolve_command_preview_returns_none_for_action_with_no_command():
    # investigate_manually deliberately has no Command (worker/executor/commands.py)
    assert chat_client.resolve_command_preview("investigate_manually", [A_MON_HOST]) is None


def test_resolve_command_preview_bakes_in_real_params_for_management_action():
    preview = chat_client.resolve_command_preview(
        "create_pool", [A_MON_HOST], {"pool_name": "my_pool", "pg_num": 32}
    )
    assert preview == "ceph osd pool create my_pool 32"


def test_resolve_command_preview_returns_none_for_management_action_missing_params():
    assert chat_client.resolve_command_preview("delete_pool", [A_MON_HOST], None) is None


# --- run_chat_turn: plain text, no tools -------------------------------------


def test_run_chat_turn_rejects_non_ceph_question_without_calling_ai(monkeypatch):
    monkeypatch.setattr(chat_client.auth, "is_ceph_chat_restricted", lambda actor: True)
    monkeypatch.setattr(
        chat_client,
        "_get_client",
        lambda: pytest.fail("out-of-scope questions must not call the AI provider"),
    )

    result = asyncio.run(chat_client.run_chat_turn([], "Thời tiết hôm nay thế nào?", "admin"))

    assert result == {
        "reply_text": chat_client.with_romantic_address(chat_client.OUT_OF_SCOPE_MESSAGE, "AI"),
        "proposal": None,
        "tools_used": [],
    }


def test_run_chat_turn_unrestricted_user_can_ask_non_ceph_question(monkeypatch):
    monkeypatch.setattr(chat_client.auth, "is_ceph_chat_restricted", lambda actor: False)
    monkeypatch.setattr(chat_client.auth, "chat_ai_name", lambda actor: "Bé Mây")
    monkeypatch.setattr(
        chat_client.auth, "chat_female_address", lambda actor: "Anh yêu ơi, em là"
    )
    captured = {}

    class FakeCompletions:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream(_text_completion("Câu trả lời tự do"))

    class FakeClient:
        class FakeChat:
            completions = FakeCompletions()

        chat = FakeChat()

    monkeypatch.setattr(chat_client, "_get_client", lambda: FakeClient())

    result = asyncio.run(chat_client.run_chat_turn([], "Thời tiết hôm nay?", "admin"))

    assert result["reply_text"] == "Anh yêu ơi, em là Bé Mây. Câu trả lời tự do"
    prompt = captured["messages"][0]["content"]
    assert "Được trả lời cả câu hỏi ngoài lĩnh vực Ceph" in prompt
    assert "CHỈ trả lời hoặc thao tác nội dung liên quan Ceph" not in prompt
    assert "Tên của bạn là 'Bé Mây'" in prompt
    assert "'Anh yêu ơi, em là'" in prompt


def test_ceph_scope_allows_contextual_follow_up():
    history = [{"role": "user", "content": "Ceph cluster đang HEALTH_WARN"}]
    assert chat_client.is_ceph_scoped("giải thích chi tiết", history) is True
    assert chat_client.is_ceph_scoped("giải thích chi tiết", []) is False


def test_run_chat_turn_plain_text_answer(monkeypatch):
    _install_fake_client(monkeypatch, [_text_completion("Cluster đang HEALTH_OK.")])

    result = asyncio.run(chat_client.run_chat_turn([], "cluster có khoẻ không?", "admin"))

    assert result == {
        "reply_text": "Mình yêu ơi, em là AI. Cluster đang HEALTH_OK.",
        "proposal": None,
        "tools_used": [],
    }


def test_copilot_evidence_tools_are_exposed_to_all_authenticated_users():
    names = {item["function"]["name"] for item in chat_client._tool_schemas(is_admin=False)}
    assert {
        "get_recent_incidents", "get_incident_timeline", "get_capacity_forecast"
    } <= names


def test_copilot_server_appends_verified_citations(monkeypatch):
    from types import SimpleNamespace

    _install_fake_client(monkeypatch, [
        _tool_call_completion(("get_capacity_forecast", {})),
        _text_completion("Dung lượng chưa đủ lịch sử để dự báo."),
    ])
    observed = "2026-08-22T03:00:00+00:00"
    monkeypatch.setattr(chat_client, "configured_nodes", lambda cluster=None: [])
    monkeypatch.setattr(chat_client, "_run_tool", lambda *args, **kwargs: (
        json.dumps({"status": "insufficient_history", "_citations": [{
            "source_id": "capacity-series:cluster-1", "observed_at": observed,
            "confidence": 0.0,
        }]}), False,
    ))

    result = asyncio.run(chat_client.run_chat_turn(
        [], "dự báo dung lượng Ceph", "admin", SimpleNamespace(id="cluster-1", name="CS-LAB")
    ))

    assert "Nguồn đã kiểm chứng:" in result["reply_text"]
    assert "[capacity-series:cluster-1]" in result["reply_text"]
    assert observed in result["reply_text"]
    assert result["tools_used"] == ["get_capacity_forecast"]


def test_copilot_evidence_manifest_is_not_cut_at_raw_command_limit(monkeypatch):
    payload = json.dumps({
        "incidents": [{"summary": "x" * 300} for _ in range(15)],
        "_citations": [{"source_id": f"incident:{i}"} for i in range(15)],
    })
    monkeypatch.setattr(chat_client, "_run_recent_incidents", lambda *args: payload)

    result, is_error = chat_client._run_tool(
        "get_recent_incidents", {"hours": 24, "limit": 15}, cluster=object()
    )

    assert is_error is False
    assert len(result) > chat_client.MAX_TOOL_RESULT_CHARS
    assert len(chat_client._citations_from_result(result)) == 15


# --- run_chat_turn: local tools (list_nodes, get_node_metrics) --------------


def test_node_journal_tool_is_exposed_only_to_admin():
    admin_names = {item["function"]["name"] for item in chat_client._tool_schemas(is_admin=True)}
    user_names = {item["function"]["name"] for item in chat_client._tool_schemas(is_admin=False)}
    assert "get_node_journal" in admin_names
    assert "get_node_journal" not in user_names
    assert "propose_node_command" in admin_names
    assert "propose_node_command" not in user_names


def test_admin_can_read_mon_journal_on_configured_mon(monkeypatch):
    calls = []
    monkeypatch.setattr(chat_client.auth, "is_admin_user", lambda actor: actor == "admin")
    monkeypatch.setattr(
        chat_client,
        "run_command_on_node",
        lambda host, command: calls.append((host, command)) or "Aug 14 mon.a started",
    )

    result, is_error = chat_client._run_tool(
        "get_node_journal", {"host": A_MON_HOST, "service": "mon", "lines": 100}, "admin"
    )

    assert is_error is False
    assert json.loads(result)["lines"] == ["Aug 14 mon.a started"]
    assert calls == [(A_MON_HOST, "journalctl --no-pager --utc -n 100 -u 'ceph-mon@*' -u 'ceph-*@mon.*.service'")]


def test_non_admin_cannot_run_node_journal_even_if_called_directly(monkeypatch):
    ran = {"called": False}
    monkeypatch.setattr(chat_client.auth, "is_admin_user", lambda actor: False)
    monkeypatch.setattr(chat_client, "run_command_on_node", lambda *_: ran.update(called=True))

    result, is_error = chat_client._run_tool(
        "get_node_journal", {"host": A_MON_HOST, "service": "mon", "lines": 100}, "operator"
    )

    assert is_error is True
    assert "Chỉ tài khoản admin" in result
    assert ran["called"] is False


def test_claude_admin_can_call_node_journal_and_receive_result(monkeypatch):
    prompts = []
    replies = iter([
        json.dumps({
            "type": "tool",
            "name": "get_node_journal",
            "arguments": {"host": A_MON_HOST, "service": "mon", "lines": 100},
        }),
        json.dumps({"type": "final", "content": "MON đã khởi động lại lúc 10:15 UTC."}),
    ])

    async def fake_claude_prompt(prompt, *, timeout):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(chat_client, "run_claude_prompt", fake_claude_prompt)
    monkeypatch.setattr(chat_client.auth, "is_admin_user", lambda actor: actor == "admin")
    monkeypatch.setattr(chat_client, "run_command_on_node", lambda host, command: "10:15 mon started")

    result = asyncio.run(chat_client._run_claude_chat_turn(
        [], "Kiểm tra journal MON", chat_client.system_prompt(), "admin"
    ))

    assert result == {
        "reply_text": "MON đã khởi động lại lúc 10:15 UTC.",
        "proposal": None,
        "tools_used": ["get_node_journal"],
    }
    assert "10:15 mon started" in prompts[1]


def test_claude_non_admin_is_not_offered_node_journal(monkeypatch):
    captured = {}

    async def fake_claude_prompt(prompt, *, timeout):
        captured["prompt"] = prompt
        return json.dumps({"type": "final", "content": "Không có quyền đọc journal."})

    monkeypatch.setattr(chat_client, "run_claude_prompt", fake_claude_prompt)
    monkeypatch.setattr(chat_client.auth, "is_admin_user", lambda actor: False)

    result = asyncio.run(chat_client._run_claude_chat_turn(
        [], "Kiểm tra journal MON", chat_client.system_prompt(), "operator"
    ))

    assert result["tools_used"] == []
    assert '"name": "get_node_journal"' not in captured["prompt"]


def test_parse_claude_tool_envelope_accepts_fenced_json():
    assert chat_client._parse_claude_tool_envelope(
        '```json\n{"type":"final","content":"ok"}\n```'
    ) == {"type": "final", "content": "ok"}


def test_run_chat_turn_get_node_metrics_rejects_unconfigured_host(monkeypatch):
    ran = {"called": False}

    def _spy_collect_metrics(host):
        ran["called"] = True
        return {"cpu_percent": 0}

    monkeypatch.setattr(chat_client, "collect_node_metrics", _spy_collect_metrics)
    _install_fake_client(
        monkeypatch,
        [
            _tool_call_completion(("get_node_metrics", {"host": UNCONFIGURED_HOST})),
            _text_completion("Không truy vấn được node đó."),
        ],
    )

    result = asyncio.run(chat_client.run_chat_turn([], "xem tải node 9.9.9.9", "admin"))

    assert ran["called"] is False  # SSRF-via-SSH guard: never reaches the real SSH call
    assert result["proposal"] is None
    assert result["tools_used"] == []  # the failed call is not counted as "used"


def test_run_chat_turn_list_nodes_tracks_tools_used(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            _tool_call_completion(("list_nodes", {})),
            _text_completion("Cụm có 4 node đã cấu hình."),
        ],
    )

    result = asyncio.run(chat_client.run_chat_turn([], "cụm có bao nhiêu node?", "admin"))

    assert result["reply_text"] == "Mình yêu ơi, em là AI. Cụm có 4 node đã cấu hình."
    assert result["tools_used"] == ["list_nodes"]


# --- run_chat_turn: in-process Ceph cluster-query tools ---------------------
# (dashboard/ceph_tools.py, reusing watcher/ceph_client.py's SSH infra
# directly — no separate MCP server subprocess anymore)


def test_run_chat_turn_calls_fixed_ceph_tool_and_tracks_tools_used(monkeypatch):
    monkeypatch.setattr(
        chat_client, "run_fixed_tool", lambda name: [{"pool_name": "volumes"}]
    )
    _install_fake_client(
        monkeypatch,
        [
            _tool_call_completion(("get_pool_list", {})),
            _text_completion("Cụm có 1 pool: volumes."),
        ],
    )

    result = asyncio.run(chat_client.run_chat_turn([], "cụm có bao nhiêu pool?", "admin"))

    assert result["reply_text"] == "Mình yêu ơi, em là AI. Cụm có 1 pool: volumes."
    assert result["tools_used"] == ["get_pool_list"]


def test_run_chat_turn_run_ceph_command_tool_tracks_tools_used(monkeypatch):
    captured = []

    def fake_run_ceph_command_tool(command):
        captured.append(command)
        return {"result": "ok"}

    monkeypatch.setattr(chat_client, "run_ceph_command_tool", fake_run_ceph_command_tool)
    _install_fake_client(
        monkeypatch,
        [
            _tool_call_completion(("run_ceph_command", {"command": "ceph osd dump"})),
            _text_completion("Đây là kết quả ceph osd dump."),
        ],
    )

    result = asyncio.run(chat_client.run_chat_turn([], "show tôi ceph osd dump", "admin"))

    assert captured == ["ceph osd dump"]
    assert result["tools_used"] == ["run_ceph_command"]


def test_run_chat_turn_can_query_rbd_trash(monkeypatch):
    monkeypatch.setattr(
        chat_client,
        "query_rbd_trash",
        lambda pool: [{"id": "trash-1", "name": "old-volume", "deletion_time": "", "status": ""}],
    )
    _install_fake_client(
        monkeypatch,
        [
            _tool_call_completion(("get_rbd_trash", {"pool": "volumes"})),
            _text_completion("Pool volumes có 1 image trong trash: old-volume."),
        ],
    )

    result = asyncio.run(chat_client.run_chat_turn([], "kiểm tra RBD trash pool volumes", "admin"))

    assert result["reply_text"] == "Mình yêu ơi, em là AI. Pool volumes có 1 image trong trash: old-volume."
    assert result["tools_used"] == ["get_rbd_trash"]


def test_rbd_trash_tool_rejects_invalid_pool_without_querying(monkeypatch):
    monkeypatch.setattr(chat_client, "query_rbd_trash", lambda pool: (_ for _ in ()).throw(AssertionError()))

    result, is_error = chat_client._run_tool("get_rbd_trash", {"pool": "volumes; id"})

    assert is_error is True
    assert "không hợp lệ" in result


def test_prompt_requires_dedicated_rbd_trash_tool():
    assert "BẮT BUỘC dùng get_rbd_trash" in chat_client.SYSTEM_PROMPT


def test_run_chat_turn_blocked_ceph_command_is_not_counted_as_tools_used(monkeypatch):
    monkeypatch.setattr(
        chat_client,
        "run_ceph_command_tool",
        lambda command: {"blocked": True, "reason": "Lệnh không được phép"},
    )
    _install_fake_client(
        monkeypatch,
        [
            _tool_call_completion(("run_ceph_command", {"command": "ceph osd pool delete volumes"})),
            _text_completion("Tôi không thể thực hiện lệnh xóa pool."),
        ],
    )

    result = asyncio.run(chat_client.run_chat_turn([], "xoá pool volumes", "admin"))

    # run_ceph_command_tool returning a "blocked" dict is still a successful
    # (non-error) tool result as far as _run_tool is concerned — the model
    # itself decides to refuse, based on the {"blocked": true, ...} content
    # it reads back, not on tools_used bookkeeping.
    assert result["tools_used"] == ["run_ceph_command"]
    assert result["reply_text"] == "Mình yêu ơi, em là AI. Tôi không thể thực hiện lệnh xóa pool."


# --- run_chat_turn: propose_action -------------------------------------------


def test_run_chat_turn_stages_valid_proposal_with_command_preview(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            _tool_call_completion(
                (
                    "propose_action",
                    {
                        "action_id": "resync_ntp",
                        "target_nodes": [A_MON_HOST],
                        "rationale": "clock skew detected",
                    },
                ),
                content="MON node bị lệch giờ, đề xuất resync NTP.",
            )
        ],
    )

    result = asyncio.run(chat_client.run_chat_turn([], "sửa lỗi lệch giờ giúp tôi", "admin"))

    assert result["reply_text"] == "Mình yêu ơi, em là AI. MON node bị lệch giờ, đề xuất resync NTP."
    assert result["proposal"]["action_id"] == "resync_ntp"
    assert result["proposal"]["target_nodes"] == [A_MON_HOST]
    assert result["proposal"]["rationale"] == "clock skew detected"
    assert result["proposal"]["command_preview"]  # resolved, non-empty


def test_run_chat_turn_drops_proposal_with_invalid_host(monkeypatch):
    _install_fake_client(
        monkeypatch,
        [
            _tool_call_completion(
                (
                    "propose_action",
                    {"action_id": "resync_ntp", "target_nodes": [UNCONFIGURED_HOST], "rationale": "x"},
                )
            )
        ],
    )

    result = asyncio.run(chat_client.run_chat_turn([], "sửa Ceph giúp tôi", "admin"))

    assert result["proposal"] is None
    assert "không hợp lệ" in result["reply_text"]


# --- run_chat_turn: bounded tool-loop -----------------------------------------


def test_run_chat_turn_stops_after_max_iterations(monkeypatch):
    monkeypatch.setattr(chat_client, "MAX_TOOL_ITERATIONS", 3)
    responses = [_tool_call_completion(("list_nodes", {})) for _ in range(3)]
    _install_fake_client(monkeypatch, responses)

    result = asyncio.run(chat_client.run_chat_turn([], "keep listing nodes forever", "admin"))

    assert result["proposal"] is None
    assert "Đã dừng sau nhiều bước" in result["reply_text"]


def test_run_chat_turn_length_finish_reason_appends_truncation_note_and_stops(monkeypatch):
    _install_fake_client(monkeypatch, [_length_truncated_completion("Cluster đang")])

    result = asyncio.run(chat_client.run_chat_turn([], "giải thích chi tiết Ceph cluster", "admin"))

    assert "Cluster đang" in result["reply_text"]
    assert "cắt do vượt giới hạn token" in result["reply_text"]


# --- run_chat_turn: 9router call failure -------------------------------------


def test_run_chat_turn_wraps_api_errors(monkeypatch):
    _install_fake_client(monkeypatch, [RuntimeError("connection refused")])

    with pytest.raises(chat_client.ChatTurnError):
        asyncio.run(chat_client.run_chat_turn([], "Ceph status", "admin"))


def test_run_chat_turn_reports_invalid_api_key_with_friendly_message(monkeypatch):
    response = httpx.Response(401, request=httpx.Request("GET", "http://localhost:20128"))
    auth_error = openai.AuthenticationError("invalid API key", response=response, body=None)
    _install_fake_client(monkeypatch, [auth_error])

    with pytest.raises(chat_client.ChatTurnError, match="API key không hợp lệ"):
        asyncio.run(chat_client.run_chat_turn([], "Ceph status", "admin"))


def test_run_chat_turn_reports_connection_error_with_host_port_hint(monkeypatch):
    conn_error = openai.APIConnectionError(request=httpx.Request("GET", "http://localhost:20128"))
    _install_fake_client(monkeypatch, [conn_error])

    with pytest.raises(chat_client.ChatTurnError, match="Kiểm tra host/port"):
        asyncio.run(chat_client.run_chat_turn([], "Ceph status", "admin"))


def test_run_chat_turn_reports_missing_router_config_as_chat_turn_error_not_raw_exception(
    monkeypatch,
):
    # shared/router_client.py::build_router_client raises
    # RouterNotConfiguredError when settings.router_base_url is blank (no
    # direct-to-vendor fallback, by policy) — dashboard/routes/chat.py only
    # catches ChatTurnError around run_chat_turn(), so this must be
    # translated, not left to propagate as a raw exception the route would
    # 500 on.
    monkeypatch.setattr(chat_client.settings, "router_base_url", "", raising=False)

    with pytest.raises(chat_client.ChatTurnError):
        asyncio.run(chat_client.run_chat_turn([], "Ceph status", "admin"))
