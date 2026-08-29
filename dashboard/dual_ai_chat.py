"""Bounded two-agent discussion used by the Dashboard chat widget.

Both agents run in read-only mode. This mode discusses a request and makes a
reviewable plan; it does not edit, commit, push, deploy, or execute Ceph
commands. The account/model pair is the one configured for each Code Repair
role, so the same configured or separate Codex/Claude accounts are reused.
"""

from __future__ import annotations

import asyncio
import subprocess
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

SHORT_REPLY_INSTRUCTIONS = """Chỉ trả lời các ý chính đang làm:
- tối đa 5 gạch đầu dòng, tối đa 600 ký tự;
- nêu kết luận/việc đang làm, blocker hoặc rủi ro (nếu có), và bước tiếp theo;
- không chào hỏi, không nhắc lại yêu cầu, không giải thích dài, không độc thoại.
"""


class DualAIChatError(RuntimeError):
    def __init__(self, message: str, *, events: list[dict] | None = None):
        super().__init__(message)
        self.events = list(events or [])


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


def _compact_agent_output(output: str) -> str:
    """Keep the operator-facing exchange to a few useful points."""
    lines = [" ".join(line.split()) for line in (output or "").splitlines() if line.strip()]
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
    try:
        config = RepairConfig(
            repo=repo,
            planner_account_profile=_profile("planner"),
            implementer_account_profile=_profile("implementer"),
        )
        profile = _profile(role)
        codex_home, claude_config_dir = _role_account_dirs(config, profile)
        provider, command = _provider_command(
            provider_spec, repo, prompt, DISCUSSION_TIMEOUT_SECONDS,
            claude_config_dir=claude_config_dir, codex_home=codex_home,
            model=model, mode="review",
        )
        completed = await asyncio.to_thread(
            subprocess.run, command, cwd=repo,
            input=prompt if provider == "codex" else None,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            check=False, timeout=DISCUSSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise DualAIChatError(
            f"AI không phản hồi trong thời gian cho phép ({DISCUSSION_TIMEOUT_SECONDS} giây)"
        ) from exc
    except OSError as exc:
        raise DualAIChatError(f"Không khởi chạy được AI: {exc}") from exc
    except RepairError as exc:
        raise DualAIChatError(f"Cấu hình tài khoản/provider AI không hợp lệ: {exc}") from exc
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        raise DualAIChatError(f"{provider} trả về lỗi ({completed.returncode}): {output[-3000:]}")
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
    """Yield each Planner/Implementer reply as soon as it is available."""
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
            raise DualAIChatError(str(exc), events=events) from exc

    planner = await ask_and_track(
        "planner",
        """Bạn là Planner/Reviewer trong cuộc trao đổi giữa hai AI.
Phân tích yêu cầu của người dùng, làm rõ giả định, rủi ro và đề xuất kế hoạch
cụ thể cho Implementer. Chỉ đọc và suy luận; không sửa file, không chạy lệnh,
không commit/deploy. Trả lời bằng tiếng Việt.

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

    implementer = await ask_and_track(
        "implementer",
        """Bạn là Implementer đang trao đổi với Planner/Reviewer.
Đánh giá yêu cầu và kế hoạch bên dưới, chỉ ra điểm đúng/sai và mô tả cách
thực hiện cụ thể. Trong chế độ này không sửa file, không chạy lệnh,
không commit/deploy; chỉ trả lời để hai AI thống nhất. Trả lời bằng tiếng Việt.

%s

Yêu cầu:
---
%s
---
Planner/Reviewer:
---
%s
---""" % (SHORT_REPLY_INSTRUCTIONS, request, planner["content"]),
    )
    events.append(implementer)
    yield implementer
    rounds = max(0, min(int(settings.code_repair_max_review_rounds), 2))
    if rounds:
        reviewer = await ask_and_track(
            "planner",
            """Tiếp tục vai trò Planner/Reviewer. Hãy phản biện câu trả lời của
Implementer dưới đây, kiểm tra tính khả thi, phạm vi, an toàn và test plan.
Đưa ra các chỉnh sửa bắt buộc hoặc kết luận rõ ràng. Chỉ đọc và suy luận.
Trả lời bằng tiếng Việt.

%s

Yêu cầu ban đầu:
---
%s
---
Implementer:
---
%s
---""" % (SHORT_REPLY_INSTRUCTIONS, request, implementer["content"]),
        )
        events.append(reviewer)
        yield reviewer
        final = await ask_and_track(
            "implementer",
            """Bạn là Implementer. Hãy trả lời cuối cùng sau phản biện của
Planner/Reviewer: chốt phương án, các bước thực hiện và điều kiện kiểm thử.
Không sửa file hay thực hiện hành động thật trong chế độ trao đổi này.
Trả lời bằng tiếng Việt.

%s

Yêu cầu:
---
%s
---
Phản biện:
---
%s
---""" % (SHORT_REPLY_INSTRUCTIONS, request, reviewer["content"]),
        )
        events.append(final)
        yield final


async def run_dual_ai_chat(prompt: str, history: list[dict] | None = None) -> list[dict]:
    """Collect a complete Planner -> Implementer exchange for callers that need it."""
    events = []
    async for event in stream_dual_ai_chat(prompt, history):
        events.append(event)
    return events
