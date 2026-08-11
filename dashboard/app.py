import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from config.settings import (
    DEFAULT_DASHBOARD_PASSWORD_HASH,
    DEFAULT_SESSION_SECRET_KEY,
    settings,
)
from dashboard import telegram_approval_bot
from dashboard.routes import (
    actions,
    auth,
    backups,
    bucket_access_log,
    chat,
    clusters as clusters_routes,
    convert_cluster,
    crush_map,
    delete_cluster,
    deploy_cluster,
    incidents,
    maintenance,
    nodes,
    patch,
    pgs,
    restore_cluster,
    settings as settings_routes,
    telegram_alerts,
    test_runner,
    upgrade,
    users,
    volumes,
)
from dashboard.ws import router as ws_router
from shared import db
from shared.clusters import ensure_default_cluster

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
    # Multi-cluster observability Phase 1 — idempotent, same "safe to
    # re-enter on every TestClient block" property as telegram_approval_bot
    # .start() above (shared/clusters.py::ensure_default_cluster re-queries
    # rather than blindly inserting).
    with db.SessionLocal() as session:
        ensure_default_cluster(session)
    # Epic 10 Story 10.8 — rebuilds dashboard/routes/test_runner.py's
    # in-memory `_run_states` from the durable `TestRunResult` table so a
    # Dashboard restart doesn't wipe upgrade-test-runner results back to a
    # blank slate. Idempotent, same TestClient-re-entry posture as the two
    # calls above (a fresh load just overwrites _run_states again).
    test_runner._load_persisted_run_states()
    yield


def create_app() -> FastAPI:
    _warn_if_using_dev_defaults()
    application = FastAPI(title="Ceph AIOps Dashboard", lifespan=_lifespan)
    application.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    application.include_router(auth.router)
    application.include_router(incidents.router)
    application.include_router(nodes.router)
    application.include_router(settings_routes.router)
    application.include_router(maintenance.router)
    application.include_router(actions.router)
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
    application.include_router(telegram_alerts.router)
    application.include_router(test_runner.router)
    application.include_router(crush_map.router)
    application.include_router(clusters_routes.router)
    application.include_router(ws_router)
    return application


app = create_app()
