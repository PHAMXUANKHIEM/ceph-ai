import asyncio

from fastapi import HTTPException
import pytest

from worker import full_executor
from shared.single_full_scope import sign_scope


CLUSTER_CONTEXT = {
    "cluster_id": "cluster-1",
    "cluster_ref": "local:cluster-1",
    "name": "CS-LAB",
    "database_source": "local",
    "database_url": "sqlite:///tmp/ceph-ai-test.db",
    "ceph_mon_nodes": "10.3.53.1",
}


def test_finished_run_is_removed_even_when_callback_runs_later(monkeypatch):
    async def completed(_prompt, _history, **kwargs):
        assert kwargs["cluster_context"]["name"] == "CS-LAB"
        return {"content": "ok"}

    monkeypatch.setattr(full_executor, "executor_token", lambda: "test-token")
    monkeypatch.setattr(full_executor, "_scope_matches_database", lambda _scope: True)
    monkeypatch.setattr(full_executor, "run_single_full_access_chat", completed)

    async def scenario():
        result = await full_executor.run_full(
            "finished-run",
            full_executor.FullRunRequest(
                prompt="status", cluster_context=CLUSTER_CONTEXT,
                scope_signature=sign_scope(CLUSTER_CONTEXT, "test-token"),
            ),
            "Bearer test-token",
        )
        assert result["event"]["content"] == "ok"
        await asyncio.sleep(0)
        assert "finished-run" not in full_executor._runs

    asyncio.run(scenario())


def test_cancel_waits_for_task_and_releases_run_state(monkeypatch):
    started = asyncio.Event()

    async def pending(_prompt, _history, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(full_executor, "executor_token", lambda: "test-token")
    monkeypatch.setattr(full_executor, "_scope_matches_database", lambda _scope: True)
    monkeypatch.setattr(full_executor, "run_single_full_access_chat", pending)

    async def scenario():
        request_task = asyncio.create_task(full_executor.run_full(
            "cancelled-run",
            full_executor.FullRunRequest(
                prompt="status", cluster_context=CLUSTER_CONTEXT,
                scope_signature=sign_scope(CLUSTER_CONTEXT, "test-token"),
            ),
            "Bearer test-token",
        ))
        await started.wait()
        result = await full_executor.cancel_full("cancelled-run", "Bearer test-token")
        assert result == {"cancelled": True}
        outcome = (await asyncio.gather(request_task, return_exceptions=True))[0]
        assert isinstance(outcome, HTTPException)
        assert outcome.status_code == 409
        await asyncio.sleep(0)
        assert "cancelled-run" not in full_executor._runs

    asyncio.run(scenario())


def test_invalid_scope_signature_is_rejected(monkeypatch):
    monkeypatch.setattr(full_executor, "executor_token", lambda: "test-token")
    request = full_executor.FullRunRequest(
        prompt="status",
        cluster_context=CLUSTER_CONTEXT,
        scope_signature="0" * 64,
    )

    async def scenario():
        with pytest.raises(HTTPException) as error:
            await full_executor.run_full("invalid-scope", request, "Bearer test-token")
        assert error.value.status_code == 403

    asyncio.run(scenario())


def test_scope_that_is_not_current_in_database_is_rejected(monkeypatch):
    monkeypatch.setattr(full_executor, "executor_token", lambda: "test-token")
    monkeypatch.setattr(full_executor, "_scope_matches_database", lambda _scope: False)
    request = full_executor.FullRunRequest(
        prompt="status",
        cluster_context=CLUSTER_CONTEXT,
        scope_signature=sign_scope(CLUSTER_CONTEXT, "test-token"),
    )

    async def scenario():
        with pytest.raises(HTTPException) as error:
            await full_executor.run_full("stale-scope", request, "Bearer test-token")
        assert error.value.status_code == 403

    asyncio.run(scenario())
