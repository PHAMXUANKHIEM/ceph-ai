"""Request correlation context shared by API and synchronous Ceph callers."""

from __future__ import annotations

import re
from contextvars import ContextVar, Token
from typing import Mapping

_REQUEST_ID: ContextVar[str | None] = ContextVar("ceph_ai_request_id", default=None)
REQUEST_ID_HEADER = "x-request-id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def set_request_id(value: str | None) -> Token:
    return _REQUEST_ID.set(value)


def reset_request_id(token: Token) -> None:
    _REQUEST_ID.reset(token)


def get_request_id() -> str | None:
    return _REQUEST_ID.get()


def request_id_from_headers(headers: Mapping[str, object] | None) -> str | None:
    """Extract only a safe correlation ID from an untrusted message header."""
    value = (headers or {}).get(REQUEST_ID_HEADER)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _REQUEST_ID_RE.fullmatch(value) else None
