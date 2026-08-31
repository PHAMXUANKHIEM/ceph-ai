import asyncio

from fastapi import HTTPException

from worker import full_executor


def test_finished_run_is_removed_even_when_callback_runs_later(monkeypatch):
    async def completed(_prompt, _history):
        return {"content": "ok"}

    monkeypatch.setattr(full_executor, "executor_token", lambda: "test-token")
    monkeypatch.setattr(full_executor, "run_single_full_access_chat", completed)

    async def scenario():
        result = await full_executor.run_full(
            "finished-run",
            full_executor.FullRunRequest(prompt="status"),
            "Bearer test-token",
        )
        assert result["event"]["content"] == "ok"
        await asyncio.sleep(0)
        assert "finished-run" not in full_executor._runs

    asyncio.run(scenario())


def test_cancel_waits_for_task_and_releases_run_state(monkeypatch):
    started = asyncio.Event()

    async def pending(_prompt, _history):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(full_executor, "executor_token", lambda: "test-token")
    monkeypatch.setattr(full_executor, "run_single_full_access_chat", pending)

    async def scenario():
        request_task = asyncio.create_task(full_executor.run_full(
            "cancelled-run",
            full_executor.FullRunRequest(prompt="status"),
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
