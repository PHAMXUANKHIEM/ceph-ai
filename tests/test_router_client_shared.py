import pytest

from shared.router_client import RouterNotConfiguredError, build_router_client, readable_exception_message


def test_build_router_client_appends_v1_to_base_url():
    # openai SDK's own convention expects base_url to already include "/v1"
    # (mirroring "https://api.openai.com/v1") — build_router_client appends
    # it once so every caller can keep configuring the operator-facing
    # address exactly as shown on the Settings page (no "/v1" suffix).
    client = build_router_client("sk-router-key", "http://localhost:20128")

    assert str(client.base_url) == "http://localhost:20128/v1/"


def test_build_router_client_strips_trailing_slash_before_appending_v1():
    client = build_router_client("sk-router-key", "http://localhost:20128/")

    assert str(client.base_url) == "http://localhost:20128/v1/"


def test_build_router_client_uses_bearer_auth_header():
    client = build_router_client("sk-router-key", "http://localhost:20128")

    assert client.api_key == "sk-router-key"


def test_build_router_client_raises_when_base_url_is_blank():
    # No direct-to-vendor fallback, by policy — a blank base_url is a
    # configuration error to fail loudly on, not a signal to silently call
    # some other endpoint instead.
    with pytest.raises(RouterNotConfiguredError):
        build_router_client("sk-router-key", "")


def test_build_router_client_raises_when_api_key_is_blank():
    with pytest.raises(RouterNotConfiguredError):
        build_router_client("", "http://localhost:20128")


def test_readable_exception_message_prefers_str_when_non_empty():
    assert readable_exception_message(ValueError("bad input")) == "bad input"


def test_readable_exception_message_falls_back_to_class_name_when_str_is_empty():
    # httpx.ReadTimeout() stringifies to "" with no args (verified directly
    # against the real httpx exception) — a blank reason string is worse
    # than none when shown to an operator on a timed-out request.
    class ReadTimeout(Exception):
        pass

    assert readable_exception_message(ReadTimeout()) == "ReadTimeout"
