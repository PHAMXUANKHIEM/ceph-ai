"""AI-powered backup failure/anomaly analysis (Story 9.5, PRD FR-11 to
FR-13, FR-14) — reuses the SAME provider-agnostic router client
`shared/router_client.py::build_router_client()` that `worker/llm/
router_client.py` (Incident diagnosis) and `dashboard/chat_client.py`
(Chat-with-AI) already use. Deliberately does NOT import the `anthropic`
SDK — that package isn't even in this project's dependency graph since the
2026-07-24 "9router -> API AI" rename (verified against `pyproject.toml`
before writing this module) — and does NOT create a new LLM client.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from config.settings import settings
from shared import db
from shared.models import BackupAnomaly, BackupJob
from shared.router_client import build_router_client
from worker.backup import alerting
from worker.redaction import default_redactor

logger = logging.getLogger(__name__)

# 2026-08-07: same fix as worker/llm/router_client.py's MAX_TOKENS (see its
# comment) -- this module calls the SAME router/model via the SAME
# build_router_client(), so it hit the identical openai.LengthFinishReasonError
# truncation (confirmed in worker.log: worker.backup.ai_analysis.AIAnalysisError:
# "Could not parse response content as the length limit was reached").
MAX_TOKENS = 8192
ROUTER_TIMEOUT_SECONDS = 60.0
TOOL_NAME = "report_backup_analysis"

SYSTEM_PROMPT = (
    "You are an expert Ceph storage cluster SRE analyzing a FAILED or "
    "anomalous backup job. Given the job's context (pool, image, job type, "
    "error message or anomaly details), summarize the likely root cause in "
    "plain Vietnamese an operator can understand, classify how serious it "
    "is, and suggest one concrete remediation action referencing the "
    "specific numbers/hosts/error observed — never a generic platitude."
)

_VALID_SEVERITIES = ("critical", "warning", "info")


class AIAnalysisError(Exception):
    """Raised for any invalid/failed router response — mirrors
    `worker/llm/router_client.py::RouterDiagnosisError`'s posture."""


def _tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Report the root-cause analysis of a failed or anomalous backup job.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "root_cause_summary_vi": {
                        "type": "string",
                        "description": "Plain-language (Vietnamese) root cause summary.",
                    },
                    "severity": {"type": "string", "enum": list(_VALID_SEVERITIES)},
                    "suggested_action_vi": {
                        "type": "string",
                        "description": (
                            "Concrete remediation suggestion referencing the specific "
                            "error/numbers/hosts observed — not a generic statement."
                        ),
                    },
                },
                "required": ["root_cause_summary_vi", "severity", "suggested_action_vi"],
                "additionalProperties": False,
            },
        },
    }


def _get_client():
    return build_router_client(settings.router_api_key, settings.router_base_url)


def _build_user_content(context: dict) -> str:
    return (
        f"Pool: {context.get('pool')}\n"
        f"Image: {context.get('image')}\n"
        f"Job type: {context.get('job_type')}\n"
        f"Error message: {context.get('error_message') or '(none)'}\n"
        f"Anomaly kind: {context.get('anomaly_kind') or '(none)'}\n"
        f"Anomaly details: {context.get('anomaly_details') or '(none)'}"
    )


async def _call_router(user_content: str) -> dict:
    """The single call site to the router (AD-6's redaction insertion
    point is the caller, right before this runs) — same tool-use/
    structured-output pattern `worker/llm/router_client.py::_call_router`
    already uses (forced `tool_choice`, `strict` schema — verified there
    against a real running router that otherwise answers in plain text)."""
    client = _get_client()
    try:
        async with client.chat.completions.stream(
            model=settings.router_model,
            max_tokens=MAX_TOKENS,
            tools=[_tool_schema()],
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            timeout=httpx.Timeout(ROUTER_TIMEOUT_SECONDS),
        ) as stream:
            completion = await stream.get_final_completion()
    except Exception as exc:
        raise AIAnalysisError(f"Router call failed: {exc}") from exc

    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise AIAnalysisError(f"Router response truncated at max_tokens={MAX_TOKENS}")
    for call in choice.message.tool_calls or []:
        if call.function.name == TOOL_NAME:
            try:
                return json.loads(call.function.arguments or "{}")
            except (TypeError, ValueError) as exc:
                raise AIAnalysisError(
                    f"{TOOL_NAME} arguments were not valid JSON: {call.function.arguments!r}"
                ) from exc
    raise AIAnalysisError(f"Router response contained no {TOOL_NAME} tool call")


DIGEST_SYSTEM_PROMPT = (
    "You are an expert Ceph storage cluster SRE writing a periodic backup "
    "health digest for an operator. Given raw counts (jobs succeeded/"
    "failed, anomalies detected, latest restore-drill result), write a "
    "short, plain Vietnamese natural-language summary of the period — no "
    "structured format needed, just clear prose an operator can skim."
)


def _build_digest_user_content(stats: dict) -> str:
    return (
        f"Kỳ digest: {stats['period_start']} đến {stats['period_end']}\n"
        f"Số job backup thành công: {stats['succeeded_count']}\n"
        f"Số job backup thất bại: {stats['failed_count']}\n"
        f"Số bất thường (anomaly) phát hiện: {stats['anomaly_count']}\n"
        f"Chi tiết bất thường: {'; '.join(stats['anomaly_details']) or '(không có)'}\n"
        f"RestoreDrill gần nhất: {stats['latest_restore_drill_status'] or 'chưa từng chạy'} "
        f"(lúc {stats['latest_restore_drill_at'] or 'N/A'})"
    )


async def _call_digest_router(user_content: str) -> str:
    """Digest has no structured schema (AC #6 only needs free-text prose,
    unlike per-job analysis's machine-readable severity) — still uses
    `.stream()`+`get_final_completion()` rather than a plain non-streaming
    `create()` call, matching `worker/llm/router_client.py::_call_router`'s
    own verified workaround for this project's router always responding
    via SSE regardless of whether streaming was requested."""
    client = _get_client()
    try:
        async with client.chat.completions.stream(
            model=settings.router_model,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            timeout=httpx.Timeout(ROUTER_TIMEOUT_SECONDS),
        ) as stream:
            completion = await stream.get_final_completion()
    except Exception as exc:
        raise AIAnalysisError(f"Digest router call failed: {exc}") from exc
    content = (completion.choices[0].message.content or "").strip()
    if not content:
        raise AIAnalysisError("Digest router response had no content")
    return content


async def summarize_digest(stats: dict) -> str:
    """`stats` — see `worker/backup/digest.py::_gather_stats()` for the
    exact shape. Redacts (AD-6) then calls the router for a free-text
    summary."""
    payload = default_redactor.redact(stats)
    user_content = _build_digest_user_content(payload)
    return await _call_digest_router(user_content)


async def analyze(context: dict) -> dict:
    """Redacts (AD-6) then calls the router. Returns
    `{root_cause_summary_vi, severity, suggested_action_vi}`."""
    payload = default_redactor.redact(context)
    user_content = _build_user_content(payload)
    result = await _call_router(user_content)

    summary = (result.get("root_cause_summary_vi") or "").strip()
    severity = (result.get("severity") or "").strip()
    suggestion = (result.get("suggested_action_vi") or "").strip()
    if not summary or severity not in _VALID_SEVERITIES or not suggestion:
        raise AIAnalysisError(f"incomplete/invalid analysis result: {result!r}")
    return {"root_cause_summary_vi": summary, "severity": severity, "suggested_action_vi": suggestion}


def _has_since_recovered(job: BackupJob) -> bool:
    """AC #3 de-dup: a later SUCCESS already exists for the same
    (pool, image, job_type) — the failure already self-recovered, so it
    must not be classified/alerted as critical."""
    if job.pool is None or job.image is None:
        return False
    with db.SessionLocal() as session:
        later_success = (
            session.query(BackupJob)
            .filter(
                BackupJob.pool == job.pool,
                BackupJob.image == job.image,
                BackupJob.job_type == job.job_type,
                BackupJob.status == "SUCCESS",
                BackupJob.created_at > job.created_at,
            )
            .first()
        )
    return later_success is not None


def _fallback_result(job: BackupJob, anomaly: dict | None) -> dict:
    """Used when the AI call itself fails — the operator must still get
    SOME signal, not silence, even if the router is unreachable."""
    return {
        "root_cause_summary_vi": job.error_message or (anomaly["details"] if anomaly else "Không rõ nguyên nhân"),
        "severity": "critical" if job.status == "FAILED" else "warning",
        "suggested_action_vi": "Kiểm tra log Worker để biết chi tiết — AI phân tích thất bại.",
    }


def analyze_backup_job(job: BackupJob, anomaly: dict | None = None) -> None:
    """Called by `worker/backup/engine.py`/`worker/backup/metadata.py`
    right after a BackupJob is marked FAILED, or after a SUCCESS job's
    `worker/backup/anomaly.py::check_anomaly()` returns non-None. Never
    called for an ordinary successful job with no anomaly (AC #8 — cost/
    noise control).

    Synchronous on purpose — `engine.py`/`metadata.py` are plain sync
    functions running inside `asyncio.to_thread` (Story 9.1's Scheduler),
    so a nested `asyncio.run()` here is safe (this thread has no event
    loop of its own already running)."""
    if job.status != "FAILED" and anomaly is None:
        return

    context = {
        "pool": job.pool,
        "image": job.image,
        "job_type": job.job_type,
        "error_message": job.error_message,
        "anomaly_kind": anomaly["kind"] if anomaly else None,
        "anomaly_details": anomaly["details"] if anomaly else None,
    }

    try:
        result = asyncio.run(analyze(context))
    except AIAnalysisError:
        logger.exception(
            "analyze_backup_job: AI analysis failed for BackupJob %s — falling back to a generic message",
            job.id,
        )
        result = _fallback_result(job, anomaly)

    severity = result["severity"]
    if job.status == "FAILED" and severity == "critical" and _has_since_recovered(job):
        severity = "warning"

    if anomaly is not None:
        with db.SessionLocal() as session:
            session.add(
                BackupAnomaly(
                    backup_job_id=job.id,
                    kind=anomaly["kind"],
                    severity=severity,
                    ai_summary=result["root_cause_summary_vi"],
                )
            )
            session.commit()

    message = (
        f"🤖 Phân tích AI: {result['root_cause_summary_vi']}\n"
        f"🔧 Giải pháp đề xuất: {result['suggested_action_vi']}"
    )
    if job.status == "FAILED":
        # Every failed job needs one actionable notification immediately.
        # Previously a failure classified WARNING was only logged, then the
        # periodic checker sent a separate raw traceback with no AI result.
        alerting.send_alert(severity, message, backup_job_id=job.id)
    elif severity == "critical":
        alerting.send_alert("critical", message, backup_job_id=job.id)
    else:
        # Non-critical anomalies on successful jobs stay digest-only.
        logger.log(
            logging.WARNING if severity == "warning" else logging.INFO,
            "backup analysis [%s]: %s (backup_job_id=%s)",
            severity,
            message,
            job.id,
        )
