"""Supervisor for delegated, isolated Ceph investigation agents.

This module deliberately uses separate chat-turn invocations rather than
replaying one growing transcript. The agents are read-only; write operations
remain in the existing Action/approval pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import and_, exists, or_, update

from config.settings import settings
from shared import db
from shared.models import ChatMessage, Cluster, DelegatedAISubtask, DelegatedAITask

logger = logging.getLogger(__name__)


def _cluster_snapshot(cluster) -> SimpleNamespace:
    return SimpleNamespace(**{column.name: getattr(cluster, column.name) for column in Cluster.__table__.columns})


MAX_SUBTASKS = 4
MAX_PROMPT_CHARS = 50_000
MAX_RESULT_CHARS = 12_000
# Keep crash recovery comfortably below the normal delegated task timeout.
# A redelivered RabbitMQ message is ACKed when another execution owner still
# holds this lease, so this value is also the upper bound before the watchdog
# can reclaim a task after a Worker crash.
# Keep it below the configured task timeout as well as below five minutes.
TASK_LEASE_SECONDS = max(30, min(300, settings.delegated_ai_task_timeout_seconds // 3))
SUBTASK_LEASE_SECONDS = 1_800
MAX_SUBTASK_ATTEMPTS = 2
SUBTASK_RETRY_BACKOFF_SECONDS = 1.0
DISPATCH_RETRY_SECONDS = 60
WORKER_INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"
_SUBTASK_SEMAPHORE = asyncio.Semaphore(settings.delegated_ai_max_parallel_subtasks)


class DelegatedProviderCallBudgetError(RuntimeError):
    """Raised before a provider request when the parent task is exhausted."""


class DelegatedTaskCancelled(Exception):
    """Internal signal used to stop provider work after operator cancellation."""


class _ProviderCallBudget:
    def __init__(self, limit: int, task_id: str | None = None, owner: str | None = None):
        self.limit = max(1, int(limit))
        self.remaining = self.limit
        self.task_id = task_id
        self.owner = owner
        self._lock = asyncio.Lock()

    async def _reserve(self, *, keep_slots: int) -> None:
        async with self._lock:
            if self.task_id is not None:
                call_limit = max(0, self.limit - keep_slots)
                # The DB counter is authoritative so a task reclaimed by a
                # fresh Worker cannot receive a new budget after a crash.
                with db.SessionLocal() as session:
                    result = session.execute(
                        update(DelegatedAITask)
                        .where(
                            DelegatedAITask.id == self.task_id,
                            DelegatedAITask.execution_owner == self.owner,
                            DelegatedAITask.status.in_(
                                ("PLANNING", "RUNNING", "AGGREGATING")
                            ),
                            DelegatedAITask.provider_call_count < call_limit,
                        )
                        .values(
                            provider_call_count=DelegatedAITask.provider_call_count + 1,
                            updated_at=_now(),
                        )
                    )
                    session.commit()
                if result.rowcount != 1:
                    raise DelegatedProviderCallBudgetError(
                        "Delegated task đã đạt giới hạn tổng số lượt gọi AI hoặc mất quyền sở hữu"
                    )
                self.remaining = max(0, self.remaining - 1)
                return
            if self.remaining <= keep_slots:
                raise DelegatedProviderCallBudgetError(
                    "Delegated task đã đạt giới hạn tổng số lượt gọi AI"
                )
            self.remaining -= 1

    async def reserve(self) -> None:
        """Reserve a sub-agent provider call while keeping one final slot."""
        await self._reserve(keep_slots=1)

    async def reserve_aggregate(self) -> None:
        """Reserve the final synthesis call."""
        await self._reserve(keep_slots=0)


def _now() -> datetime:
    return datetime.utcnow()


def _lease_deadline(seconds: int) -> datetime:
    return _now() + timedelta(seconds=seconds)


def _claim_task(task_id: str) -> tuple[str, str, str, str] | None:
    """Atomically claim a parent task, or return None if another worker owns it.

    QUEUED tasks are claimed once. PLANNING/RUNNING/AGGREGATING tasks are
    reclaimable only after their lease expires, which recovers work after a
    Worker crash without allowing two live Workers to execute the same task.
    """
    now = _now()
    with db.SessionLocal() as session:
        result = session.execute(
            update(DelegatedAITask)
            .where(
                DelegatedAITask.id == task_id,
                or_(
                    DelegatedAITask.status == "QUEUED",
                    and_(
                        DelegatedAITask.status.in_(("PLANNING", "RUNNING", "AGGREGATING")),
                        or_(DelegatedAITask.lease_until.is_(None), DelegatedAITask.lease_until < now),
                    ),
                ),
            )
            .values(
                status="PLANNING",
                execution_owner=WORKER_INSTANCE_ID,
                lease_until=_lease_deadline(TASK_LEASE_SECONDS),
                dispatch_claimed_at=None,
                # A reclaimed task gets a fresh execution start; the original
                # creation time remains available in created_at.
                started_at=now,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            return None
        task = session.get(DelegatedAITask, task_id)
        if task is None:
            session.rollback()
            return None
        claimed = (task.prompt, task.actor, task.cluster_id, WORKER_INSTANCE_ID)
        session.commit()
        return claimed


def _set_task_owned(
    task_id: str,
    owner: str,
    expected_status: str | tuple[str, ...] | None = None,
    **values,
) -> bool:
    with db.SessionLocal() as session:
        values["updated_at"] = _now()
        statement = update(DelegatedAITask).where(
            DelegatedAITask.id == task_id,
            DelegatedAITask.execution_owner == owner,
        )
        if expected_status is not None:
            if isinstance(expected_status, tuple):
                statement = statement.where(DelegatedAITask.status.in_(expected_status))
            else:
                statement = statement.where(DelegatedAITask.status == expected_status)
        result = session.execute(statement.values(**values))
        session.commit()
        return result.rowcount == 1


async def _renew_task_lease(task_id: str, owner: str) -> None:
    """Keep a healthy long-running parent task from being reclaimed."""
    interval = max(10, TASK_LEASE_SECONDS // 3)
    while True:
        await asyncio.sleep(interval)
        if not _set_task_owned(
            task_id,
            owner,
            lease_until=_lease_deadline(TASK_LEASE_SECONDS),
            expected_status=("PLANNING", "RUNNING", "AGGREGATING"),
        ):
            return


def _claim_subtask(task_id: str, subtask_id: str, cluster_id: str, owner: str):
    now = _now()
    with db.SessionLocal() as session:
        result = session.execute(
            update(DelegatedAISubtask)
            .where(
                DelegatedAISubtask.id == subtask_id,
                DelegatedAISubtask.task_id == task_id,
                exists().where(
                    DelegatedAITask.id == task_id,
                    DelegatedAITask.execution_owner == owner,
                    DelegatedAITask.status.in_(("PLANNING", "RUNNING", "AGGREGATING")),
                    DelegatedAITask.lease_until > now,
                ),
                or_(
                    and_(
                        DelegatedAISubtask.status.in_(("QUEUED", "FAILED")),
                        DelegatedAISubtask.attempt < MAX_SUBTASK_ATTEMPTS,
                    ),
                    and_(
                        DelegatedAISubtask.status == "RUNNING",
                        or_(DelegatedAISubtask.lease_until.is_(None), DelegatedAISubtask.lease_until < now),
                        DelegatedAISubtask.attempt < MAX_SUBTASK_ATTEMPTS,
                    ),
                ),
            )
            .values(
                status="RUNNING",
                execution_owner=owner,
                lease_until=_lease_deadline(SUBTASK_LEASE_SECONDS),
                attempt=DelegatedAISubtask.attempt + 1,
                started_at=now,
                finished_at=None,
                result_text=None,
                tools_used_json=None,
                error=None,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            return None
        subtask = session.get(DelegatedAISubtask, subtask_id)
        cluster = session.get(Cluster, cluster_id)
        if subtask is None or cluster is None:
            session.rollback()
            return None
        claimed = (
            subtask.role,
            subtask.objective,
            json.loads(subtask.allowed_tools_json or "[]"),
            _cluster_snapshot(cluster),
            owner,
        )
        session.commit()
        return claimed


def _set_subtask_owned(subtask_id: str, owner: str, **values) -> bool:
    with db.SessionLocal() as session:
        values["updated_at"] = _now()
        result = session.execute(
            update(DelegatedAISubtask).where(
                DelegatedAISubtask.id == subtask_id,
                DelegatedAISubtask.execution_owner == owner,
                exists().where(
                    DelegatedAITask.id == DelegatedAISubtask.task_id,
                    DelegatedAITask.status.in_(
                        ("PLANNING", "RUNNING", "AGGREGATING")
                    ),
                ),
            ).values(**values)
        )
        session.commit()
        return result.rowcount == 1


def _task_is_cancelled(task_id: str, owner: str) -> bool:
    with db.SessionLocal() as session:
        return (
            session.query(DelegatedAITask.id)
            .filter(
                DelegatedAITask.id == task_id,
                DelegatedAITask.execution_owner == owner,
                DelegatedAITask.status == "CANCELLED",
            )
            .first()
            is not None
        )


async def _wait_for_task_cancel(task_id: str, owner: str) -> None:
    """Wake the supervisor when the API marks its parent task CANCELLED."""
    while True:
        if await asyncio.to_thread(_task_is_cancelled, task_id, owner):
            return
        await asyncio.sleep(1)


def claim_tasks_for_dispatch(limit: int = 100) -> list[str]:
    """Atomically reserve tasks that need a RabbitMQ dispatch.

    This closes the crash window between the DB commit and the initial
    publish, and also re-publishes a task whose Worker lease expired. The
    dispatch timestamp is only a short de-duplication window; a failed
    publish is retried on the next watchdog pass.
    """
    now = _now()
    retry_before = now - timedelta(seconds=DISPATCH_RETRY_SECONDS)
    active_statuses = ("PLANNING", "RUNNING", "AGGREGATING")
    dispatchable = or_(
        DelegatedAITask.status == "QUEUED",
        and_(
            DelegatedAITask.status.in_(active_statuses),
            or_(DelegatedAITask.lease_until.is_(None), DelegatedAITask.lease_until < now),
        ),
    )
    dispatch_window = or_(
        DelegatedAITask.dispatch_claimed_at.is_(None),
        DelegatedAITask.dispatch_claimed_at < retry_before,
    )
    with db.SessionLocal() as session:
        candidates = (
            session.query(DelegatedAITask.id, DelegatedAITask.status)
            .filter(dispatchable, dispatch_window)
            .order_by(DelegatedAITask.created_at.asc())
            .limit(max(1, min(limit, 100)))
            .all()
        )
        claimed_ids = []
        for task_id, status in candidates:
            result = session.execute(
                update(DelegatedAITask)
                .where(
                    DelegatedAITask.id == task_id,
                    dispatchable,
                    dispatch_window,
                )
                .values(
                    status="QUEUED" if status != "QUEUED" else status,
                    execution_owner=None if status != "QUEUED" else DelegatedAITask.execution_owner,
                    lease_until=None if status != "QUEUED" else DelegatedAITask.lease_until,
                    dispatch_claimed_at=now,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                continue
            if status != "QUEUED":
                session.execute(
                    update(DelegatedAISubtask)
                    .where(
                        DelegatedAISubtask.task_id == task_id,
                        DelegatedAISubtask.status.not_in(("COMPLETED", "CANCELLED")),
                        DelegatedAISubtask.attempt >= MAX_SUBTASK_ATTEMPTS,
                    )
                    .values(
                        status="FAILED",
                        execution_owner=None,
                        lease_until=None,
                        finished_at=now,
                        error="Worker mất lease sau khi sub-agent đã hết số lần thử",
                        updated_at=now,
                    )
                )
                session.execute(
                    update(DelegatedAISubtask)
                    .where(
                        DelegatedAISubtask.task_id == task_id,
                        DelegatedAISubtask.status.not_in(("COMPLETED", "CANCELLED")),
                        DelegatedAISubtask.attempt < MAX_SUBTASK_ATTEMPTS,
                    )
                    .values(
                        status="QUEUED",
                        execution_owner=None,
                        lease_until=None,
                        updated_at=now,
                    )
                )
            claimed_ids.append(task_id)
        session.commit()
        return claimed_ids


def build_plan(prompt: str) -> list[dict]:
    """Build a bounded plan from the request.

    The first implementation is intentionally deterministic: it cannot be
    prompt-injected into creating arbitrary tools or an unbounded DAG. The
    plan still behaves like a supervisor because roles are selected per task,
    executed independently, and synthesized only after all evidence returns.
    """
    text = (prompt or "").lower()
    ceph_terms = (
        "ceph", "osd", "pg", "pool", "rbd", "mon", "cluster", "node", "host",
        "cpu", "ram", "memory", "process", "load", "iops", "latency", "throughput",
        "volume", "snapshot", "performance", "chậm", "lỗi", "sự cố", "incident", "log",
        "journal", "history", "nguyên nhân", "dung lượng",
    )
    if not any(word in text for word in ceph_terms):
        return [{
            "role": "general_analysis",
            "objective": "Phân tích yêu cầu gốc, giải quyết bằng kiến thức và suy luận phù hợp; nêu rõ giả định và phần còn thiếu.",
            "tools": [],
        }]
    plan = [
        {
            "role": "cluster_health",
            "objective": "Đọc sức khỏe tổng thể, OSD, PG và dung lượng; chỉ ra tín hiệu bất thường có số liệu.",
            "tools": ["get_health_detail", "get_osd_stat", "get_pg_stat", "get_df"],
        }
    ]
    if any(word in text for word in ("node", "host", "cpu", "ram", "memory", "process", "load", "99%", "99.3")):
        plan.append({
            "role": "node_resources",
            "objective": "Kiểm tra CPU/RAM và tình trạng node; xác định node hoặc tiến trình có dấu hiệu quá tải.",
            "tools": ["list_nodes", "get_node_metrics", "get_node_journal"],
        })
    if any(word in text for word in ("pool", "rbd", "iops", "latency", "throughput", "osd", "volume", "snapshot", "performance", "chậm")):
        plan.append({
            "role": "storage_performance",
            "objective": "Phân tích pool/RBD/OSD và hiệu năng; tách số liệu quan sát được khỏi giả thuyết.",
            "tools": ["get_pool_list", "get_osd_tree", "get_df", "get_pg_stat", "get_capacity_forecast"],
        })
    if any(word in text for word in ("lỗi", "sự cố", "incident", "log", "journal", "history", "nguyên nhân", "ceph")):
        plan.append({
            "role": "incident_history",
            "objective": "Đối chiếu incident và timeline gần đây để tìm tương quan thời gian, nguyên nhân lặp lại và kết quả xử lý.",
            "tools": ["get_recent_incidents", "get_incident_timeline", "get_disk_failure_risk"],
        })
    if len(plan) == 1:
        plan.append({
            "role": "ceph_topology",
            "objective": "Đọc topology OSD/pool để xác định phạm vi ảnh hưởng và các điểm cần kiểm tra tiếp.",
            "tools": ["get_osd_tree", "get_pool_list", "get_pg_stat"],
        })
    return plan[:min(MAX_SUBTASKS, settings.delegated_ai_max_subtasks)]


def _make_subtasks(task_id: str, plan: list[dict]) -> list[str]:
    with db.SessionLocal() as session:
        # Serialize planning with the cancel endpoint. Without the parent row
        # lock, cancellation could commit between the status check and these
        # inserts, leaving fresh QUEUED subtasks behind a CANCELLED parent.
        parent = (
            session.query(DelegatedAITask)
            .filter_by(id=task_id)
            .with_for_update()
            .first()
        )
        if parent is None or parent.status == "CANCELLED":
            session.rollback()
            return []
        existing = session.query(DelegatedAISubtask).filter_by(task_id=task_id).all()
        if existing:
            return [item.id for item in existing]
        ids = []
        for item in plan:
            row = DelegatedAISubtask(
                task_id=task_id,
                role=item["role"],
                objective=item["objective"],
                allowed_tools_json=json.dumps(item["tools"], ensure_ascii=False),
                status="QUEUED",
            )
            session.add(row)
            session.flush()
            ids.append(row.id)
        session.commit()
        return ids


async def _run_subtask(
    subtask_id: str,
    task_id: str,
    prompt: str,
    actor: str,
    cluster_id: str,
    owner: str,
    call_budget: _ProviderCallBudget,
) -> None:
    # The parent owner is passed through so a stale Worker cannot continue
    # claiming new subtasks after another Worker has reclaimed the parent.
    claimed = _claim_subtask(task_id, subtask_id, cluster_id, owner)
    if claimed is None:
        return
    role, objective, tools, cluster, owner = claimed

    agent_prompt = (
        "Bạn là một sub-agent độc lập trong hệ thống giao việc AI.\n"
        f"Vai trò: {role}.\n"
        f"Nhiệm vụ riêng: {objective}\n"
        "Chỉ dùng các tool được cấp trong lượt này. Không đề xuất hoặc thực hiện thay đổi cấu hình, "
        "không chạy hành động ghi, không suy đoán khi thiếu dữ liệu. Trả về kết quả ngắn gọn gồm: "
        "finding, evidence cụ thể, mức độ chắc chắn, và câu hỏi còn thiếu.\n"
        f"Tool được cấp: {', '.join(tools)}\n"
        f"Yêu cầu gốc của operator: {prompt[:min(MAX_PROMPT_CHARS, settings.delegated_ai_max_prompt_chars)]}"
    )
    try:
        from dashboard.chat_client import run_chat_turn

        async with _SUBTASK_SEMAPHORE:
            result = await run_chat_turn(
                [],
                agent_prompt,
                actor,
                cluster,
                allowed_tools=tools,
                max_tool_iterations=settings.delegated_ai_max_tool_iterations,
                timeout_seconds=settings.delegated_ai_provider_timeout_seconds,
                max_tokens=settings.delegated_ai_max_output_tokens,
                provider_call_budget=call_budget.reserve,
                preferred_provider="claude",
                allow_unrestricted=True,
            )
        content = str(result.get("reply_text") or "(agent không trả kết quả)")[:min(MAX_RESULT_CHARS, settings.delegated_ai_max_result_chars)]
        _set_subtask_owned(
            subtask_id,
            owner,
            status="COMPLETED",
            result_text=content,
            tools_used_json=json.dumps(result.get("tools_used") or [], ensure_ascii=False),
            error=None,
            lease_until=None,
            finished_at=_now(),
        )
    except Exception as exc:
        logger.warning("delegated subtask %s failed: %s", subtask_id, exc)
        _set_subtask_owned(
            subtask_id,
            owner,
            status="FAILED",
            error=str(exc)[:2000],
            lease_until=None,
            finished_at=_now(),
        )
    except asyncio.CancelledError:
        # Parent timeout/shutdown cancels the child. Persist a terminal
        # status before propagating cancellation so the task is not left
        # looking RUNNING forever.
        _set_subtask_owned(
            subtask_id,
            owner,
            status="FAILED",
            error="Sub-agent bị hủy do delegated task timeout/shutdown",
            lease_until=None,
            finished_at=_now(),
        )
        raise


async def _aggregate(
    task_id: str,
    prompt: str,
    actor: str,
    cluster,
    call_budget: _ProviderCallBudget,
) -> str:
    with db.SessionLocal() as session:
        rows = session.query(DelegatedAISubtask).filter_by(task_id=task_id).order_by(DelegatedAISubtask.role).all()
        reports = []
        for row in rows:
            status = row.status
            body = row.result_text or row.error or "không có kết quả"
            reports.append(
                f"[{row.role} | {status}]\n"
                f"{body[:min(MAX_RESULT_CHARS, settings.delegated_ai_max_result_chars)]}"
            )
    synthesis_prompt = (
        "Bạn là lead agent tổng hợp kết quả giao việc. Đây là các báo cáo từ những agent độc lập; "
        "không gọi tool và không bịa số liệu. Hãy trả lời operator bằng tiếng Việt, phân biệt rõ "
        "evidence đã kiểm chứng với giả thuyết, nêu kết luận ưu tiên, tác động, và các bước tiếp theo. "
        "Không tự ý tuyên bố đã sửa gì.\n\n"
        f"Yêu cầu gốc: {prompt[:min(MAX_PROMPT_CHARS, settings.delegated_ai_max_prompt_chars)]}\n\n"
        "Báo cáo sub-agent:\n" + "\n\n".join(reports)
    )
    from dashboard.chat_client import run_chat_turn

    result = await run_chat_turn(
        [],
        synthesis_prompt,
        actor,
        cluster,
        allowed_tools=[],
        max_tool_iterations=1,
        timeout_seconds=settings.delegated_ai_provider_timeout_seconds,
        max_tokens=settings.delegated_ai_max_output_tokens,
        provider_call_budget=call_budget.reserve_aggregate,
        preferred_provider="claude",
        allow_unrestricted=True,
    )
    return str(result.get("reply_text") or "Không tổng hợp được kết quả")[:min(MAX_RESULT_CHARS, settings.delegated_ai_max_result_chars)]


async def _execute_claimed_task(
    task_id: str,
    prompt: str,
    actor: str,
    cluster_id: str,
    owner: str,
) -> None:
    call_budget = _ProviderCallBudget(
        settings.delegated_ai_max_provider_calls, task_id=task_id, owner=owner
    )
    subtask_ids = _make_subtasks(task_id, build_plan(prompt))
    if not _set_task_owned(task_id, owner, status="RUNNING", expected_status="PLANNING"):
        return
    pending_subtask_ids = list(subtask_ids)
    for attempt_round in range(MAX_SUBTASK_ATTEMPTS):
        if not pending_subtask_ids:
            break
        await asyncio.gather(*(
            _run_subtask(subtask_id, task_id, prompt, actor, cluster_id, owner, call_budget)
            for subtask_id in pending_subtask_ids
        ))
        with db.SessionLocal() as session:
            pending_subtask_ids = [
                row.id
                for row in session.query(DelegatedAISubtask)
                .filter(DelegatedAISubtask.task_id == task_id)
                .all()
                if row.status in ("QUEUED", "FAILED") and row.attempt < MAX_SUBTASK_ATTEMPTS
            ]
        if pending_subtask_ids and attempt_round + 1 < MAX_SUBTASK_ATTEMPTS:
            logger.info(
                "retrying %d failed/queued delegated subtasks for task %s",
                len(pending_subtask_ids), task_id,
            )
            await asyncio.sleep(SUBTASK_RETRY_BACKOFF_SECONDS * (2 ** attempt_round))
    with db.SessionLocal() as session:
        task = session.get(DelegatedAITask, task_id)
        cluster = session.get(Cluster, cluster_id)
        rows = session.query(DelegatedAISubtask).filter_by(task_id=task_id).all()
        if task is None or cluster is None:
            return
        cluster = _cluster_snapshot(cluster)
        completed = [row for row in rows if row.status == "COMPLETED"]
        failed = [row for row in rows if row.status == "FAILED"]
    if not completed:
        raise RuntimeError("Tất cả sub-agent đều thất bại")
    if not _set_task_owned(task_id, owner, status="AGGREGATING", expected_status="RUNNING"):
        return
    result_text = await _aggregate(task_id, prompt, actor, cluster, call_budget)
    error = f"{len(failed)} sub-agent thất bại" if failed else None
    with db.SessionLocal() as session:
        result = session.execute(
            update(DelegatedAITask)
            .where(
                DelegatedAITask.id == task_id,
                DelegatedAITask.execution_owner == owner,
                DelegatedAITask.status == "AGGREGATING",
            )
            .values(
                status="COMPLETED",
                result_text=result_text,
                error=error,
                lease_until=None,
                finished_at=_now(),
                updated_at=_now(),
            )
        )
        if result.rowcount != 1:
            session.rollback()
            return
        task = session.get(DelegatedAITask, task_id)
        message = session.get(ChatMessage, task.assistant_message_id) if task else None
        if message is not None:
            message.content = result_text
            message.tools_used = json.dumps(
                [row.role for row in rows if row.status == "COMPLETED"], ensure_ascii=False
            )
        session.commit()


async def execute_task(task_id: str) -> None:
    """Execute one parent task idempotently; called by the Worker queue."""
    claimed = _claim_task(task_id)
    if claimed is None:
        logger.info("delegated task %s is already owned or terminal", task_id)
        return
    prompt, actor, cluster_id, owner = claimed
    lease_task = asyncio.create_task(_renew_task_lease(task_id, owner))
    work_task = asyncio.create_task(
        asyncio.wait_for(
            _execute_claimed_task(task_id, prompt, actor, cluster_id, owner),
            timeout=settings.delegated_ai_task_timeout_seconds,
        )
    )
    cancel_watch = asyncio.create_task(_wait_for_task_cancel(task_id, owner))

    try:
        done, _ = await asyncio.wait(
            {work_task, cancel_watch}, return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_watch in done:
            logger.info("delegated task %s cancelled by operator", task_id)
            work_task.cancel()
            try:
                await work_task
            except BaseException:
                pass
            return
        await work_task
    except Exception as exc:
        if _task_is_cancelled(task_id, owner):
            logger.info("delegated task %s stopped after cancellation", task_id)
            return
        logger.exception("delegated task %s failed", task_id)
        if _set_task_owned(
            task_id,
            owner,
            status="FAILED",
            error=str(exc)[:4000],
            lease_until=None,
            finished_at=_now(),
        ):
            with db.SessionLocal() as session:
                task = session.get(DelegatedAITask, task_id)
                if task is not None:
                    message = session.get(ChatMessage, task.assistant_message_id)
                    if message is not None:
                        message.content = f"Delegated task thất bại: {str(exc)[:1000]}"
                        session.commit()
    finally:
        cancel_watch.cancel()
        try:
            await cancel_watch
        except asyncio.CancelledError:
            pass
        if not work_task.done():
            work_task.cancel()
        try:
            await work_task
        except BaseException:
            pass
        lease_task.cancel()
        try:
            await lease_task
        except asyncio.CancelledError:
            pass


async def enqueue_task(task_id: str) -> None:
    from shared.mq import publish_delegated_task

    await publish_delegated_task(task_id)
