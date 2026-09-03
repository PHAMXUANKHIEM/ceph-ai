"""Privileged, authenticated executor for Telegram Single Full only."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from dashboard.dual_ai_chat import (
    DualAIChatBusy,
    DualAIChatError,
    DualAIChatExhausted,
    run_single_full_access_chat,
)
from dashboard.telegram_chat import _is_direct_data_destruction
from shared import service_health
from shared.full_executor_auth import executor_token

_runs: dict[str, asyncio.Task] = {}
_runs_lock = asyncio.Lock()


class FullRunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    history: list[dict] = Field(default_factory=list)


def _authorize(authorization: str | None) -> None:
    expected = executor_token()
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    if not executor_token():
        raise RuntimeError("SINGLE_FULL_EXECUTOR_TOKEN is required")
    async def heartbeat() -> None:
        while True:
            service_health.record_safe("full-executor")
            await asyncio.sleep(10)

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        async with _runs_lock:
            tasks = list(_runs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _discard_run(run_id: str, task: asyncio.Task) -> None:
    async with _runs_lock:
        if _runs.get(run_id) is task:
            _runs.pop(run_id, None)


def _remove_completed_run(run_id: str, task: asyncio.Task) -> None:
    """Always release run state, including when the HTTP client disconnects."""
    asyncio.create_task(_discard_run(run_id, task))


app = FastAPI(title="Ceph AI Single Full Executor", lifespan=_lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/runs/{run_id}")
async def run_full(run_id: str, request: FullRunRequest, authorization: str | None = Header(default=None)) -> dict:
    _authorize(authorization)
    if _is_direct_data_destruction(request.prompt):
        raise HTTPException(status_code=403, detail="direct destructive operations are blocked")
    async with _runs_lock:
        if run_id in _runs:
            raise HTTPException(status_code=409, detail="run already exists")
        task = asyncio.create_task(run_single_full_access_chat(request.prompt, request.history))
        _runs[run_id] = task
        task.add_done_callback(lambda finished, run_id=run_id: _remove_completed_run(run_id, finished))
    try:
        event = await asyncio.shield(task)
        return {"event": event}
    except DualAIChatExhausted as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "provider_quota_exhausted",
                "message": str(exc),
                "provider": exc.provider,
                "account_profile": exc.account_profile,
                "retryable": False,
            },
        ) from exc
    except DualAIChatBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "executor_busy", "message": str(exc), "retryable": True},
        ) from exc
    except DualAIChatError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "single_full_failed", "message": str(exc), "retryable": True},
        ) from exc
    except asyncio.CancelledError:
        # The shared task itself was cancelled (a concurrent DELETE), not
        # just this request; asyncio.shield does not protect against that.
        raise HTTPException(status_code=409, detail="run was cancelled") from None
    finally:
        if task.done():
            await _discard_run(run_id, task)


@app.delete("/v1/runs/{run_id}")
async def cancel_full(run_id: str, authorization: str | None = Header(default=None)) -> dict:
    _authorize(authorization)
    async with _runs_lock:
        task = _runs.get(run_id)
    if task is None:
        return {"cancelled": False}
    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        return {"cancelled": False, "stopping": True}
    return {"cancelled": True}
