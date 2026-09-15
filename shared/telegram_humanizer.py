"""Best-effort AI humanization for technical Telegram alert text.

The router is optional. This module must never become a delivery dependency:
all provider/configuration failures return a compact source fallback.

Thay đổi so với bản cũ (2026-09):
- Chuẩn hoá cả câu trả lời lẫn "protected fact" trước khi so khớp
  (`osd.7` == `osd 7` == `osd_7`, `95,5 %` == `95.5%`), nên một câu đúng
  không còn bị loại chỉ vì khác dấu chấm/khoảng trắng.
- Đổi tiêu chí từ "phải nhắc lại MỌI số liệu" sang "không được bịa ID mới
  + phải nhắc ít nhất một mốc định danh". Số liệu đầy đủ vẫn có trong phần
  bằng chứng kỹ thuật gửi kèm, nên humanizer không cần gánh nhiệm vụ đó.
- Thu hẹp blacklist tiếng Anh về đúng các từ ngữ pháp (is/are/the/...);
  bỏ `failed`, `check`, `from`, `with`, `cannot` vì đó là chữ Ceph in ra,
  câu tiếng Việt trích lại `health check failed` không còn bị loại oan.
- Mọi lần loại bỏ đều log rõ LÝ DO, để còn debug được trên production.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

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
HUMANIZER_MAX_OUTPUT_TOKENS = 320  # 220 hay cắt cụt câu -> mất dấu chấm cuối
HUMANIZER_MAX_SENTENCES = 3

_SYSTEM_PROMPT = """Bạn là lớp humanizer cho cảnh báo vận hành Ceph gửi qua Telegram.
Chỉ trả về tiếng Việt tự nhiên, dễ hiểu với admin không rành thuật ngữ máy.
Tuyệt đối không xuất YAML, JSON, stacktrace, log format, markdown code block,
key-value kiểu máy hoặc nhãn kỹ thuật dài dòng. Viết tối đa 2-3 câu ngắn và
LUÔN kết thúc câu cuối bằng dấu chấm.
LUÔN nhắc ít nhất một tên định danh của sự cố (mã lỗi Ceph, hoặc tên OSD/
node/pool chính), viết đúng y nguyên dạng có trong nguồn — ví dụ nguồn ghi
osd.7 thì giữ osd.7. Ngoài mốc định danh đó ra thì không cần liệt kê lại
mọi con số: bằng chứng kỹ thuật đầy đủ đã được gửi kèm ngay bên dưới câu
tóm tắt của bạn.
Không bịa thêm OSD/node/pool/mã lỗi không có trong nguồn, không suy đoán
nguyên nhân gốc hay hành động khắc phục nếu nguồn không nói. Nếu nguồn chỉ
là thông tin kỹ thuật, hãy nói rõ đó là thông tin quan sát được.
"""

_MACHINE_RESPONSE_RE = re.compile(
    r"(?:^\s*[\[{]|^\s*(?:json|yaml|yml|traceback|stack\s+trace)\b|"
    r"^\s*(?:[-*]|\d+[.)])\s+|^\s*[-*]?\s*[A-Za-z_][\w.-]*\s*[:=]\s*\S)",
    re.IGNORECASE | re.MULTILINE,
)
_VIETNAMESE_LETTER_RE = re.compile(r"[À-ỹĐđ]")
_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")
# Chỉ giữ các từ NGỮ PHÁP tiếng Anh — dấu hiệu model trả lời bằng tiếng Anh.
# Đã bỏ failed/failure/check/from/with/cannot: đó là chữ Ceph tự in ra và
# câu tiếng Việt hoàn toàn có thể trích lại ("health check failed").
_ENGLISH_WORD_RE = re.compile(
    r"\b(?:is|are|was|were|the|and|please|this|that|must|should|would|could)\b",
    re.IGNORECASE,
)

# --- Nhận diện dữ kiện -------------------------------------------------

# osd.7 / osd 7 / OSD_7 / pg 3.1a
_DAEMON_ID_RE = re.compile(
    r"\b(osd|pg|mon|mgr|mds|rgw)\s*[._-]?\s*(\d+(?:\.[0-9a-f]+)?)\b",
    re.IGNORECASE,
)
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_HEALTH_CODE_RE = re.compile(
    r"\b(?:OSD|PG|MON|MGR|MDS|RGW|POOL|HEALTH|BLUESTORE|CEPHADM|DEVICE|"
    r"TOO|SLOW|LARGE|RECENT)(?:_[A-Z0-9]+)+\b"
)
_NAMED_VALUE_RE = re.compile(
    r"\b(?:node|host|server|pool|image|volume|bucket)\s*[:=]?\s*"
    r"([A-Za-z][A-Za-z0-9._/@-]{2,})",
    re.IGNORECASE,
)


def _canonical(value: str | None) -> str:
    """Đưa hai vế về cùng một dạng trước khi so khớp chuỗi.

    Đây chính là chỗ bản cũ sai: fact được sinh ra dạng `osd 7` (có khoảng
    trắng) nhưng model lại trả `osd.7` đúng như prompt yêu cầu, nên phép
    `fact in response` luôn False với gần như mọi log Ceph thật.
    """
    text = unicodedata.normalize("NFC", str(value or "")).casefold()
    text = re.sub(r"(\d),(\d)", r"\1.\2", text)          # 95,5 -> 95.5
    text = _DAEMON_ID_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}", text)
    text = re.sub(
        r"(\d(?:\.\d+)?)\s+(%|ms|s|sec|secs|giây|bytes?|[kmgtp]i?b)\b",
        r"\1\2",
        text,
    )
    return " ".join(text.split())


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


def _identity_facts(source_text: str | None) -> tuple[str, ...]:
    """Các mốc ĐỊNH DANH của sự cố (không phải mọi số đo).

    Dùng để (a) biết câu tóm tắt có bám vào sự cố thật không, và (b) phát
    hiện model bịa ra OSD/node/mã lỗi không tồn tại trong nguồn.
    """
    source = source_text or ""
    facts: set[str] = set()
    facts.update(f"{daemon}{number}" for daemon, number in _DAEMON_ID_RE.findall(source))
    facts.update(_IP_RE.findall(source))
    facts.update(_HEALTH_CODE_RE.findall(source))
    facts.update(match.group(1) for match in _NAMED_VALUE_RE.finditer(source))
    return tuple(sorted({_canonical(f) for f in facts if f}, key=str.casefold))


def rejection_reason(
    value: str, *, identity_facts: tuple[str, ...] = ()
) -> str | None:
    """Trả về lý do loại bỏ, hoặc None nếu câu trả lời dùng được.

    Prompt không phải là biên an toàn: provider vẫn có thể trả JSON, YAML,
    stacktrace hay một đoạn quá dài. Nhưng tiêu chí ở đây chỉ cần bảo đảm
    "không sai, không phải văn máy" — phần số liệu đầy đủ đã nằm ở khối
    bằng chứng kỹ thuật gửi kèm.
    """
    text = (value or "").strip()
    if not text:
        return "phản hồi rỗng"
    if len(text) > 1_200:
        return f"quá dài ({len(text)} ký tự)"
    if "```" in text or "{" in text or "}" in text:
        return "chứa code block / JSON"
    if _MACHINE_RESPONSE_RE.search(text):
        return "định dạng máy (key: value, bullet, traceback)"
    if not _VIETNAMESE_LETTER_RE.search(text):
        return "không phải tiếng Việt"
    if _ENGLISH_WORD_RE.search(text):
        return "lẫn câu tiếng Anh"
    sentence_count = len(_SENTENCE_END_RE.findall(text))
    if sentence_count < 1:
        # Hết max_tokens giữa câu: gửi đi sẽ là một câu cụt trên Telegram.
        return "thiếu dấu kết câu (nhiều khả năng bị cắt cụt)"
    if sentence_count > HUMANIZER_MAX_SENTENCES:
        return "quá nhiều câu"

    canonical_response = _canonical(text)

    # (a) không được bịa ID mới
    invented = [
        f"{daemon}{number}"
        for daemon, number in _DAEMON_ID_RE.findall(text)
        if f"{daemon.lower()}{number.lower()}" not in identity_facts
    ]
    if invented:
        return f"nhắc tới ID không có trong nguồn: {', '.join(sorted(set(invented)))}"
    invented_ip = [ip for ip in _IP_RE.findall(text) if _canonical(ip) not in identity_facts]
    if invented_ip:
        return f"nhắc tới IP không có trong nguồn: {', '.join(sorted(set(invented_ip)))}"

    # (b) phải bám vào ít nhất một mốc định danh của sự cố
    if identity_facts and not any(fact in canonical_response for fact in identity_facts):
        return "không nhắc lại bất kỳ mốc định danh nào của sự cố"
    return None


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
        reason = rejection_reason(result, identity_facts=_identity_facts(fallback))
        if reason is not None:
            logger.warning(
                "telegram humanizer rejected response (%s): %r", reason, result[:200]
            )
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
