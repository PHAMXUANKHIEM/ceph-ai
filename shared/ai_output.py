"""Provider-independent output budgets for CLI-backed AI adapters."""

from __future__ import annotations


def output_budget_instruction(max_tokens: int) -> str:
    """Ask CLI providers to stop before the configured output budget."""
    limit = max(256, int(max_tokens or 0))
    return (
        f"\n\n[Output budget] Keep the response concise and below approximately {limit} "
        "tokens. Stop immediately once the requested answer or structured "
        "tool payload is complete."
    )


def trim_text_to_token_budget(value: object, max_tokens: int) -> str:
    """Apply a conservative 4-characters/token response boundary."""
    text = str(value or "")
    limit = max(256, int(max_tokens or 0)) * 4
    if len(text) <= limit:
        return text
    marker = "\n[… phản hồi đã được rút gọn theo giới hạn token …]"
    body_limit = max(1, limit - len(marker))
    body = text[:body_limit].rsplit(" ", 1)[0].rstrip()
    return body + marker
