from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config.settings import settings


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
