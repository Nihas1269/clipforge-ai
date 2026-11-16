from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.config import get_database_url
from app.database import Base, create_db_engine

config = context.config

# Tests set "configure_logger" to False so Alembic does not replace the
# logging handlers of the running process.
if config.config_file_name is not None and config.attributes.get(
    "configure_logger", True
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    # An explicit sqlalchemy.url (for example set by tests) wins; otherwise the
    # shared Settings DATABASE_URL is used.
    return config.get_main_option("sqlalchemy.url") or get_database_url()


def _render_as_batch(url: str) -> bool:
    # SQLite needs batch mode to alter tables in later migrations.
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Emit migration SQL without connecting to a database."""
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_render_as_batch(url),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    url = _database_url()
    engine = create_db_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=_render_as_batch(url),
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
