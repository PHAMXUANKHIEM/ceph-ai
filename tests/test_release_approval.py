"""Telegram approval of candidate PRs: nothing merges or deploys without an operator tap."""

import asyncio
import json

import httpx
import pytest

from scripts.deploy import release_notifier as notifier
from shared import release_approval as ra

REPO = "org/ceph-ai"
HEAD = "a" * 40
MERGED = "m" * 40


class FakeGitHub:
    def __init__(self, *, head=HEAD, ref="cand/x", ci="success", merged=False):
        self.head, self.ref, self.ci, self.merged = head, ref, ci, merged
        self.calls = []

    def __call__(self, request):
        path, method = request.url.path, request.method
        self.calls.append((method, path, json.loads(request.content) if request.content else None))
        if path.endswith("/pulls/7") and method == "GET":
            return httpx.Response(200, json={"state": "open", "merged": self.merged, "mergeable": True,
                                             "title": "feat: x", "head": {"sha": self.head, "ref": self.ref}})
        if path.endswith("/actions/runs"):
            return httpx.Response(200, json={"workflow_runs": [
                {"path": ra.WORKFLOW_PATH, "status": "completed", "conclusion": self.ci}]})
        if path.endswith("/pulls/7/merge"):
            return httpx.Response(200, json={"sha": MERGED, "merged": True})
        if "/git/refs/heads/" in path:
            return httpx.Response(204)
        return httpx.Response(404)


def _approve(fake, tmp_path, short=HEAD[:12]):
    return ra.approve(REPO, "t", number=7, short_sha=short, actor="telegram-chat:1 (op)",
                      deploy_request_dir=str(tmp_path), client=httpx.Client(transport=httpx.MockTransport(fake)))


def test_an_approved_green_pr_is_merged_pinned_and_recorded(tmp_path):
    fake = FakeGitHub()

    record = _approve(fake, tmp_path)

    merge = next(body for method, path, body in fake.calls if path.endswith("/merge"))
    assert merge["sha"] == HEAD and merge["merge_method"] == "squash"
    assert record["merge_sha"] == MERGED
    stored = json.loads((tmp_path / "approved" / f"{MERGED}.json").read_text())
    assert stored["pr"] == 7 and stored["approved_by"] == "telegram-chat:1 (op)"


@pytest.mark.parametrize(("fake", "short", "reason"), [
    (FakeGitHub(head="b" * 40), HEAD[:12], "đã đổi"),
    (FakeGitHub(ci="failure"), HEAD[:12], "chưa xanh"),
    (FakeGitHub(ref="feature/x"), HEAD[:12], "cand/"),
    (FakeGitHub(merged=True), HEAD[:12], "đã được merge"),
])
def test_a_changed_red_foreign_or_merged_pr_is_not_merged(tmp_path, fake, short, reason):
    with pytest.raises(ra.ApprovalError, match=reason):
        _approve(fake, tmp_path, short)
    assert not any(path.endswith("/merge") for _m, path, _b in fake.calls)
    assert not (tmp_path / "approved").exists()


# --- Telegram button ------------------------------------------------------------------------

def _callback(data):
    return {"data": data, "from": {"id": 42, "username": "op"},
            "message": {"chat": {"id": -100}, "message_id": 5, "text": "🟢 PR #7 đã qua CI"}}


def test_only_operators_can_approve_from_telegram(monkeypatch):
    from dashboard import telegram_chat

    monkeypatch.setattr(telegram_chat, "is_allowed_callback", lambda *a: True)
    monkeypatch.setattr(telegram_chat, "_sender_can_use_full_access", lambda update: False)
    monkeypatch.setattr(ra, "approve", lambda *a, **k: pytest.fail("must not merge"))

    reply = asyncio.run(telegram_chat.handle_callback(_callback(f"relapprove:7:{HEAD[:12]}"), "bot"))

    assert "operator" in reply


def test_an_operator_tap_merges_and_marks_the_card(monkeypatch):
    from dashboard import telegram_chat

    edits, seen = [], {}
    monkeypatch.setattr(telegram_chat, "is_allowed_callback", lambda *a: True)
    monkeypatch.setattr(telegram_chat, "_sender_can_use_full_access", lambda update: True)
    monkeypatch.setattr(telegram_chat.ci_control, "read_token", lambda path: "t")
    monkeypatch.setattr(telegram_chat, "edit_telegram_message", lambda *args: edits.append(args))

    def approve(repo, token, **kwargs):
        seen.update(kwargs)
        return {"merge_sha": MERGED}

    monkeypatch.setattr(ra, "approve", approve)

    reply = asyncio.run(telegram_chat.handle_callback(_callback(f"relapprove:7:{HEAD[:12]}"), "bot"))

    assert reply == "Đã merge PR #7." and seen["number"] == 7 and seen["short_sha"] == HEAD[:12]
    assert "Đã duyệt bởi op" in edits[0][3]
    skipped = asyncio.run(telegram_chat.handle_callback(_callback(f"relskip:7:{HEAD[:12]}"), "bot"))
    assert skipped == "Đã bỏ qua PR #7."


# --- host notifier ------------------------------------------------------------------------------

@pytest.fixture
def host(monkeypatch, tmp_path):
    monkeypatch.setattr(notifier.runner, "REQUEST_DIR", tmp_path)
    monkeypatch.setattr(notifier.runner, "running_revision", lambda: "r" * 40)
    monkeypatch.setattr(notifier, "_containers_healthy", lambda: True)
    sent = []
    monkeypatch.setattr(notifier, "send_telegram", lambda env, text: sent.append(text))
    return tmp_path, sent


def _github(monkeypatch, *, main=(MERGED, "r" * 40), push_ci="success"):
    """``main``: commits newest first (the running revision "r"*40 last); ``push_ci``: one state or {sha: state}."""
    def get(path, token):
        if path.startswith("/commits?sha=main"):
            return [{"sha": sha} for sha in main]
        if path.startswith("/actions/runs?head_sha="):
            sha = path.split("head_sha=")[1].split("&")[0]
            state = push_ci.get(sha, "running") if isinstance(push_ci, dict) else push_ci
            if state == "running":
                return {"workflow_runs": [{"path": notifier.WORKFLOW_PATH, "event": "push", "status": "in_progress"}]}
            return {"workflow_runs": [{"path": notifier.WORKFLOW_PATH, "event": "push", "status": "completed",
                                       "conclusion": state}]}
        raise AssertionError(path)

    monkeypatch.setattr(notifier, "_get", get)


def _approved(directory, *shas):
    (directory / "approved").mkdir(exist_ok=True)
    for number, sha in enumerate(shas, 1):
        (directory / "approved" / f"{sha}.json").write_text(json.dumps({"pr": number, "approved_by": "op"}))


def test_main_is_deployed_only_when_its_head_was_approved_and_green(host, monkeypatch):
    directory, sent = host
    _github(monkeypatch)
    state = {}

    assert notifier.deploy_approved_head("t", {}, state) is None  # merged without approval: not deployed
    (directory / "approved").mkdir()
    (directory / "approved" / f"{MERGED}.json").write_text(json.dumps({"pr": 7, "approved_by": "op"}))

    assert notifier.deploy_approved_head("t", {}, state) == MERGED
    request = json.loads((directory / "pending.json").read_text())
    assert request["sha"] == MERGED and request["requested_by"] == "telegram:op"
    (directory / "pending.json").unlink()
    assert notifier.deploy_approved_head("t", {}, state) is None  # requested once only


def test_the_newest_green_approved_merge_deploys_while_newer_merges_are_still_in_ci(host, monkeypatch):
    """09/10/2026: six approved merges 3-20 minutes apart, CI ~41 minutes each; waiting for main's head to be
    green deployed nothing for 1h45."""
    directory, sent = host
    newest, middle, older = "c" * 40, "b" * 40, "a" * 40
    _approved(directory, older, middle, newest)
    _github(monkeypatch, main=(newest, middle, older, "r" * 40),
            push_ci={newest: "running", middle: "success", older: "success"})

    assert notifier.deploy_approved_head("t", {}, {}) == middle
    assert json.loads((directory / "pending.json").read_text())["sha"] == middle
    assert sent == [f"🚀 CI trên main xanh: deploy {middle[:8]} (PR #2, duyệt bởi op)."]


def test_nothing_older_than_the_running_revision_is_deployed(host, monkeypatch):
    directory, _sent = host
    newer, older = "c" * 40, "a" * 40
    _approved(directory, older, newer)
    _github(monkeypatch, main=(newer, "r" * 40, older), push_ci={newer: "running", older: "success"})

    assert notifier.deploy_approved_head("t", {}, {}) is None and not (directory / "pending.json").exists()


def test_a_red_main_ci_is_not_deployed(host, monkeypatch):
    directory, _sent = host
    _github(monkeypatch, push_ci="failure")
    (directory / "approved").mkdir()
    (directory / "approved" / f"{MERGED}.json").write_text(json.dumps({"pr": 7, "approved_by": "op"}))

    assert notifier.deploy_approved_head("t", {}, {}) is None and not (directory / "pending.json").exists()


def test_each_green_pr_is_announced_once_with_its_sensitive_files(monkeypatch):
    cards = []
    pull = {"number": 7, "title": "feat: x", "html_url": "https://pr/7", "body": "Tóm tắt",
            "head": {"sha": HEAD, "ref": "cand/x"}}

    def get(path, token):
        if path.startswith("/pulls?"):
            return [pull]
        if path.startswith("/actions/runs"):
            return {"workflow_runs": [{"path": notifier.WORKFLOW_PATH, "event": "pull_request",
                                       "status": "completed", "conclusion": "success"}]}
        if path.startswith("/pulls/7/files"):
            return [{"filename": "alembic/versions/x.py", "additions": 3, "deletions": 0},
                    {"filename": "shared/y.py", "additions": 1, "deletions": 1}]
        raise AssertionError(path)

    monkeypatch.setattr(notifier, "_get", get)
    monkeypatch.setattr(notifier, "send_approval_card", lambda env, text, number, head: cards.append(text) or True)
    state = {}

    assert notifier.announce_green_pull_requests("t", {}, state) == [7]
    assert notifier.announce_green_pull_requests("t", {}, state) == []  # same head: not again
    assert "⚠️ Đụng phần nhạy cảm: alembic/versions/x.py" in cards[0] and "2 file (+4/-1)" in cards[0]


def test_the_shared_gateway_routes_release_buttons_to_the_chat_handler():
    """08/10/2026: taps reached telegram_approval_bot, which logged them as unrecognized."""
    import ast
    import inspect

    from dashboard import telegram_approval_bot, telegram_chat

    assert "telegram_chat.CALLBACK_PREFIXES" in inspect.getsource(telegram_approval_bot._listen_loop_for_token)
    handled = set()
    for node in ast.walk(ast.parse(inspect.getsource(telegram_chat.handle_callback))):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "startswith":
            for arg in ast.walk(node.args[0]):
                if isinstance(arg, ast.Name) and arg.id.endswith("_PREFIX"):
                    handled.add(getattr(telegram_chat, arg.id))
                elif isinstance(arg, ast.Attribute) and arg.attr.endswith("_PREFIX"):
                    handled.add(getattr(ra, arg.attr))
    assert handled and handled <= set(telegram_chat.CALLBACK_PREFIXES)
    for data in (ra.callback_data(ra.APPROVE_PREFIX, 7, HEAD), ra.callback_data(ra.SKIP_PREFIX, 7, HEAD)):
        assert data.startswith(telegram_chat.CALLBACK_PREFIXES)
