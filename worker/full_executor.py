"""Privileged, authenticated executor for Telegram Single Full only."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from dashboard.dual_ai_chat import (
    DualAIChatBusy,
    DualAIChatError,
    DualAIChatExhausted,
    run_single_full_access_chat,
)
from dashboard.telegram_chat import _is_direct_data_destruction
from shared import db, service_health, telegram_federation
from shared.full_executor_auth import executor_token
from shared.models import Cluster
from shared.single_full_scope import normalize_scope, verify_scope

_runs: dict[str, asyncio.Task] = {}
_runs_lock = asyncio.Lock()

_CLUSTER_SCOPE_FIELDS = (
    "name", "ceph_mon_nodes", "ceph_mon_hostnames", "ceph_mgr_nodes",
    "ceph_osd_nodes", "ceph_rgw_nodes", "ceph_exec_mode",
    "ceph_container_name", "ceph_osd_container_name", "ceph_rgw_container_name",
    "ssh_user", "ssh_key_path", "ceph_keyring_path",
)


class FullRunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    history: list[dict] = Field(default_factory=list, max_length=20)
    # The executor is intentionally fail-closed: an unrestricted run without
    # an explicit cluster scope must never fall back to the host's/global
    # Ceph configuration.
    cluster_context: dict[str, str] = Field(min_length=1, max_length=20)
    scope_signature: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("history")
    @classmethod
    def validate_history(cls, value: list[dict]) -> list[dict]:
        total_chars = 0
        for item in value:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                raise ValueError("history chỉ nhận message user/assistant")
            content = item.get("content")
            if not isinstance(content, str) or len(content) > 4000:
                raise ValueError("mỗi message history tối đa 4000 ký tự")
            total_chars += len(content)
        if total_chars > 24000:
            raise ValueError("tổng history tối đa 24000 ký tự")
        return value

    @field_validator("cluster_context")
    @classmethod
    def validate_cluster_context(cls, value: dict[str, str]) -> dict[str, str]:
        try:
            return normalize_scope(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


def _authorize(authorization: str | None) -> str:
    expected = executor_token()
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="unauthorized")
    return expected


def _scope_matches_database(scope: dict[str, str]) -> bool:
    """Reconcile the signed scope with the selected source's current DB row."""
    source = next(
        (
            item for item in telegram_federation.database_sources()
            if item.key == scope.get("database_source") and item.url == scope.get("database_url")
        ),
        None,
    )
    if source is None:
        return False
    try:
        with db.use_database(source.url):
            with db.SessionLocal() as session:
                cluster = session.get(Cluster, scope.get("cluster_id"))
                if cluster is None or not cluster.is_active:
                    return False
                if scope.get("cluster_ref") != f"{source.key}:{cluster.id}":
                    return False
                return all(
                    str(getattr(cluster, field, "") or "") == str(scope.get(field, "") or "")
                    for field in _CLUSTER_SCOPE_FIELDS
                )
    except Exception:
        return False


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
    auth_secret = _authorize(authorization)
    if not verify_scope(request.cluster_context, request.scope_signature, auth_secret):
        raise HTTPException(status_code=403, detail="cluster scope signature is invalid")
    if not await asyncio.to_thread(_scope_matches_database, request.cluster_context):
        raise HTTPException(status_code=403, detail="cluster scope does not match the selected database")
    if _is_direct_data_destruction(request.prompt):
        raise HTTPException(status_code=403, detail="direct destructive operations are blocked")
    async with _runs_lock:
        if run_id in _runs:
            raise HTTPException(status_code=409, detail="run already exists")
        task = asyncio.create_task(
            run_single_full_access_chat(
                request.prompt,
                request.history,
                cluster_context=request.cluster_context,
            )
        )
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
