"""Continuous two-agent implementation loop used by the Dashboard chat widget.

The planner/reviewer inspects the repository in read-only mode. The
implementer can edit application source and run focused tests in the same
worktree, but cannot commit, push, deploy, or execute Ceph/SSH commands. The
account/model pair is the one configured for each Code Repair role.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings
from worker.code_repair import RepairConfig, RepairError, _provider_command, _role_account_dirs


MAX_DISCUSSION_CONTEXT = 24_000
# The widget is an operator status surface, not a transcript of model
# thinking. Keep each turn compact even when a provider ignores the prompt.
MAX_AGENT_OUTPUT = 1_800
MAX_AGENT_LINES = 6
DISCUSSION_TIMEOUT_SECONDS = 600
MAX_DUAL_PROMPT_CHARS = 12_000
MAX_EXCHANGE_CONTEXT_EVENTS = 8
# Do not match a generic ``token`` word: Codex prints a normal ``tokens used``
# footer, and a different non-zero provider error would otherwise be reported
# incorrectly as quota exhaustion.
TOKEN_STOP_RE = re.compile(
    r"(?i)(?:"
    r"quota"
    r"|rate\s*limit"
    r"|usage\s*limit"
    r"|context\s*length"
    r"|tokens?\s*(?:limit|exhausted|depleted)"
    r"|(?:out|ran)\s+of\s+(?:available\s+)?tokens?"
    r")"
)
PROCESS_STOP_TIMEOUT_SECONDS = 3

SHORT_REPLY_INSTRUCTIONS = """Chỉ trả lời các ý chính đang làm:
- tối đa 5 gạch đầu dòng, tối đa 600 ký tự;
- nêu kết luận/việc đang làm, blocker hoặc rủi ro (nếu có), và bước tiếp theo;
- không chào hỏi, không nhắc lại yêu cầu, không giải thích dài, không độc thoại.
"""

PLANNER_INSTRUCTIONS = """Bạn là Planner/Reviewer của repo Ceph-AI.
Bạn được phép tự đọc repository bằng các tool có sẵn trong workdir. Không hỏi
người dùng cung cấp cây thư mục, file hay quyền đọc. Ngay lượt đầu hãy tự xem
git status, cây file và entrypoint/module liên quan rồi chọn đúng một task nhỏ
để cải thiện tính năng AI. Ở các lượt sau, đọc diff và test hiện tại để review
task trước hoặc chọn task nhỏ kế tiếp. Chỉ đọc/suy luận, không sửa file.
"""

IMPLEMENTER_INSTRUCTIONS = """Bạn là Implementer của repo Ceph-AI.
Bạn được phép tự đọc và sửa source/test trong workdir. Không hỏi người dùng
cung cấp context. Đọc repo và diff hiện tại, thực hiện ngay task cụ thể mà
Planner vừa nêu; không chỉ mô tả kế hoạch. Nếu task đã làm rồi, review kết quả
và sửa phần còn thiếu. Chạy focused test phù hợp sau khi sửa.
Giữ nguyên mọi thay đổi có sẵn không thuộc task; không reset hoặc xoá diff của
người dùng.
Không sửa credentials/.env, workflow, migration, deployment script hoặc file
generated; không chạy lệnh Ceph/SSH/destructive; không commit, push hay deploy.
"""


class DualAIChatError(RuntimeError):
    def __init__(self, message: str, *, events: list[dict] | None = None):
        super().__init__(message)
        self.events = list(events or [])


class DualAIChatExhausted(DualAIChatError):
    """Provider stopped because its token/quota limit was reached."""


def _profile(role: str) -> str:
    source = getattr(settings, f"code_repair_{role}_account_source", "configured")
    profile = getattr(settings, f"code_repair_{role}_account_profile", "")
    return profile.strip() if source == "separate" else "configured"


def _context(history: list[dict] | None) -> str:
    lines = []
    for item in (history or [])[-8:]:
        role = "Người dùng" if item.get("role") == "user" else "AI"
        lines.append(f"{role}: {str(item.get('content') or '')[-4000:]}")
    return "\n".join(lines)[-MAX_DISCUSSION_CONTEXT:]


def _exchange_context(events: list[dict]) -> str:
    """Return only the latest compact turns for the next AI prompt."""
    lines = [
        f"{event.get('speaker', 'AI')}: {event.get('content', '')}"
        for event in events[-MAX_EXCHANGE_CONTEXT_EVENTS:]
    ]
    return "\n".join(lines)[-MAX_DISCUSSION_CONTEXT:]


def _compact_agent_output(output: str) -> str:
    """Keep the operator-facing exchange to a few useful points."""
    lines = [" ".join(line.split()) for line in (output or "").splitlines() if line.strip()]
    # Codex writes its own startup banner to stdout before the actual answer.
    # Keeping it consumed the whole six-line display budget and hid the work.
    if lines and lines[0].startswith("OpenAI Codex "):
        while lines and (
            lines[0] == "--------"
            or lines[0].startswith(("workdir:", "model:", "provider:"))
            or lines[0].startswith("OpenAI Codex ")
        ):
            lines.pop(0)
    lines = [line for line in lines if not line.startswith("```")]
    if len(lines) > MAX_AGENT_LINES:
        lines = lines[: MAX_AGENT_LINES - 1] + [lines[-1]]
    compact = "\n".join(lines).strip()
    if len(compact) <= MAX_AGENT_OUTPUT:
        return compact
    tail_chars = 420
    head_chars = MAX_AGENT_OUTPUT - tail_chars - len("\n…\n")
    tail = lines[-1]
    if len(tail) > tail_chars:
        half = (tail_chars - len(" … ")) // 2
        tail = tail[:half].rstrip() + " … " + tail[-half:].lstrip()
    return compact[:head_chars].rstrip() + "\n…\n" + tail


async def _ask(role: str, prompt: str) -> dict:
    repo = Path(__file__).resolve().parents[1]
    provider_spec = getattr(settings, f"code_repair_{role}_provider")
    model = getattr(settings, f"code_repair_{role}_model") or ""
    process = None
    try:
        config = RepairConfig(
            repo=repo,
            planner_account_profile=_profile("planner"),
            implementer_account_profile=_profile("implementer"),
        )
        profile = _profile(role)
        codex_home, claude_config_dir = _role_account_dirs(config, profile)
        mode = "review" if role == "planner" else "implement"
        provider, command = _provider_command(
            provider_spec, repo, prompt, DISCUSSION_TIMEOUT_SECONDS,
            claude_config_dir=claude_config_dir, codex_home=codex_home,
            model=model, mode=mode,
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=repo,
            stdin=asyncio.subprocess.PIPE if provider == "codex" else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_bytes, _ = await asyncio.wait_for(
            process.communicate(prompt.encode() if provider == "codex" else None),
            DISCUSSION_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), PROCESS_STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    except asyncio.TimeoutError as exc:
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), PROCESS_STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise DualAIChatError(
            f"AI không phản hồi trong thời gian cho phép ({DISCUSSION_TIMEOUT_SECONDS} giây)"
        ) from exc
    except OSError as exc:
        raise DualAIChatError(f"Không khởi chạy được AI: {exc}") from exc
    except RepairError as exc:
        raise DualAIChatError(f"Cấu hình tài khoản/provider AI không hợp lệ: {exc}") from exc
    output = output_bytes.decode(errors="replace").strip()
    if process.returncode != 0:
        if TOKEN_STOP_RE.search(output):
            raise DualAIChatExhausted("Provider đã hết token hoặc quota")
        raise DualAIChatError(f"{provider} trả về lỗi ({process.returncode}): {output[-3000:]}")
    if not output:
        raise DualAIChatError(f"{provider} không trả về nội dung")
    return {
        "speaker": "Planner/Reviewer" if role == "planner" else "Implementer",
        "provider": provider,
        "model": model,
        "content": _compact_agent_output(output),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def stream_dual_ai_chat(prompt: str, history: list[dict] | None = None):
    """Yield alternating replies until a provider stops responding.

    There is intentionally no review-round stop condition here. Provider quota,
    token/context exhaustion, timeout, or another provider error ends the
    background exchange.
    """
    request = prompt.strip()
    if len(request) > MAX_DUAL_PROMPT_CHARS:
        raise DualAIChatError(
            f"Yêu cầu quá dài; chế độ hai AI chỉ nhận tối đa {MAX_DUAL_PROMPT_CHARS} ký tự"
        )
    prior = _context(history)
    events: list[dict] = []

    async def ask_and_track(role: str, prompt_text: str) -> dict:
        try:
            return await _ask(role, prompt_text)
        except DualAIChatError as exc:
            error_type = DualAIChatExhausted if isinstance(exc, DualAIChatExhausted) else DualAIChatError
            raise error_type(str(exc), events=events) from exc

    planner = await ask_and_track(
        "planner",
        f"""{PLANNER_INSTRUCTIONS}
Trả lời bằng tiếng Việt.

%s

Yêu cầu người dùng:
---
%s
---
Lịch sử liên quan:
---
%s
---""" % (SHORT_REPLY_INSTRUCTIONS, request, prior or "(mới)"),
    )
    events.append(planner)
    yield planner

    role = "implementer"
    while True:
        speaker = "Implementer" if role == "implementer" else "Planner/Reviewer"
        prompt_text = f"""{IMPLEMENTER_INSTRUCTIONS if role == 'implementer' else PLANNER_INSTRUCTIONS}
Bạn là {speaker}, đang tiếp tục trao đổi liên tục với AI còn lại. Đọc các lượt
gần nhất, phản hồi trực tiếp ý trước, rồi làm/đề xuất đúng một task nhỏ tiếp
theo. Không tuyên bố kết thúc; tiếp tục cho đến khi provider hết token hoặc
người dùng bấm Dừng.
Trả lời bằng tiếng Việt.

{SHORT_REPLY_INSTRUCTIONS}

Yêu cầu ban đầu:
---
{request}
---
Các lượt gần nhất:
---
{_exchange_context(events)}
---"""
        event = await ask_and_track(role, prompt_text)
        events.append(event)
        yield event
        role = "planner" if role == "implementer" else "implementer"


async def run_dual_ai_chat(prompt: str, history: list[dict] | None = None) -> list[dict]:
    """Collect a complete Planner -> Implementer exchange for callers that need it."""
    events = []
    async for event in stream_dual_ai_chat(prompt, history):
        events.append(event)
    return events
