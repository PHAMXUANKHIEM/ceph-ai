from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Iterator

import logging

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config.settings import settings


logger = logging.getLogger(__name__)
_ACTION_STATE_EVENTS_KEY = "ceph_ai_action_state_events"
_CLUSTER_STATE_EVENTS_KEY = "ceph_ai_cluster_state_events"


def _validate_production_database_url(url: str) -> None:
    environment = str(getattr(settings, "ceph_ai_environment", "development")).lower()
    if environment != "production":
        return
    normalized = url.lower()
    if not normalized.startswith((
        "postgresql://", "postgres://", "postgresql+psycopg://", "postgresql+psycopg2://",
    )):
        raise RuntimeError(
            "Production requires a PostgreSQL DATABASE_URL; SQLite is only "
            "allowed for development, test, or lab environments."
        )


class Base(DeclarativeBase):
    pass


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(database_url: str | None = None):
    url = database_url or settings.database_url
    _validate_production_database_url(url)
    is_sqlite = url.startswith("sqlite")
    if is_sqlite:
        connect_args = {"check_same_thread": False}
        engine_options = {}
    elif url.startswith("postgresql"):
        # Each service is a separate process. SQLAlchemy defaults (five
        # pooled connections plus ten overflow connections per process) can
        # exhaust a small managed PostgreSQL instance during a restart storm,
        # preventing Dashboard startup at pg_catalog.version().
        connect_args = {"connect_timeout": 5}
        engine_options = {
            "pool_size": max(1, int(settings.database_pool_size)),
            "max_overflow": 0,
            "pool_timeout": max(1, int(settings.database_pool_timeout_seconds)),
            "pool_pre_ping": True,
        }
    else:
        connect_args = {}
        engine_options = {}
    if not is_sqlite:
        engine_options.update(
            pool_pre_ping=True,
            pool_timeout=max(1, int(settings.database_pool_timeout_seconds)),
            pool_recycle=300,
        )
    engine = create_engine(url, connect_args=connect_args, **engine_options)
    if url.startswith("sqlite"):
        # SQLite ignores FK constraints by default — without this, the
        # ForeignKeyConstraint on Action.incident_id (AD-1) is purely
        # decorative and never actually rejects an orphaned Action row.
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


engine = make_engine()
_default_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Telegram's central gateway can serve more than one independently-operated
# Ceph deployment.  Keep the existing process-wide database as the default,
# but allow one request/task to route all SessionLocal() calls to a selected
# database without changing the hundreds of existing call sites.
_database_url_override: ContextVar[str | None] = ContextVar(
    "ceph_ai_database_url_override", default=None
)


@contextmanager
def use_database(database_url: str | None) -> Iterator[None]:
    """Route SessionLocal() calls in the current context to ``database_url``.

    Context variables flow through asyncio tasks and ``asyncio.to_thread``;
    unrelated watcher/worker threads continue to use the normal configured
    database.  Passing ``None`` explicitly restores the default database.
    """
    token = _database_url_override.set(database_url)
    try:
        yield
    finally:
        _database_url_override.reset(token)


def current_database_url() -> str | None:
    """Return the database URL selected for the current execution context."""
    return _database_url_override.get()


@lru_cache(maxsize=16)
def session_factory_for_url(database_url: str):
    """Return a cached SQLAlchemy session factory for a federated database."""
    if not database_url:
        return _default_session_local
    return sessionmaker(
        bind=make_engine(database_url), autoflush=False, autocommit=False
    )


class _RoutedSessionLocal:
    """Backward-compatible callable facade over the default sessionmaker."""

    def __call__(self, *args, **kwargs):
        database_url = _database_url_override.get()
        return session_factory_for_url(database_url)(*args, **kwargs)

    def __getattr__(self, name):
        # Preserve the small sessionmaker API used by migrations/tests and
        # make the default behavior indistinguishable from the old object.
        return getattr(_default_session_local, name)


SessionLocal = _RoutedSessionLocal()


def _action_cluster_id(session: Session, incident_id: str | None) -> str | None:
    """Resolve an Action's effective cluster without importing models here."""
    if not incident_id:
        return None
    row = session.execute(
        text("SELECT cluster_id FROM incidents WHERE id = :incident_id"),
        {"incident_id": incident_id},
    ).first()
    if row is None:
        return None
    if row[0]:
        return str(row[0])
    default_row = session.execute(
        text("SELECT id FROM clusters WHERE is_default = :is_default LIMIT 1"),
        {"is_default": True},
    ).first()
    return str(default_row[0]) if default_row and default_row[0] else None


@event.listens_for(Session, "after_flush")
def _collect_action_state_events(session: Session, _flush_context) -> None:
    pending = session.info.setdefault(_ACTION_STATE_EVENTS_KEY, {})
    candidates = set(session.new).union(session.dirty)
    for action in candidates:
        if action.__class__.__name__ != "Action":
            continue
        state = getattr(action, "status", None)
        if state is None:
            continue
        if action in session.dirty:
            inspected = inspect(action)
            if not inspected.attrs.status.history.has_changes():
                continue
        cluster_id = _action_cluster_id(session, getattr(action, "incident_id", None))
        if not cluster_id:
            logger.warning("action state event skipped: no cluster for action %s", getattr(action, "id", "unknown"))
            continue
        pending[(cluster_id, str(action.id))] = str(state)


def _sections_for_resolved_incident(incident: object) -> tuple[str, ...]:
    """Map a post-check-confirmed Incident to bounded snapshot sections.

    The Worker/Watcher owns the authoritative refresh. This hook only emits a
    small invalidation hint after the database commit that records RESOLVED;
    it never claims that a command succeeded before the post-check.
    """
    code = str(getattr(incident, "ceph_code", "") or "").upper()
    sections = {"health", "status"}
    if "POOL" in code:
        sections.add("pools")
    if "PG" in code:
        sections.add("pgs")
    if "OSD" in code or "NODE" in code or "HOST" in code:
        sections.add("nodes")
    return tuple(section for section in ("health", "status", "pools", "pgs", "crush", "nodes") if section in sections)


@event.listens_for(Session, "after_flush")
def _collect_cluster_state_events(session: Session, _flush_context) -> None:
    pending = session.info.setdefault(_CLUSTER_STATE_EVENTS_KEY, {})
    for incident in set(session.dirty):
        if incident.__class__.__name__ != "Incident":
            continue
        inspected = inspect(incident)
        if not inspected.attrs.status.history.has_changes():
            continue
        if str(getattr(incident, "status", "")) not in {"RESOLVED", "IncidentStatus.RESOLVED"}:
            continue
        cluster_id = str(getattr(incident, "cluster_id", "") or "").strip()
        if cluster_id:
            pending[cluster_id] = _sections_for_resolved_incident(incident)


@event.listens_for(Session, "after_commit")
def _publish_action_state_events(session: Session) -> None:
    pending = session.info.pop(_ACTION_STATE_EVENTS_KEY, {})
    if not pending:
        return
    from shared.cluster_events import publish_action_state_event
    for (cluster_id, action_id), status in pending.items():
        try:
            publish_action_state_event(cluster_id, action_id, status)
        except Exception:
            logger.exception("could not publish committed Action state for %s", action_id)


@event.listens_for(Session, "after_commit")
def _publish_cluster_state_events(session: Session) -> None:
    pending = session.info.pop(_CLUSTER_STATE_EVENTS_KEY, {})
    if not pending:
        return
    from shared.cluster_events import publish_event
    for cluster_id, sections in pending.items():
        try:
            publish_event(cluster_id, "snapshot_changed", sections=sections)
        except Exception:
            logger.exception("could not publish post-check snapshot invalidation for %s", cluster_id)


@event.listens_for(Session, "after_rollback")
def _discard_action_state_events(session: Session) -> None:
    session.info.pop(_ACTION_STATE_EVENTS_KEY, None)
    session.info.pop(_CLUSTER_STATE_EVENTS_KEY, None)
