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
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    elif url.startswith("postgresql"):
        connect_args = {"options": "-c idle_in_transaction_session_timeout=60000"}
    else:
        connect_args = {}
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        # SQLite ignores FK constraints by default — without this, the
        # ForeignKeyConstraint on Action.incident_id (AD-1) is purely
        # decorative and never actually rejects an orphaned Action row.
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
