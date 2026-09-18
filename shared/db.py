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


class Base(DeclarativeBase):
    pass


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_engine(database_url: str | None = None):
    url = database_url or settings.database_url
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
            "pool_size": 3,
            "max_overflow": 0,
            "pool_timeout": 5,
            "pool_pre_ping": True,
        }
    else:
        connect_args = {}
        engine_options = {}
    if not is_sqlite:
        engine_options.update(pool_pre_ping=True, pool_timeout=5, pool_recycle=300)
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
    """Resolve an Action's effective cluster without importing models here.

    ``shared.models`` imports ``Base`` from this module, so using a small
    parameterized SQL lookup avoids a circular import while still mapping
    legacy NULL-cluster incidents to the configured default cluster.
    """
    if not incident_id:
        return None
    row = session.execute(
        text("SELECT cluster_id FROM incidents WHERE id = :incident_id"),
        {"incident_id": incident_id},
    ).first()
    if row is None:
        return None
    cluster_id = row[0]
    if cluster_id:
        return str(cluster_id)
    default_row = session.execute(
        text("SELECT id FROM clusters WHERE is_default = :is_default LIMIT 1"),
        {"is_default": True},
    ).first()
    return str(default_row[0]) if default_row and default_row[0] else None


@event.listens_for(Session, "after_flush")
def _collect_action_state_events(session: Session, _flush_context) -> None:
    """Collect Action transitions before SQLAlchemy expires the objects."""
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
            history = inspected.attrs.status.history
            if not history.has_changes():
                continue
        cluster_id = _action_cluster_id(session, getattr(action, "incident_id", None))
        if not cluster_id:
            logger.warning(
                "action state event skipped: no cluster for action %s",
                getattr(action, "id", "unknown"),
            )
            continue
        pending[(cluster_id, str(action.id))] = str(state)


@event.listens_for(Session, "after_commit")
def _publish_action_state_events(session: Session) -> None:
    """Publish only after the DB commit succeeds; never affect the commit."""
    pending = session.info.pop(_ACTION_STATE_EVENTS_KEY, {})
    if not pending:
        return
    from shared.cluster_events import publish_action_state_event

    for (cluster_id, action_id), status in pending.items():
        try:
            publish_action_state_event(cluster_id, action_id, status)
        except Exception:
            logger.exception(
                "could not publish committed Action state event for %s",
                action_id,
            )


@event.listens_for(Session, "after_rollback")
def _discard_action_state_events(session: Session) -> None:
    session.info.pop(_ACTION_STATE_EVENTS_KEY, None)
