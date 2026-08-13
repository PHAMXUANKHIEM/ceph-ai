"""Provider-neutral, read-only AI diagnosis for Vitastor telemetry evidence."""

from __future__ import annotations

import json

from config.settings import settings
from shared.claude_cli import run_claude_prompt
from shared.codex_app_server import codex_app_server
from shared.router_client import build_router_client, readable_exception_message

SYSTEM_PROMPT = """Bạn là chuyên gia vận hành Vitastor. Chỉ chẩn đoán từ evidence được cấp.
Không được tuyên bố đã chạy lệnh hoặc đã thay đổi cluster. Không bịa host, OSD, pool hay
metric. Nếu evidence thiếu, nói rõ. Trả về duy nhất JSON hợp lệ với các trường:
root_cause, impact, confidence (low|medium|high), evidence (mảng chuỗi),
recommended_steps (mảng chuỗi), commands_preview (mảng chuỗi), safety_notes (mảng chuỗi).
Command chỉ là preview read-only hoặc đề xuất cần phê duyệt; không tự thực thi."""


def _prompt(cluster_name: str, evidence: dict) -> str:
    return (
        f"{SYSTEM_PROMPT}\n\nCluster: {cluster_name}\n"
        f"Evidence JSON:\n{json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}"
    )


def parse_diagnosis(text: str) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0].strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("AI không trả về JSON chẩn đoán")
    value = json.loads(raw[start:end + 1])
    required = {
        "root_cause", "impact", "confidence", "evidence",
        "recommended_steps", "commands_preview", "safety_notes",
    }
    if not isinstance(value, dict) or not required.issubset(value):
        raise ValueError("JSON chẩn đoán thiếu trường bắt buộc")
    if value["confidence"] not in {"low", "medium", "high"}:
        raise ValueError("Confidence của chẩn đoán không hợp lệ")
    for key in ("evidence", "recommended_steps", "commands_preview", "safety_notes"):
        if not isinstance(value[key], list) or not all(isinstance(item, str) for item in value[key]):
            raise ValueError(f"Trường {key} phải là mảng chuỗi")
    return value


async def diagnose(cluster_name: str, evidence: dict) -> tuple[str, dict]:
    """Call the selected Vitastor AI provider and validate its JSON contract."""
    prompt = _prompt(cluster_name, evidence)
    if settings.vitastor_codex_chat_enabled:
        async def no_tools(_name, _arguments):
            return "Không có tool thực thi trong chẩn đoán Vitastor", False
        response = await codex_app_server.run_turn(prompt, [], no_tools)
        text = response.get("reply_text") or ""
    elif settings.vitastor_claude_chat_enabled:
        text = await run_claude_prompt(prompt)
    elif (
        settings.vitastor_router_enabled and settings.vitastor_router_api_key
        and settings.vitastor_router_base_url and settings.vitastor_router_model
    ):
        client = build_router_client(settings.vitastor_router_api_key, settings.vitastor_router_base_url)
        response = await client.chat.completions.create(
            model=settings.vitastor_router_model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content or ""
    else:
        raise RuntimeError("Chưa cấu hình AI cho Vitastor")
    try:
        return text, parse_diagnosis(text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(readable_exception_message(exc)) from exc
