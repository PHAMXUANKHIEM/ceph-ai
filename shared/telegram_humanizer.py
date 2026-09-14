"""Best-effort AI humanization for technical Telegram alert text.

The router is optional. This module must never become a delivery dependency:
all provider/configuration failures return a compact source fallback.
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

from config.settings import settings
from shared.router_client import (
    RouterNotConfiguredError,
    build_router_client,
    readable_exception_message,
)

logger = logging.getLogger(__name__)

HUMANIZER_TIMEOUT_SECONDS = 8.0
HUMANIZER_MAX_INPUT_CHARS = 3_500
HUMANIZER_MAX_OUTPUT_TOKENS = 220

_SYSTEM_PROMPT = """Bạn là lớp humanizer cho cảnh báo vận hành Ceph gửi qua Telegram.
Chỉ trả về tiếng Việt tự nhiên, dễ hiểu với admin không rành thuật ngữ máy.
Tuyệt đối không xuất YAML, JSON, stacktrace, log format, markdown code block,
key-value kiểu máy hoặc nhãn kỹ thuật dài dòng. Viết tối đa 2-3 câu ngắn.
Giữ nguyên chính xác mọi số liệu, tên OSD/node/pool và mã lỗi Ceph có trong
nội dung nguồn. Không bịa thêm dữ kiện, nguyên nhân hoặc hành động chưa có
trong nguồn. Nếu nguồn chỉ là thông tin kỹ thuật, hãy nói rõ đó là thông tin
quan sát được và không khẳng định nó là nguyên nhân gốc.
"""

_MACHINE_RESPONSE_RE = re.compile(
    r"(?:^\s*[\[{]|^\s*(?:json|yaml|yml|traceback|stack\s+trace)\b|"
    r"^\s*(?:[-*]|\d+[.)])\s+|^\s*[-*]?\s*[A-Za-z_][\w.-]*\s*[:=]\s*\S)",
    re.IGNORECASE | re.MULTILINE,
)
_VIETNAMESE_LETTER_RE = re.compile(r"[À-ỹĐđ]")
_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")
_ENGLISH_WORD_RE = re.compile(
    r"\b(?:is|are|was|were|the|and|or|from|with|failed|failure|please|"
    r"check|this|that|must|should|cannot|could|would)\b",
    re.IGNORECASE,
)
_PROTECTED_DAEMON_RE = re.compile(
    r"\b(osd|pg|mon|mgr|mds|rgw)\s*[._-]?\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)
_PROTECTED_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PROTECTED_CODE_RE = re.compile(
    r"\b(?:OSD|PG|MON|MGR|RGW|POOL|HEALTH|BLUESTORE)"
    r"(?:_[A-Z0-9]+)+\b"
)
_PROTECTED_VALUE_RE = re.compile(
    r"\b(?:node|host|server|pool|image|volume)\s*[:=]\s*"
    r"([A-Za-z0-9][A-Za-z0-9._/@:-]*)",
    re.IGNORECASE,
)
_PROTECTED_QUANTITY_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|ms|s|sec|secs|seconds?|bytes?|"
    r"KiB|MiB|GiB|TiB|KB|MB|GB|TB)\b",
    re.IGNORECASE,
)


def _compact_input(value: str | None) -> str:
    lines = [" ".join(line.split()) for line in (value or "").splitlines()]
    compact = "\n".join(line for line in lines if line)
    if len(compact) <= HUMANIZER_MAX_INPUT_CHARS:
        return compact
    return compact[: HUMANIZER_MAX_INPUT_CHARS - 1].rstrip() + "…"


def _response_text(response) -> str:
    choices = getattr(response, "choices", None)
    if choices is None and isinstance(response, dict):
        choices = response.get("choices")
    if not choices:
        return ""
    first = choices[0]
    message = getattr(first, "message", None)
    if message is None and isinstance(first, dict):
        message = first.get("message")
    content = getattr(message, "content", None) if message is not None else None
    if content is None and isinstance(message, dict):
        content = message.get("content")
    return str(content or "").strip()


def _protected_facts(source_text: str | None) -> tuple[str, ...]:
    """Extract facts that a humanized response must not alter or omit."""
    source = source_text or ""
    facts: set[str] = set(_PROTECTED_IP_RE.findall(source))
    facts.update(
        f"{daemon.lower()} {number}"
        for daemon, number in _PROTECTED_DAEMON_RE.findall(source)
    )
    facts.update(_PROTECTED_CODE_RE.findall(source))
    facts.update(match.group(1) for match in _PROTECTED_VALUE_RE.finditer(source))
    facts.update(_PROTECTED_QUANTITY_RE.findall(source))
    return tuple(sorted(facts, key=str.casefold))


def _valid_response(value: str, *, protected_facts: tuple[str, ...] = ()) -> bool:
    """Accept only a short, human-readable Vietnamese paragraph.

    Prompt instructions are not a sufficient safety boundary: a provider can
    still return JSON, YAML, a stack trace, or an unexpectedly long answer.
    Invalid output falls back to the original evidence instead of reaching
    Telegram with a misleading "humanized" label.
    """
    text = (value or "").strip()
    if not text or len(text) > 1_200:
        return False
    if "```" in text or any(char in text for char in "{}[]"):
        return False
    if _MACHINE_RESPONSE_RE.search(text):
        return False
    if not _VIETNAMESE_LETTER_RE.search(text):
        return False
    if _ENGLISH_WORD_RE.search(text):
        return False
    sentence_count = len(_SENTENCE_END_RE.findall(text))
    if not 1 <= sentence_count <= 3:
        return False
    normalized_response = " ".join(text.casefold().split())
    return all(fact.casefold() in normalized_response for fact in protected_facts)


async def humanize_log_for_telegram(raw_text: str, *, context: str) -> str:
    """Return short natural Vietnamese, or compact source text on any error."""
    fallback = _compact_input(raw_text)
    if not fallback or not settings.telegram_ai_humanize_enabled:
        return fallback
    if not settings.router_enabled:
        logger.info("telegram humanizer skipped: router is disabled")
        return fallback
    if not str(settings.router_model or "").strip():
        logger.warning("telegram humanizer skipped: router model is not configured")
        return fallback

    client = None
    try:
        client = build_router_client(settings.router_api_key, settings.router_base_url)
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=settings.router_model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Loại nội dung: {str(context or 'log kỹ thuật')[:160]}\n"
                            f"Nội dung nguồn:\n{fallback}"
                        ),
                    },
                ],
                max_tokens=HUMANIZER_MAX_OUTPUT_TOKENS,
                temperature=0.1,
                timeout=httpx.Timeout(HUMANIZER_TIMEOUT_SECONDS),
            ),
            timeout=HUMANIZER_TIMEOUT_SECONDS + 1,
        )
        result = _response_text(response)
        if not _valid_response(
            result,
            protected_facts=_protected_facts(fallback),
        ):
            logger.warning("telegram humanizer returned an empty or machine-formatted response")
            return fallback
        return result
    except RouterNotConfiguredError as exc:
        logger.warning("telegram humanizer skipped: %s", exc)
    except asyncio.TimeoutError:
        logger.warning("telegram humanizer timed out after %.1fs", HUMANIZER_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("telegram humanizer failed: %s", readable_exception_message(exc))
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("telegram humanizer client close failed", exc_info=True)
    return fallback
