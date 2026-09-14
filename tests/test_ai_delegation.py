import asyncio
from datetime import datetime, timedelta

import pytest
import dashboard.chat_client as chat_client
import dashboard.routes.chat as chat_routes
import shared.ai_delegation as delegation
from sqlalchemy.orm import sessionmaker
from shared import db as db_module
from shared.models import ChatMessage, Cluster, DelegatedAISubtask, DelegatedAITask


def test_provider_budget_keeps_one_slot_for_aggregate():
    budget = delegation._ProviderCallBudget(2)

    asyncio.run(budget.reserve())
    with pytest.raises(delegation.DelegatedProviderCallBudgetError):
        asyncio.run(budget.reserve())
    asyncio.run(budget.reserve_aggregate())


def test_build_plan_supports_non_ceph_delegated_request():
    plan = delegation.build_plan("Viết email xin nghỉ phép 3 ngày")

    assert [item["role"] for item in plan] == ["general_analysis"]
    assert plan[0]["tools"] == []


def test_dispatch_recovery_marks_exhausted_running_subtask_failed(db_session, monkeypatch):
    cluster = Cluster(
        name="test",
        ceph_mon_nodes="127.0.0.1",
        ssh_user="root",
        ssh_key_path="/tmp/test-key",
    )
    db_session.add(cluster)
    db_session.flush()
    message = ChatMessage(
        session_id="recovery-session",
        cluster_id=cluster.id,
        role="assistant",
        content="pending",
        actor="admin",
    )
    db_session.add(message)
    db_session.flush()
    task = DelegatedAITask(
        actor="admin",
        cluster_id=cluster.id,
        session_id="recovery-session",
        assistant_message_id=message.id,
        prompt="kiểm tra Ceph",
        status="RUNNING",
        execution_owner="dead-worker",
        lease_until=datetime.utcnow() - timedelta(minutes=1),
    )
    db_session.add(task)
    db_session.flush()
    subtask = DelegatedAISubtask(
        task_id=task.id,
        role="cluster_health",
        objective="health",
        allowed_tools_json="[]",
        status="RUNNING",
        execution_owner="dead-worker",
        lease_until=datetime.utcnow() - timedelta(minutes=1),
        attempt=delegation.MAX_SUBTASK_ATTEMPTS,
    )
    db_session.add(subtask)
    db_session.commit()
    monkeypatch.setattr(
        delegation.db,
        "SessionLocal",
        sessionmaker(bind=db_session.get_bind(), autoflush=False),
    )

    assert delegation.claim_tasks_for_dispatch() == [task.id]
    with delegation.db.SessionLocal() as session:
        recovered = session.get(DelegatedAISubtask, subtask.id)
        assert recovered.status == "FAILED"
        assert recovered.attempt == delegation.MAX_SUBTASK_ATTEMPTS


def test_delegated_turn_prefers_claude_to_avoid_shared_codex_lock(monkeypatch):
    monkeypatch.setattr(chat_client.settings, "codex_chat_enabled", True)
    monkeypatch.setattr(chat_client.settings, "claude_chat_enabled", True)
    monkeypatch.setattr(chat_client.settings, "router_api_key", "")
    monkeypatch.setattr(chat_client.auth, "is_ceph_chat_restricted", lambda _actor: False)
    monkeypatch.setattr(chat_client.auth, "chat_ai_name", lambda _actor: "AI")
    monkeypatch.setattr(chat_client.auth, "chat_female_address", lambda _actor: "Em là")
    calls = []

    async def fake_claude(*_args, **_kwargs):
        calls.append("claude")
        return {"reply_text": "kết quả", "proposal": None, "tools_used": []}

    async def unexpected_codex(*_args, **_kwargs):
        calls.append("codex")
        raise AssertionError("delegated turn phải thử Claude trước")

    monkeypatch.setattr(chat_client, "_run_claude_chat_turn", fake_claude)
    monkeypatch.setattr(chat_client, "_run_codex_chat_turn", unexpected_codex)

    result = asyncio.run(
        chat_client.run_chat_turn(
            [],
            "kiểm tra Ceph",
            "admin",
            allowed_tools=["get_health_detail"],
            max_tool_iterations=1,
            timeout_seconds=1,
            max_tokens=256,
            preferred_provider="claude",
        )
    )

    assert calls == ["claude"]
    assert result["reply_text"].endswith("kết quả")


def test_delegated_codex_fallback_uses_an_isolated_app_server(monkeypatch):
    monkeypatch.setattr(chat_client.settings, "codex_chat_enabled", True)
    monkeypatch.setattr(chat_client.settings, "claude_chat_enabled", False)
    monkeypatch.setattr(chat_client.settings, "router_api_key", "")
    monkeypatch.setattr(chat_client.auth, "is_ceph_chat_restricted", lambda _actor: False)
    monkeypatch.setattr(chat_client.auth, "chat_ai_name", lambda _actor: "AI")
    monkeypatch.setattr(chat_client.auth, "chat_female_address", lambda _actor: "Em là")

    class FakeCodexServer:
        closed = False

        async def close(self):
            self.closed = True

    isolated_server = FakeCodexServer()
    monkeypatch.setattr(chat_client, "CodexAppServer", lambda: isolated_server)
    seen = []

    async def fake_codex(*_args, **kwargs):
        seen.append(kwargs["app_server"])
        return {"reply_text": "fallback", "proposal": None, "tools_used": []}

    monkeypatch.setattr(chat_client, "_run_codex_chat_turn", fake_codex)
    result = asyncio.run(
        chat_client.run_chat_turn([], "kiểm tra Ceph", "admin", preferred_provider="claude")
    )

    assert seen == [isolated_server]
    assert isolated_server.closed is True
    assert result["reply_text"].endswith("fallback")


def test_claim_subtask_does_not_exceed_attempt_budget(db_session, monkeypatch):
    cluster = Cluster(
        name="test",
        ceph_mon_nodes="127.0.0.1",
        ssh_user="root",
        ssh_key_path="/tmp/test-key",
    )
    db_session.add(cluster)
    db_session.flush()
    message = ChatMessage(
        session_id="delegated-session",
        cluster_id=cluster.id,
        role="assistant",
        content="pending",
        actor="admin",
    )
    db_session.add(message)
    db_session.flush()
    task = DelegatedAITask(
        actor="admin",
        cluster_id=cluster.id,
        session_id="delegated-session",
        assistant_message_id=message.id,
        prompt="kiểm tra Ceph",
        status="RUNNING",
        execution_owner="owner",
        lease_until=datetime.utcnow() + timedelta(minutes=5),
    )
    db_session.add(task)
    db_session.flush()
    subtask = DelegatedAISubtask(
        task_id=task.id,
        role="cluster_health",
        objective="health",
        allowed_tools_json="[]",
        status="FAILED",
        attempt=delegation.MAX_SUBTASK_ATTEMPTS,
    )
    db_session.add(subtask)
    db_session.commit()
    monkeypatch.setattr(
        delegation.db,
        "SessionLocal",
        sessionmaker(bind=db_session.get_bind(), autoflush=False),
    )

    assert delegation._claim_subtask(task.id, subtask.id, cluster.id, "owner") is None


def test_execute_retries_failed_subtask_then_aggregates(db_session, monkeypatch):
    cluster = Cluster(
        name="test",
        ceph_mon_nodes="127.0.0.1",
        ssh_user="root",
        ssh_key_path="/tmp/test-key",
    )
    db_session.add(cluster)
    db_session.flush()
    message = ChatMessage(
        session_id="delegated-session",
        cluster_id=cluster.id,
        role="assistant",
        content="pending",
        actor="admin",
    )
    db_session.add(message)
    db_session.flush()
    task = DelegatedAITask(
        actor="admin",
        cluster_id=cluster.id,
        session_id="delegated-session",
        assistant_message_id=message.id,
        prompt="kiểm tra Ceph",
        status="PLANNING",
        execution_owner="owner",
        lease_until=datetime.utcnow() + timedelta(minutes=5),
    )
    db_session.add(task)
    db_session.flush()
    subtask = DelegatedAISubtask(
        task_id=task.id,
        role="cluster_health",
        objective="health",
        allowed_tools_json="[]",
        status="QUEUED",
    )
    db_session.add(subtask)
    db_session.commit()
    monkeypatch.setattr(
        delegation.db,
        "SessionLocal",
        sessionmaker(bind=db_session.get_bind(), autoflush=False),
    )
    monkeypatch.setattr(delegation, "_make_subtasks", lambda *_args: [subtask.id])
    monkeypatch.setattr(delegation, "SUBTASK_RETRY_BACKOFF_SECONDS", 0)
    attempts = []

    async def fake_run_subtask(subtask_id, *_args):
        attempts.append(len(attempts) + 1)
        with delegation.db.SessionLocal() as session:
            row = session.get(DelegatedAISubtask, subtask_id)
            row.attempt += 1
            row.status = "FAILED" if len(attempts) == 1 else "COMPLETED"
            row.error = "temporary provider error" if len(attempts) == 1 else None
            row.finished_at = datetime.utcnow()
            session.commit()

    async def fake_aggregate(*_args):
        return "aggregated result"

    monkeypatch.setattr(delegation, "_run_subtask", fake_run_subtask)
    monkeypatch.setattr(delegation, "_aggregate", fake_aggregate)

    asyncio.run(
        delegation._execute_claimed_task(
            task.id, task.prompt, task.actor, cluster.id, "owner"
        )
    )

    assert attempts == [1, 2]
    with delegation.db.SessionLocal() as session:
        assert session.get(DelegatedAITask, task.id).status == "COMPLETED"
        result = session.get(DelegatedAISubtask, subtask.id)
        assert result.status == "COMPLETED"
        assert result.attempt == 2


def test_delegate_route_persists_task_and_enqueues_it(dashboard_client, monkeypatch):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    enqueued = []

    async def fake_enqueue(task_id):
        enqueued.append(task_id)

    monkeypatch.setattr(chat_routes, "enqueue_task", fake_enqueue)
    response = dashboard_client.post(
        "/api/chat/messages",
        json={"session_id": "delegated-session", "content": "Kiểm tra sức khỏe Ceph", "mode": "delegate"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "delegate"
    assert payload["processing"] is True
    assert payload["task_id"] in enqueued
    with db_module.SessionLocal() as session:
        task = session.get(DelegatedAITask, payload["task_id"])
        assert task is not None
        assert task.status == "QUEUED"
