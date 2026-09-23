import asyncio

import bcrypt
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse

from config.settings import (
    DEFAULT_DASHBOARD_PASSWORD_HASH,
    DEFAULT_SESSION_SECRET_KEY,
    Settings,
    settings,
)
from dashboard import app as dashboard_app
from shared import db


_VALID_PRODUCTION_PASSWORD_HASH = bcrypt.hashpw(
    b"test-production-password", bcrypt.gensalt()
).decode()
_VALID_PRODUCTION_SESSION_SECRET = "test-production-session-secret-" + ("x" * 40)


def test_production_database_rejects_sqlite(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")

    with pytest.raises(RuntimeError, match="requires a PostgreSQL DATABASE_URL"):
        db.make_engine("sqlite:///:memory:")


def test_production_database_accepts_postgresql(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")

    engine = db.make_engine("postgresql+psycopg://user:password@db.example/ceph_aiops")
    assert engine.pool.size() == 5
    assert engine.pool._max_overflow == 0
    assert engine.pool.timeout() == 10
    assert engine.pool._recycle == 300
    engine.dispose()


def test_production_dashboard_rejects_dev_security_defaults(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_password_hash", DEFAULT_DASHBOARD_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", DEFAULT_SESSION_SECRET_KEY)

    with pytest.raises(RuntimeError, match="Production security configuration rejected"):
        dashboard_app._warn_if_using_dev_defaults()


@pytest.mark.parametrize(
    ("password_hash", "session_secret", "message"),
    [
        ("", _VALID_PRODUCTION_SESSION_SECRET, "DASHBOARD_PASSWORD_HASH"),
        ("   ", _VALID_PRODUCTION_SESSION_SECRET, "DASHBOARD_PASSWORD_HASH"),
        ("not-a-bcrypt-hash", _VALID_PRODUCTION_SESSION_SECRET, "DASHBOARD_PASSWORD_HASH"),
        (_VALID_PRODUCTION_PASSWORD_HASH, "", "SESSION_SECRET_KEY"),
        (_VALID_PRODUCTION_PASSWORD_HASH, "   ", "SESSION_SECRET_KEY"),
        (_VALID_PRODUCTION_PASSWORD_HASH, "short-secret", "SESSION_SECRET_KEY"),
    ],
)
def test_production_dashboard_rejects_empty_short_or_malformed_security_values(
    monkeypatch, password_hash, session_secret, message,
):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_password_hash", password_hash)
    monkeypatch.setattr(settings, "session_secret_key", session_secret)
    monkeypatch.setattr(settings, "dashboard_trusted_hosts", "admin.example")
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "https://admin.example")

    with pytest.raises(RuntimeError, match=message):
        dashboard_app._warn_if_using_dev_defaults()


def test_settings_rejects_unknown_environment_profile():
    with pytest.raises(ValueError, match="ceph_ai_environment"):
        Settings(_env_file=None, ceph_ai_environment="Production")


def test_production_dashboard_accepts_valid_bcrypt_and_long_secret(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_username", "admin")
    monkeypatch.setattr(settings, "dashboard_password_hash", _VALID_PRODUCTION_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", _VALID_PRODUCTION_SESSION_SECRET)
    monkeypatch.setattr(settings, "dashboard_trusted_hosts", "admin.example")
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "https://admin.example")
    monkeypatch.setattr(settings, "dashboard_trusted_proxy_ips", "")

    dashboard_app._warn_if_using_dev_defaults()


def test_production_dashboard_rejects_missing_host_and_origin_policy(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_password_hash", _VALID_PRODUCTION_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", _VALID_PRODUCTION_SESSION_SECRET)
    monkeypatch.setattr(settings, "dashboard_trusted_hosts", "")
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "")

    with pytest.raises(RuntimeError, match="DASHBOARD_TRUSTED_HOSTS.*DASHBOARD_ALLOWED_ORIGINS"):
        dashboard_app._warn_if_using_dev_defaults()


def test_every_mutation_route_has_authentication_guard():
    from dashboard.app import app

    public_mutations = {"/login", "/logout", "/product/select"}
    unguarded = []
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        if not methods.intersection({"POST", "PUT", "PATCH", "DELETE"}):
            continue
        if route.path in public_mutations:
            continue
        names = []
        dependant = getattr(route, "dependant", None)
        pending = list(getattr(dependant, "dependencies", []) or [])
        while pending:
            dependency = pending.pop()
            call = getattr(dependency, "call", None)
            if call is not None:
                names.append(getattr(call, "__name__", str(call)))
            pending.extend(getattr(dependency, "dependencies", []) or [])
        if not any("login" in name or "admin" in name for name in names):
            unguarded.append(route.path)

    assert unguarded == []


def _request(headers: dict[str, str], scheme: str = "https", session: dict | None = None) -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": scheme,
        "path": "/settings/save",
        "raw_path": b"/settings/save",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 443),
        "session": session or {},
    })


def test_production_mutation_origin_allows_same_origin(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "https://admin.example")
    request = _request({"Host": "admin.example", "Origin": "https://admin.example"})
    assert dashboard_app._mutation_origin_allowed(request)


def test_production_mutation_origin_rejects_cross_site_and_missing_source(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "https://admin.example")
    assert not dashboard_app._mutation_origin_allowed(
        _request({"Host": "admin.example", "Origin": "https://evil.example"})
    )
    assert not dashboard_app._mutation_origin_allowed(_request({"Host": "admin.example"}))


def test_production_csrf_requires_matching_session_cookie_and_header(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "https://admin.example")
    token = "a" * 43
    request = _request(
        {
            "Host": "admin.example",
            "Origin": "https://admin.example",
            "Cookie": f"ceph_ai_csrf={token}",
            "X-CSRF-Token": token,
        },
        session={"_ceph_ai_csrf_token": token},
    )
    assert asyncio.run(dashboard_app._csrf_token_allowed(request))

    request = _request(
        {
            "Host": "admin.example",
            "Origin": "https://admin.example",
            "Cookie": f"ceph_ai_csrf={token}",
            "X-CSRF-Token": "wrong",
        },
        session={"_ceph_ai_csrf_token": token},
    )
    assert not asyncio.run(dashboard_app._csrf_token_allowed(request))


def test_production_html_response_gets_csrf_form_and_script():
    token = "b" * 43
    response = asyncio.run(
        dashboard_app._protect_html_response(
            HTMLResponse('<html><head></head><body><form method="post"></form></body></html>'),
            token,
            secure_cookie=True,
        )
    )
    body = response.body.decode()
    assert 'name="_csrf_token"' in body
    assert "X-CSRF-Token" in body
    assert "Secure" in response.headers["set-cookie"]


def test_production_api_rate_limit_is_shared_by_app_requests(monkeypatch, dashboard_client):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_password_hash", _VALID_PRODUCTION_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", _VALID_PRODUCTION_SESSION_SECRET)
    monkeypatch.setattr(settings, "dashboard_trusted_hosts", "testserver")
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "http://testserver")
    monkeypatch.setattr(settings, "dashboard_api_rate_limit", 2)
    monkeypatch.setattr(settings, "dashboard_api_rate_limit_window_seconds", 60)

    with TestClient(dashboard_app.create_app()) as client:
        first = client.get("/api/system/health")
        second = client.get("/api/system/health")
        third = client.get("/api/system/health")

    assert first.status_code != 429
    assert second.status_code != 429
    assert third.status_code == 429
    assert third.headers["retry-after"] == "60"


def test_production_trusted_host_rejects_unlisted_host(monkeypatch, dashboard_client):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_password_hash", _VALID_PRODUCTION_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", _VALID_PRODUCTION_SESSION_SECRET)
    monkeypatch.setattr(settings, "dashboard_trusted_hosts", "testserver")
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "http://testserver")

    with TestClient(dashboard_app.create_app()) as client:
        response = client.get("/login", headers={"Host": "evil.example"})

    assert response.status_code == 400


def test_production_response_has_security_headers(monkeypatch, dashboard_client):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_password_hash", _VALID_PRODUCTION_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", _VALID_PRODUCTION_SESSION_SECRET)
    monkeypatch.setattr(settings, "dashboard_trusted_hosts", "testserver")
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "http://testserver")

    with TestClient(dashboard_app.create_app()) as client:
        response = client.get("/login")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    set_cookie = response.headers["set-cookie"].lower()
    assert "session=" in set_cookie
    assert "secure" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie


def test_session_cookie_policy_is_secure_only_for_staging_and_production(monkeypatch):
    monkeypatch.setattr(settings, "ceph_ai_environment", "development")
    assert dashboard_app._session_cookie_https_only() is False
    monkeypatch.setattr(settings, "ceph_ai_environment", "staging")
    assert dashboard_app._session_cookie_https_only() is True

    middleware = next(
        item for item in dashboard_app.create_app().user_middleware if item.cls is SessionMiddleware
    )
    assert middleware.kwargs["https_only"] is True
    assert middleware.kwargs["same_site"] == "lax"
    assert middleware.kwargs["max_age"] == 14 * 24 * 60 * 60


def test_trusted_forwarded_https_sets_hsts_and_secure_csrf_cookie(monkeypatch, dashboard_client):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_password_hash", _VALID_PRODUCTION_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", _VALID_PRODUCTION_SESSION_SECRET)
    monkeypatch.setattr(settings, "dashboard_trusted_hosts", "internal.example")
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "https://admin.example")
    monkeypatch.setattr(settings, "dashboard_trusted_proxy_ips", "127.0.0.1")

    async def get_through_trusted_proxy():
        transport = httpx.ASGITransport(
            app=dashboard_app.create_app(),
            client=("127.0.0.1", 12345),
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://internal.example") as client:
            return await client.get(
                "/login",
                headers={
                    "X-Forwarded-Host": "admin.example",
                    "X-Forwarded-Proto": "https",
                },
            )

    response = asyncio.run(get_through_trusted_proxy())

    assert response.status_code == 200
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    set_cookie = response.headers["set-cookie"].lower()
    assert "session=" in set_cookie
    assert "ceph_ai_csrf=" in set_cookie
    assert set_cookie.count("secure") >= 2


def test_effective_scheme_ignores_forwarded_https_from_untrusted_client(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_trusted_proxy_ips", "10.0.0.0/8")
    request = _request(
        {
            "Host": "internal.example",
            "X-Forwarded-Proto": "https",
        },
        scheme="http",
    )
    assert dashboard_app._effective_request_scheme(request) == "http"


def test_malformed_forwarded_scheme_is_rejected_even_from_trusted_proxy(monkeypatch):
    monkeypatch.setattr(settings, "dashboard_trusted_proxy_ips", "127.0.0.1")
    request = _request(
        {
            "Host": "internal.example",
            "X-Forwarded-Proto": "javascript",
        },
        scheme="http",
    )
    assert dashboard_app._forwarded_headers_allowed(request) is False
    assert dashboard_app._effective_request_scheme(request) == "http"


def test_forwarded_headers_from_direct_client_are_rejected(monkeypatch, dashboard_client):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_password_hash", _VALID_PRODUCTION_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", _VALID_PRODUCTION_SESSION_SECRET)
    monkeypatch.setattr(settings, "dashboard_trusted_hosts", "testserver")
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "http://testserver")
    monkeypatch.setattr(settings, "dashboard_trusted_proxy_ips", "")

    with TestClient(dashboard_app.create_app()) as client:
        response = client.get(
            "/login",
            headers={"X-Forwarded-Host": "admin.example", "X-Forwarded-Proto": "https"},
        )

    assert response.status_code == 400


def test_forwarded_headers_are_used_only_from_configured_proxy(monkeypatch, dashboard_client):
    monkeypatch.setattr(settings, "ceph_ai_environment", "production")
    monkeypatch.setattr(settings, "dashboard_password_hash", _VALID_PRODUCTION_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", _VALID_PRODUCTION_SESSION_SECRET)
    monkeypatch.setattr(settings, "dashboard_trusted_hosts", "internal.example")
    monkeypatch.setattr(settings, "dashboard_allowed_origins", "https://admin.example")
    monkeypatch.setattr(settings, "dashboard_trusted_proxy_ips", "10.0.0.0/8")

    request = _request(
        {
            "Host": "internal.example",
            "X-Forwarded-Host": "admin.example",
            "X-Forwarded-Proto": "https",
            "Origin": "https://admin.example",
        }
    )
    assert dashboard_app._forwarded_headers_allowed(request) is False
    # Only a configured source IP/CIDR is accepted as the proxy boundary.
    monkeypatch.setattr(settings, "dashboard_trusted_proxy_ips", "127.0.0.1")
    request = _request(
        {
            "Host": "internal.example",
            "X-Forwarded-Host": "admin.example",
            "X-Forwarded-Proto": "https",
            "Origin": "https://admin.example",
        }
    )
    assert dashboard_app._forwarded_headers_allowed(request) is True
    assert dashboard_app._request_origin(request) == "https://admin.example"


def test_non_production_dashboard_keeps_dev_warning_only(monkeypatch, caplog):
    monkeypatch.setattr(settings, "ceph_ai_environment", "development")
    monkeypatch.setattr(settings, "dashboard_password_hash", DEFAULT_DASHBOARD_PASSWORD_HASH)
    monkeypatch.setattr(settings, "session_secret_key", DEFAULT_SESSION_SECRET_KEY)

    dashboard_app._warn_if_using_dev_defaults()

    assert "DEFAULT dev-only password" in caplog.text
    assert "DEFAULT dev-only SESSION_SECRET_KEY" in caplog.text
