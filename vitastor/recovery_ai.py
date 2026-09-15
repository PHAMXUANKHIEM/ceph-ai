"""Best-effort AI explanation for resumable Vitastor deploy operations.

The AI is deliberately advisory: it explains the failed step and the verified
state, while the resumable workflow itself makes all safety decisions from
explicit SSH checks. A router outage therefore never prevents recovery.
"""

from __future__ import annotations

import asyncio
import logging

from config.settings import settings
from shared.ai_observability import mark_ai_provider, observe_ai_call, record_ai_usage
from shared.router_client import RouterNotConfiguredError, build_router_client

logger = logging.getLogger(__name__)
AI_TIMEOUT_SECONDS = 8
MAX_CONTEXT_CHARS = 9000


def _fallback(error: str, inspection: str) -> str:
    return (
        "AI chưa thể phân tích lúc này; hệ thống đã đối chiếu trạng thái từng bước "
        "và chỉ tiếp tục các bước được xác minh còn thiếu. Lỗi gần nhất: "
        f"{error[:500]}"
    )


@observe_ai_call("vitastor_deploy_recovery", scope="vitastor")
async def _call_router(error: str, inspection: str) -> str:
    if not (
        settings.vitastor_router_enabled
        and settings.vitastor_router_api_key
        and settings.vitastor_router_base_url
        and settings.vitastor_router_model
    ):
        raise RouterNotConfiguredError("Router Vitastor chưa được cấu hình")
    client = build_router_client(settings.vitastor_router_api_key, settings.vitastor_router_base_url)
    mark_ai_provider("router", settings.vitastor_router_model)
    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=settings.vitastor_router_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Bạn là trợ lý vận hành Vitastor. Chỉ trả lời tiếng Việt tự nhiên, "
                        "tối đa 3 câu. Hãy giải thích lỗi deploy và trạng thái đã kiểm tra. "
                        "Không được tự bịa dữ kiện, không đưa lệnh nguy hiểm, không tự quyết "
                        "prepare/purge disk. Nói rõ bước nào đã hoàn tất và bước nào hệ thống "
                        "sẽ tiếp tục sau khi resume."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Lỗi deploy trước đó:\n{error[:2500]}\n\n"
                        f"Kết quả kiểm tra hiện tại:\n{inspection[:MAX_CONTEXT_CHARS]}"
                    ),
                },
            ],
            max_tokens=240,
        ),
        timeout=AI_TIMEOUT_SECONDS,
    )
    record_ai_usage(response)
    content = str(response.choices[0].message.content or "").strip()
    if not content:
        raise RuntimeError("AI không trả về nội dung")
    return content[:1200]


def summarize_deploy_recovery(error: str, inspection: str) -> str:
    """Return a short AI explanation, never raising into the deploy workflow."""
    try:
        return asyncio.run(_call_router(error, inspection))
    except Exception as exc:  # best effort by design; recovery must continue
        logger.warning("Vitastor deploy recovery AI unavailable: %s", exc)
        return _fallback(error, inspection)
