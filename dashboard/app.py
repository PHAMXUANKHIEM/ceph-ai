import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from config.settings import (
    DEFAULT_DASHBOARD_PASSWORD_HASH,
    DEFAULT_SESSION_SECRET_KEY,
    settings,
)
from dashboard.routes import (
    actions,
    audit,
    auth,
    chat,
    incidents,
    maintenance,
    nodes,
    settings as settings_routes,
)
from dashboard.ws import router as ws_router

STATIC_DIR = Path(__file__).resolve().parent / "static"
logger = logging.getLogger(__name__)


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


def create_app() -> FastAPI:
    _warn_if_using_dev_defaults()
    application = FastAPI(title="Ceph AIOps Dashboard")
    application.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    application.include_router(auth.router)
    application.include_router(incidents.router)
    application.include_router(nodes.router)
    application.include_router(settings_routes.router)
    application.include_router(maintenance.router)
    application.include_router(actions.router)
    application.include_router(audit.router)
    application.include_router(chat.router)
    application.include_router(ws_router)
    return application


app = create_app()
