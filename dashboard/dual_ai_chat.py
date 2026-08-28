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
from worker.code_repair import RepairConfig, _provider_command, _role_account_dirs


MAX_DISCUSSION_CONTEXT = 24_000
MAX_AGENT_OUTPUT = 20_000
DISCUSSION_TIMEOUT_SECONDS = 600


class DualAIChatError(RuntimeError):
    pass


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


async def _ask(role: str, prompt: str) -> dict:
    repo = Path(__file__).resolve().parents[1]
    provider_spec = getattr(settings, f"code_repair_{role}_provider")
    model = getattr(settings, f"code_repair_{role}_model") or ""
    config = RepairConfig(repo=repo, planner_account_profile=_profile("planner"), implementer_account_profile=_profile("implementer"))
    profile = _profile(role)
    codex_home, claude_config_dir = _role_account_dirs(config, profile)
    provider, command = _provider_command(
        provider_spec, repo, prompt, DISCUSSION_TIMEOUT_SECONDS,
        claude_config_dir=claude_config_dir, codex_home=codex_home,
        model=model, mode="review",
    )
    try:
        completed = await asyncio.to_thread(
            subprocess.run, command, cwd=repo,
            input=prompt if provider == "codex" else None,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            check=False, timeout=DISCUSSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise DualAIChatError(f"{provider} không phản hồi trong thời gian cho phép") from exc
    except OSError as exc:
        raise DualAIChatError(f"Không khởi chạy được {provider}: {exc}") from exc
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        raise DualAIChatError(f"{provider} trả về lỗi ({completed.returncode}): {output[-3000:]}")
    if not output:
        raise DualAIChatError(f"{provider} không trả về nội dung")
    return {
        "speaker": "Planner/Reviewer" if role == "planner" else "Implementer",
        "provider": provider,
        "model": model,
        "content": output[-MAX_AGENT_OUTPUT:],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def run_dual_ai_chat(prompt: str, history: list[dict] | None = None) -> list[dict]:
    """Run a short Planner -> Implementer -> Reviewer -> Implementer exchange."""
    request = prompt.strip()
    prior = _context(history)
    planner = await _ask(
        "planner",
        """Bạn là Planner/Reviewer trong cuộc trao đổi giữa hai AI.
Phân tích yêu cầu của người dùng, làm rõ giả định, rủi ro và đề xuất kế hoạch
cụ thể cho Implementer. Chỉ đọc và suy luận; không sửa file, không chạy lệnh,
không commit/deploy. Trả lời bằng tiếng Việt.

Yêu cầu người dùng:
---
%s
---
Lịch sử liên quan:
---
%s
---""" % (request, prior or "(mới)"),
    )
    implementer = await _ask(
        "implementer",
        """Bạn là Implementer đang trao đổi với Planner/Reviewer.
Đánh giá yêu cầu và kế hoạch bên dưới, chỉ ra điểm đúng/sai và mô tả cách
thực hiện cụ thể. Trong chế độ này không sửa file, không chạy lệnh,
không commit/deploy; chỉ trả lời để hai AI thống nhất. Trả lời bằng tiếng Việt.

Yêu cầu:
---
%s
---
Planner/Reviewer:
---
%s
---""" % (request, planner["content"]),
    )
    events = [planner, implementer]
    rounds = max(0, min(int(settings.code_repair_max_review_rounds), 2))
    if rounds:
        reviewer = await _ask(
            "planner",
            """Tiếp tục vai trò Planner/Reviewer. Hãy phản biện câu trả lời của
Implementer dưới đây, kiểm tra tính khả thi, phạm vi, an toàn và test plan.
Đưa ra các chỉnh sửa bắt buộc hoặc kết luận rõ ràng. Chỉ đọc và suy luận.
Trả lời bằng tiếng Việt.

Yêu cầu ban đầu:
---
%s
---
Implementer:
---
%s
---""" % (request, implementer["content"]),
        )
        events.append(reviewer)
        final = await _ask(
            "implementer",
            """Bạn là Implementer. Hãy trả lời cuối cùng sau phản biện của
Planner/Reviewer: chốt phương án, các bước thực hiện và điều kiện kiểm thử.
Không sửa file hay thực hiện hành động thật trong chế độ trao đổi này.
Trả lời bằng tiếng Việt.

Yêu cầu:
---
%s
---
Phản biện:
---
%s
---""" % (request, reviewer["content"]),
        )
        events.append(final)
    return events
