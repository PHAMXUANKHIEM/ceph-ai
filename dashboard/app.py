import asyncio
import html
import json
import logging
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response
from sqlalchemy.exc import SQLAlchemyError

from config.settings import (
    DEFAULT_DASHBOARD_PASSWORD_HASH,
    DEFAULT_SESSION_SECRET_KEY,
    settings,
)
from dashboard import telegram_approval_bot, telegram_chat
from dashboard.cache_warmup import start as start_cache_warmup
from dashboard.routes import (
    actions,
    ai_cost as ai_cost_routes,
    ai_learning as ai_learning_routes,
    auth,
    backups,
    block_storage,
    bucket_access_log,
    object_storage,
    object_storage_users,
    capability_matrix as capability_matrix_routes,
    capacity_forecast as capacity_forecast_routes,
    disk_risk as disk_risk_routes,
    performance_rca as performance_rca_routes,
    log_intelligence as log_intelligence_routes,
    chat,
    cinder_backups,
    clusters as clusters_routes,
    convert_cluster,
    crush_map,
    delete_cluster,
    deploy_cluster,
    incidents,
    maintenance,
    nodes,
    openstack,
    patch,
    pgs,
    restore_cluster,
    runbooks as runbooks_routes,
    settings as settings_routes,
    system_health as system_health_routes,
    synthetic_incidents as synthetic_incidents_routes,
    telegram_alerts,
    upgrade,
    users,
    vitastor,
    vitastor_actions,
    vitastor_chat,
    vitastor_clusters,
    vitastor_lifecycle,
    vitastor_users,
    volumes,
)
from dashboard.ws import router as ws_router
from shared import db
from shared.codex_app_server import codex_app_server
from shared.clusters import sync_default_cluster_from_settings
from shared.logging_redaction import install_logging_redaction
from shared.api_observability import record_request
from shared.api_rate_limit import RateLimitStoreUnavailable, allow_api_request
from shared.request_context import reset_request_id, set_request_id

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CSRF_SESSION_KEY = "_ceph_ai_csrf_token"
_CSRF_COOKIE_NAME = "ceph_ai_csrf"
_UNSAFE_METHODS = frozenset(("POST", "PUT", "PATCH", "DELETE"))
install_logging_redaction()


class _CachedStaticFiles(StaticFiles):
    """Static assets are content-versioned via ``?v=STATIC_VERSION`` on every
    URL (dashboard/templating.py appends it site-wide), so a given URL's bytes
    never change within a running Dashboard process. That makes them safe to
    cache immutably — a browser then serves the 120KB+ stylesheet and every JS
    bundle from disk on subsequent navigations instead of re-downloading them,
    and only re-fetches after a deploy (which mints a fresh ``?v=``). Without
    this, StaticFiles sends no Cache-Control at all and every page load re-pulls
    every asset."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _warn_if_using_dev_defaults() -> None:
    if settings.ceph_ai_environment == "production":
        errors = []
        if settings.dashboard_password_hash == DEFAULT_DASHBOARD_PASSWORD_HASH:
            errors.append("DASHBOARD_PASSWORD_HASH is still the dev default")
        if settings.session_secret_key == DEFAULT_SESSION_SECRET_KEY:
            errors.append("SESSION_SECRET_KEY is still the dev default")
        if not _configured_values(settings.dashboard_trusted_hosts):
            errors.append("DASHBOARD_TRUSTED_HOSTS is not configured")
        if not _configured_values(settings.dashboard_allowed_origins):
            errors.append("DASHBOARD_ALLOWED_ORIGINS is not configured")
        if errors:
            raise RuntimeError(
                "Production security configuration rejected: " + "; ".join(errors)
            )
        return
    if settings.dashboard_password_hash == DEFAULT_DASHBOARD_PASSWORD_HASH:
        logger.warning(
            "Dashboard is using the DEFAULT dev-only password (admin/admin). "
            "Set DASHBOARD_PASSWORD_HASH before exposing this beyond localhost."
        )
    if settings.session_secret_key == DEFAULT_SESSION_SECRET_KEY:
        logger.warning(
            "Dashboard is using the DEFAULT dev-only SESSION_SECRET_KEY. "
            "Set a real random value before exposing this beyond localhost."
        )


def _configured_values(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated security setting without accepting blanks."""
    return tuple(dict.fromkeys(value.strip() for value in str(raw or "").split(",") if value.strip()))


def _source_origin(request) -> str | None:
    """Return the Origin/Referer origin supplied by a browser request."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return None
    parsed = urlsplit(source)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _request_origin(request) -> str | None:
    """Return the origin represented by the request Host header.

    Forwarded headers are intentionally ignored: accepting them without a
    configured, trusted proxy boundary would let a client forge the origin.
    Deployments behind a reverse proxy must list their public origin in
    DASHBOARD_ALLOWED_ORIGINS.
    """
    host = request.headers.get("host", "").strip().lower()
    if not host:
        return None
    scheme = request.url.scheme.lower()
    if scheme not in {"http", "https"}:
        return None
    return f"{scheme}://{host}"


def _mutation_origin_allowed(request) -> bool:
    """Enforce same-origin or explicitly configured-origin mutations."""
    source_origin = _source_origin(request)
    if source_origin is None:
        return False
    allowed_origins = {value.lower().rstrip("/") for value in _configured_values(settings.dashboard_allowed_origins)}
    return source_origin in allowed_origins or source_origin == _request_origin(request)


def _ensure_csrf_token(request) -> str:
    token = request.session.get(_CSRF_SESSION_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.session[_CSRF_SESSION_KEY] = token
    return token


async def _submitted_form_csrf_token(request) -> str | None:
    """Read a form token and replay the body for FastAPI's route parser."""
    content_type = request.headers.get("content-type", "").lower()
    if not (content_type.startswith("application/x-www-form-urlencoded") or content_type.startswith("multipart/form-data")):
        return None
    body = await request.body()

    async def replay_body():
        return {"type": "http.request", "body": body, "more_body": False}

    # BaseHTTPMiddleware may hand the downstream route a separate receive
    # callable; replay the buffered body so Form/File parameters still work.
    request._receive = replay_body
    if content_type.startswith("application/x-www-form-urlencoded"):
        try:
            values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError:
            return None
        return values.get("_csrf_token", [None])[0]
    try:
        form = await request.form()
    except Exception:
        return None
    value = form.get("_csrf_token")
    return str(value) if value is not None else None


async def _csrf_token_allowed(request) -> bool:
    expected = request.session.get(_CSRF_SESSION_KEY)
    cookie_token = request.cookies.get(_CSRF_COOKIE_NAME)
    submitted = request.headers.get("x-csrf-token") or await _submitted_form_csrf_token(request)
    if not all(isinstance(value, str) for value in (expected, cookie_token, submitted)):
        return False
    return (
        secrets.compare_digest(expected, cookie_token)
        and secrets.compare_digest(expected, submitted)
    )


async def _protect_html_response(response, token: str, *, secure_cookie: bool):
    """Add CSRF affordances to HTML and expose the token to same-origin JS."""
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type:
        response.set_cookie(
            _CSRF_COOKIE_NAME,
            token,
            max_age=86400,
            httponly=False,
            secure=secure_cookie,
            samesite="lax",
            path="/",
        )
        return response

    if hasattr(response, "body_iterator"):
        raw_body = b"".join([chunk async for chunk in response.body_iterator])
    else:
        raw_body = response.body or b""
    body = raw_body.decode("utf-8", errors="replace")
    escaped_token = html.escape(token, quote=True)
    hidden_input = f'<input type="hidden" name="_csrf_token" value="{escaped_token}">'
    body = re.sub(
        r'(<form\b(?=[^>]*\bmethod\s*=\s*["\']?post["\']?)[^>]*>)',
        lambda match: match.group(1) + hidden_input,
        body,
        flags=re.IGNORECASE,
    )
    token_json = json.dumps(token)
    csrf_script = f"""<script>
(function () {{
  const token = {token_json};
  const unsafe = new Set(["POST", "PUT", "PATCH", "DELETE"]);
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {{
    const options = init ? Object.assign({{}}, init) : {{}};
    const method = String(options.method || (input && input.method) || "GET").toUpperCase();
    let url;
    try {{ url = new URL(typeof input === "string" ? input : input.url, window.location.href); }}
    catch (_) {{ return nativeFetch(input, options); }}
    if (unsafe.has(method) && url.origin === window.location.origin) {{
      const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
      headers.set("X-CSRF-Token", token);
      options.headers = headers;
    }}
    return nativeFetch(input, options);
  }};
  document.addEventListener("submit", function (event) {{
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !unsafe.has((form.method || "get").toUpperCase())) return;
    if (!form.querySelector('input[name="_csrf_token"]')) {{
      const input = document.createElement("input");
      input.type = "hidden"; input.name = "_csrf_token"; input.value = token;
      form.appendChild(input);
    }}
  }});
}})();
</script>"""
    if re.search(r"</head>", body, flags=re.IGNORECASE):
        body = re.sub(r"</head>", csrf_script + "</head>", body, count=1, flags=re.IGNORECASE)
    else:
        body = csrf_script + body
    headers = dict(response.headers)
    headers.pop("content-length", None)
    protected = Response(
        content=body.encode("utf-8"),
        status_code=response.status_code,
        headers=headers,
        background=response.background,
    )
    protected.set_cookie(
        _CSRF_COOKIE_NAME,
        token,
        max_age=86400,
        httponly=False,
        secure=secure_cookie,
        samesite="lax",
        path="/",
    )
    return protected


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 2026-08-05: starts dashboard/telegram_approval_bot.py's 2 background
    # daemon threads exactly once per process — see that module's own
    # docstring for what they do and why they live here (Dashboard
    # startup) rather than Worker/Watcher. Idempotent on its own
    # (telegram_approval_bot.start() no-ops if already started), which
    # matters because FastAPI's TestClient re-enters this lifespan on
    # every `with TestClient(app) as client:` block across this project's
    # whole test suite, all sharing the same cached `app` singleton.
    # Seed the default row and repair stale mirrors left by older versions.
    # The .env-backed Settings form is the source of truth for this row.
    try:
        with db.SessionLocal() as session:
            sync_default_cluster_from_settings(session)
    except SQLAlchemyError:
        # Seeding the mirror is recoverable. Do not crash-loop the whole
        # Dashboard because a transient PostgreSQL saturation/timeout occurs
        # during deployment; database routes will report their normal 503.
        logger.exception("Dashboard startup: unable to sync default cluster from settings")
    start_cache_warmup()
    dashboard_loop = asyncio.get_running_loop()
    telegram_listener_enabled = settings.telegram_listener_enabled
    if telegram_listener_enabled:
        telegram_chat.set_dashboard_loop(dashboard_loop)
        telegram_approval_bot.start()
    try:
        yield
    finally:
        if telegram_listener_enabled:
            telegram_chat.clear_dashboard_loop(dashboard_loop)
        await codex_app_server.close()


def create_app() -> FastAPI:
    _warn_if_using_dev_defaults()
    application = FastAPI(title="Ceph AIOps Dashboard", lifespan=_lifespan)
    @application.middleware("http")
    async def observe_api_request(request, call_next):
        """Attach one correlation ID and bounded timing data to each request."""
        supplied = request.headers.get("x-request-id", "").strip()
        request_id = supplied if _REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex
        request.state.request_id = request_id
        request_context_token = set_request_id(request_id)
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            record_request(
                request.method,
                request.url.path,
                500,
                (time.monotonic() - started) * 1000,
                request_id,
            )
            raise
        finally:
            reset_request_id(request_context_token)
        response.headers["X-Request-ID"] = request_id
        if not request.url.path.startswith("/static/"):
            record_request(
                request.method,
                request.url.path,
                response.status_code,
                (time.monotonic() - started) * 1000,
                request_id,
            )
        return response

    @application.middleware("http")
    async def isolate_product_namespaces(request, call_next):
        """Keep authenticated Ceph and Vitastor sessions in separate UIs."""
        # A stale client-side navigation state once generated protocol-relative
        # paths such as ``//object-storage/buckets``. Starlette treats that as
        # a different route and returns 404, which made the Buckets screen look
        # like it had disappeared. Normalize only this application namespace;
        # do not rewrite arbitrary paths or external-style URLs.
        raw_path = request.scope.get("path", "")
        if raw_path.startswith("//object-storage/"):
            request.scope["path"] = raw_path[1:]
        user = request.session.get("user")
        product = request.session.get("product")
        path = request.url.path
        shared_path = path.startswith("/static/") or path in {"/logout", "/login"}
        is_api_request = path == "/api" or path.startswith("/api/")
        if settings.ceph_ai_environment == "production" and is_api_request:
            client_key = request.client.host if request.client else "unknown"
            try:
                allowed = allow_api_request(
                    client_key,
                    limit=settings.dashboard_api_rate_limit,
                    window_seconds=settings.dashboard_api_rate_limit_window_seconds,
                )
            except RateLimitStoreUnavailable:
                return JSONResponse(
                    {"detail": "API rate-limit store không khả dụng"},
                    status_code=503,
                )
            if not allowed:
                return JSONResponse(
                    {"detail": "Quá nhiều yêu cầu API — thử lại sau ít phút"},
                    status_code=429,
                    headers={"Retry-After": str(settings.dashboard_api_rate_limit_window_seconds)},
                )
        csrf_token = _ensure_csrf_token(request) if settings.ceph_ai_environment == "production" else None
        if (
            settings.ceph_ai_environment == "production"
            and request.method in _UNSAFE_METHODS
            and not _mutation_origin_allowed(request)
        ):
            return JSONResponse({"detail": "Cross-site mutation bị từ chối"}, status_code=403)
        if (
            settings.ceph_ai_environment == "production"
            and request.method in _UNSAFE_METHODS
            and not await _csrf_token_allowed(request)
        ):
            return JSONResponse({"detail": "CSRF token không hợp lệ hoặc đã thiếu"}, status_code=403)
        if user and request.method in _UNSAFE_METHODS and path.startswith("/vitastor"):
            # Keep the product-specific guard for non-production environments,
            # where the global production policy above is intentionally off.
            expected_host = request.headers.get("host", "").lower()
            source = request.headers.get("origin") or request.headers.get("referer")
            if source and urlsplit(source).netloc.lower() != expected_host:
                return JSONResponse({"detail": "Cross-site Vitastor request bị từ chối"}, status_code=403)
        if user and not shared_path:
            if product == "vitastor" and not path.startswith("/vitastor"):
                from fastapi.responses import RedirectResponse
                return RedirectResponse("/vitastor", status_code=303)
            if product != "vitastor" and path.startswith("/vitastor"):
                from fastapi.responses import RedirectResponse
                return RedirectResponse("/", status_code=303)
        response = await call_next(request)
        if csrf_token is not None:
            return await _protect_html_response(
                response,
                csrf_token,
                secure_cookie=request.url.scheme == "https",
            )
        return response

    # Added after the product middleware so SessionMiddleware wraps it and
    # `request.session` is available inside the namespace guard.
    application.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax")
    if settings.ceph_ai_environment == "production":
        application.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(_configured_values(settings.dashboard_trusted_hosts)),
        )
    # Outermost middleware (added last): compress every response over ~500B —
    # the 129KB stylesheet, large Jinja pages (settings.html ~52KB) and the
    # telemetry JSON APIs all shrink ~70-80% over the wire, the single biggest
    # first-paint latency win and pure gain (browsers negotiate it via
    # Accept-Encoding; nothing here changes semantically).
    application.add_middleware(GZipMiddleware, minimum_size=500)
    application.mount("/static", _CachedStaticFiles(directory=str(STATIC_DIR)), name="static")
    application.include_router(auth.router)
    application.include_router(system_health_routes.router)
    application.include_router(synthetic_incidents_routes.router)
    application.include_router(runbooks_routes.router)
    application.include_router(incidents.router)
    application.include_router(nodes.router)
    application.include_router(block_storage.router)
    application.include_router(openstack.router)
    application.include_router(settings_routes.router)
    application.include_router(maintenance.router)
    application.include_router(actions.router)
    application.include_router(ai_cost_routes.router)
    application.include_router(ai_learning_routes.router)
    application.include_router(chat.router)
    application.include_router(upgrade.router)
    application.include_router(deploy_cluster.router)
    application.include_router(delete_cluster.router)
    application.include_router(convert_cluster.router)
    application.include_router(patch.router)
    application.include_router(users.router)
    application.include_router(volumes.router)
    application.include_router(pgs.router)
    application.include_router(backups.router)
    application.include_router(cinder_backups.router)
    application.include_router(restore_cluster.router)
    application.include_router(bucket_access_log.router)
    application.include_router(object_storage.router)
    application.include_router(object_storage_users.router)
    application.include_router(telegram_alerts.router)
    application.include_router(crush_map.router)
    application.include_router(capability_matrix_routes.router)
    application.include_router(capacity_forecast_routes.router)
    application.include_router(performance_rca_routes.router)
    application.include_router(disk_risk_routes.router)
    application.include_router(log_intelligence_routes.router)
    application.include_router(clusters_routes.router)
    application.include_router(vitastor.router)
    application.include_router(vitastor_actions.router)
    application.include_router(vitastor_chat.router)
    application.include_router(vitastor_clusters.router)
    application.include_router(vitastor_lifecycle.router)
    application.include_router(vitastor_users.router)
    application.include_router(ws_router)
    return application


app = create_app()
