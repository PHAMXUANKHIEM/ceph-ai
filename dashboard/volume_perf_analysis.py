"""On-demand "Phân tích bằng AI" for the Volumes page's Load Sweep result
(dashboard/routes/volumes.py's analyze route) — sends an already-completed
sweep's full curve (worker/executor/volume_perf.py's own steps/knee/QoS/
bottleneck evidence) to the operator's configured router for a final,
plain-language conclusion.

Why this exists alongside _detect_knee's own algorithmic result: that
heuristic is a fixed set of growth-ratio thresholds, tuned against one
example shape (the operator's own "~3% more IOPS for ~14x more latency"
description) — it can miss a curve that saturates differently. This is a
genuinely SEPARATE second read of the same evidence via an LLM, not a
re-implementation of the same math; the operator explicitly asked for both
(the heuristic's number is still shown, this augments rather than replaces
it).

Runs from the Dashboard process, not Worker — same posture as
dashboard/chat_client.py: this is an on-demand, read-only analysis of
already-collected data (no SSH, no cluster mutation), so it doesn't need
Worker's propose/approve/execute machinery at all.
"""

import json
import logging
import re

import httpx

from config.settings import settings
from shared.codex_app_server import CodexAppServerError, codex_app_server
from shared.claude_cli import ClaudeCLIError, run_claude_prompt
from shared.ai_provider_runtime import refresh_chat_provider_flags
from shared.router_client import RouterNotConfiguredError, build_router_client, readable_exception_message
from shared.ai_redaction import default_redactor
from shared.ai_observability import mark_ai_provider, observe_ai_call, record_ai_usage

logger = logging.getLogger(__name__)

MAX_TOKENS = 1024
ROUTER_TIMEOUT_SECONDS = 60.0
TOOL_NAME = "report_volume_perf_conclusion"

SYSTEM_PROMPT = (
    "You are an expert Ceph storage performance engineer. Given the results of "
    "a load-sweep benchmark (fio, 4k random write, iodepth swept 1->256 "
    "against a dedicated scratch RBD image), plus supplementary QoS/"
    "bottleneck diagnostics, determine this pool's actual maximum USABLE "
    "performance ceiling — the point where pushing further trades a small "
    "IOPS gain for a disproportionate latency increase, not simply the "
    "highest IOPS number in the data. If the sweep never clearly saturated, "
    "say so honestly rather than inventing a ceiling. Explain your reasoning "
    "in Vietnamese, in plain language an operator without deep storage-"
    "benchmarking background can understand."
)

_REQUIRED_FIELDS = {"max_iops", "max_iops_basis", "confidence", "conclusion_vi", "caveats_vi"}


class VolumePerfAnalysisError(Exception):
    """Raised for anything that stops this from producing a usable
    conclusion — router not configured, network/API failure, a truncated
    response, a missing tool call, or a result missing a required field.
    Mirrors worker/llm/router_client.py::RouterDiagnosisError, kept as its
    own class since this runs from the Dashboard process, not Worker."""


def _get_client():
    return build_router_client(settings.router_api_key, settings.router_base_url)


def _tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Report the final conclusion about this pool's maximum usable "
                "performance, based on the load-sweep evidence provided."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "max_iops": {
                        "type": "number",
                        "description": "The concluded maximum usable IOPS ceiling (4k random write).",
                    },
                    "max_iops_basis": {
                        "type": "string",
                        "enum": ["saturation_knee", "highest_tested_not_saturated"],
                        "description": (
                            "saturation_knee: the sweep showed a clear knee, this is that point. "
                            "highest_tested_not_saturated: the sweep never saturated within the "
                            "tested range — this is only a lower-bound floor, not a real ceiling."
                        ),
                    },
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "conclusion_vi": {
                        "type": "string",
                        "description": "1-3 câu kết luận bằng tiếng Việt cho operator.",
                    },
                    "caveats_vi": {
                        "type": "string",
                        "description": (
                            "Lưu ý/giới hạn của kết luận này bằng tiếng Việt — ví dụ nghi ngờ nút "
                            "thắt nằm ở đâu (dựa trên bottleneck diagnostics), có bị giới hạn QoS "
                            "không, hay khi nào nên đo lại."
                        ),
                    },
                },
                "required": sorted(_REQUIRED_FIELDS),
                "additionalProperties": False,
            },
        },
    }


def _build_user_content(sweep: dict) -> str:
    steps_lines = "\n".join(
        f"- iodepth={s['iodepth']}: IOPS={s['iops']}, latency_avg={s['latency_avg_ms']}ms, "
        f"latency_p99={s['latency_p99_ms']}ms"
        for s in (sweep.get("steps") or [])
    )
    return (
        f"Pool: {sweep.get('pool')}\n"
        f"Scratch image tested: {sweep.get('scratch_image')}\n\n"
        f"Sweep steps:\n{steps_lines or '(không có dữ liệu)'}\n\n"
        f"Algorithmic knee-detection result (fixed heuristic, may be wrong): "
        f"{json.dumps(sweep.get('knee'))}\n\n"
        f"QoS notes:\n{sweep.get('qos_notes') or '(không có)'}\n\n"
        f"Bottleneck diagnostics:\n{sweep.get('bottleneck_notes') or '(không có)'}"
    )


@observe_ai_call("volume_perf_analysis")
async def analyze_volume_perf_sweep(sweep: dict) -> dict:
    """`sweep` is the plain-dict shape volume_perf_sweep_analyze_api builds
    from a VolumePerfSweep row: {"pool", "scratch_image", "steps", "knee",
    "qos_notes", "bottleneck_notes"}. Returns the tool-call arguments dict
    unchanged (already validated against _REQUIRED_FIELDS)."""
    refresh_chat_provider_flags()
    if not sweep.get("steps"):
        raise VolumePerfAnalysisError("Không có dữ liệu bước đo nào để phân tích.")
    sweep = default_redactor.redact(sweep)
    provider_errors: list[str] = []
    if settings.codex_chat_enabled:
        captured: dict = {}

        async def capture_conclusion(tool_name: str, arguments: dict) -> tuple[str, bool]:
            if tool_name != TOOL_NAME:
                return f"Tool không được phép: {tool_name}", False
            captured.update(arguments)
            return "Đã ghi nhận kết luận hiệu năng.", True

        prompt = (
            SYSTEM_PROMPT
            + "\n\nBạn BẮT BUỘC gọi tool report_volume_perf_conclusion đúng một lần; "
              "không trả kết luận chỉ bằng văn bản.\n\n"
            + _build_user_content(sweep)
        )
        try:
            await codex_app_server.run_turn(
                prompt, [_tool_schema()], capture_conclusion, timeout=ROUTER_TIMEOUT_SECONDS
            )
        except CodexAppServerError as exc:
            provider_errors.append(f"Codex: {exc}")
            logger.warning("%s; trying configured fallback", provider_errors[-1])
        else:
            if _REQUIRED_FIELDS.issubset(captured):
                return captured
            provider_errors.append(f"Codex thiếu trường bắt buộc: {captured!r}")

    if settings.claude_chat_enabled:
        prompt = (
            SYSTEM_PROMPT
            + "\n\nChỉ trả về một JSON object hợp lệ, không markdown, với đúng các trường: "
              "max_iops (number), max_iops_basis ('saturation_knee' hoặc "
              "'highest_tested_not_saturated'), confidence ('high', 'medium' hoặc 'low'), "
              "conclusion_vi (string), caveats_vi (string).\n\n"
            + _build_user_content(sweep)
        )
        try:
            raw = await run_claude_prompt(prompt, timeout=ROUTER_TIMEOUT_SECONDS)
            clean = raw.strip()
            if clean.startswith("```"):
                clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.IGNORECASE)
            result = json.loads(clean)
        except (ClaudeCLIError, json.JSONDecodeError) as exc:
            provider_errors.append(f"Claude: {exc}")
            logger.warning("%s; trying configured fallback", provider_errors[-1])
        else:
            if _REQUIRED_FIELDS.issubset(result):
                return result
            provider_errors.append(f"Claude thiếu trường bắt buộc: {result!r}")

    router_ready = bool(
        settings.router_enabled
        and settings.router_api_key
        and settings.router_base_url
        and settings.router_model
    )
    if not router_ready:
        if provider_errors:
            raise VolumePerfAnalysisError("; ".join(provider_errors))
        raise VolumePerfAnalysisError("Router đang tắt hoặc chưa cấu hình đầy đủ")

    try:
        client = _get_client()
    except RouterNotConfiguredError as exc:
        raise VolumePerfAnalysisError(str(exc)) from exc

    try:
        mark_ai_provider("router", settings.router_model)
        async with client.chat.completions.stream(
            model=settings.router_model,
            max_tokens=MAX_TOKENS,
            tools=[_tool_schema()],
            stream_options={"include_usage": True},
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_content(sweep)},
            ],
            # httpx.Timeout(...), not a bare float — worker/llm/router_client.py's
            # own _call_router found a bare float silently truncates a
            # .stream() call against the real 9router.
            timeout=httpx.Timeout(ROUTER_TIMEOUT_SECONDS),
        ) as stream:
            completion = await stream.get_final_completion()
        record_ai_usage(completion)
    except Exception as exc:
        raise VolumePerfAnalysisError(readable_exception_message(exc)) from exc

    choice = completion.choices[0]
    if choice.finish_reason == "length":
        raise VolumePerfAnalysisError(f"Phản hồi AI bị cắt ở max_tokens={MAX_TOKENS}")
    for call in choice.message.tool_calls or []:
        if call.function.name == TOOL_NAME:
            try:
                result = json.loads(call.function.arguments or "{}")
            except (TypeError, ValueError) as exc:
                raise VolumePerfAnalysisError(f"Phản hồi AI không phải JSON hợp lệ: {exc}") from exc
            if not _REQUIRED_FIELDS.issubset(result):
                raise VolumePerfAnalysisError(f"Phản hồi AI thiếu trường bắt buộc: {result!r}")
            return result
    raise VolumePerfAnalysisError(f"AI không gọi tool {TOOL_NAME} — không có kết luận")
