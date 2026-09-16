"""Request correlation context shared by API and synchronous Ceph callers."""

from __future__ import annotations

from contextvars import ContextVar, Token

_REQUEST_ID: ContextVar[str | None] = ContextVar("ceph_ai_request_id", default=None)


def set_request_id(value: str) -> Token:
    return _REQUEST_ID.set(value)


def reset_request_id(token: Token) -> None:
    _REQUEST_ID.reset(token)


def get_request_id() -> str | None:
    return _REQUEST_ID.get()
