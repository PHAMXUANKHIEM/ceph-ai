from openai import AsyncOpenAI


class RouterNotConfiguredError(Exception):
    """Raised when router_api_key/router_base_url aren't configured. This
    app never calls a vendor's AI API directly — every call goes through
    the operator's configured 9router only (config/settings.py's
    router_base_url/router_api_key)."""


def readable_exception_message(exc: BaseException) -> str:
    """str(exc) is empty for some exception types (e.g. httpx.ReadTimeout()
    with no args, which a slow/unreachable router raises) — a blank reason
    string is worse than none when shown to an operator. Falls back to the
    exception's class name (e.g. "ReadTimeout") so a timeout still reads as
    a timeout instead of silently empty text."""
    return str(exc) or type(exc).__name__


def _require_config(api_key: str, base_url: str) -> None:
    if not base_url or not api_key:
        raise RouterNotConfiguredError(
            "Chưa cấu hình API AI (API key/Base URL) — vào Settings để kết nối."
        )


def build_router_client(api_key: str, base_url: str) -> AsyncOpenAI:
    """Single place both worker/llm/router_client.py (incident diagnosis)
    and dashboard/chat_client.py (Chat-with-AI) construct their 9router
    client from — keeps this from diverging between the two call sites.

    `base_url` is the operator-facing router address exactly as shown on
    the Settings page (e.g. "http://localhost:20128", no "/v1" suffix) —
    the openai SDK's own convention expects base_url to already include
    "/v1" (mirroring "https://api.openai.com/v1"), so that's appended here
    once, in the one place that builds this client.

    Raises RouterNotConfiguredError if api_key/base_url are blank — there
    is no direct-to-vendor fallback path in this codebase, by policy.
    """
    _require_config(api_key, base_url)
    return AsyncOpenAI(api_key=api_key, base_url=base_url.rstrip("/") + "/v1")


async def list_router_models(api_key: str, base_url: str) -> list[str]:
    """Fetches the model ids actually available through this router right
    now, via GET /v1/models (the openai SDK's client.models.list(), which
    hits exactly that endpoint) — this is the real source of truth for the
    Settings page's model picker: 9router fronts many different models
    under different provider prefixes (gc/gemini-2.5-pro, alicode/qwen3.5-
    plus, ...), none of which are guessable from a hardcoded list.

    Raises RouterNotConfiguredError if api_key/base_url are blank.
    Raises an openai.APIError subclass for a non-2xx/network failure (e.g.
    openai.AuthenticationError for a bad key, openai.APIConnectionError for
    an unreachable host) — callers translate these into operator-facing
    messages themselves (see dashboard/routes/settings.py).
    """
    client = build_router_client(api_key, base_url)
    page = await client.models.list()
    return sorted(m.id for m in page.data)
