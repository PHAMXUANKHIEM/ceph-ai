from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

from config.settings import settings
from shared.db import Base
from shared import models  # noqa: F401 - registers Incident on Base.metadata

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
#
# disable_existing_loggers=False (NOT the fileConfig default): this env.py
# runs in-process, not just from the standalone `alembic` CLI — Alembic's
# own command.upgrade() is called directly by tests/test_migrations.py in
# the same pytest process as every other module's already-created loggers
# (e.g. worker/llm/router_client.py's). fileConfig()'s default of `True`
# permanently disables any logger not listed in alembic.ini's [loggers]
# section for the rest of that process — verified live: this silently
# broke tests/test_router_client.py's two logger.warning() assertions
# whenever they ran in the same session AFTER test_migrations.py (alphabetic
# collection order), a real, deterministic bug (not flakiness), not
# something a caplog fixture could work around on its own.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# DATABASE_URL comes from config/settings.py (env-driven), never hardcoded here (AD-8).
# `%` is escaped as `%%` because Config.set_main_option() stores it through a
# stdlib ConfigParser, which treats a bare `%` as the start of a `%(name)s`
# interpolation reference — a password containing ANY percent-encoded
# character (`%40`, `%3B`, ... i.e. almost any real generated password)
# raises `ValueError: invalid interpolation syntax` right here otherwise.
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
