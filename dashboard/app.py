import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from config.settings import (
    DEFAULT_DASHBOARD_PASSWORD_HASH,
    DEFAULT_SESSION_SECRET_KEY,
    settings,
)
from dashboard import telegram_approval_bot
from dashboard.routes import (
    actions,
    ai_tasks as ai_tasks_routes,
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

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)


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
    telegram_approval_bot.start()
    # Seed the default row and repair stale mirrors left by older versions.
    # The .env-backed Settings form is the source of truth for this row.
    with db.SessionLocal() as session:
        sync_default_cluster_from_settings(session)
    try:
        yield
    finally:
        await codex_app_server.close()


def create_app() -> FastAPI:
    _warn_if_using_dev_defaults()
    application = FastAPI(title="Ceph AIOps Dashboard", lifespan=_lifespan)
    @application.middleware("http")
    async def isolate_product_namespaces(request, call_next):
        """Keep authenticated Ceph and Vitastor sessions in separate UIs."""
        user = request.session.get("user")
        product = request.session.get("product")
        path = request.url.path
        shared_path = path.startswith("/static/") or path in {"/logout", "/login"}
        if user and not shared_path:
            if product == "vitastor" and not path.startswith("/vitastor"):
                from fastapi.responses import RedirectResponse
                return RedirectResponse("/vitastor", status_code=303)
            if product != "vitastor" and path.startswith("/vitastor"):
                from fastapi.responses import RedirectResponse
                return RedirectResponse("/", status_code=303)
        return await call_next(request)

    # Added after the product middleware so SessionMiddleware wraps it and
    # `request.session` is available inside the namespace guard.
    application.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)
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
    application.include_router(ai_tasks_routes.router)
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
