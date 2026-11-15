from collections.abc import Iterator
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_database_url


class Base(DeclarativeBase):
    """Declarative base class for all database models."""


def create_db_engine(database_url: str, **engine_kwargs: Any) -> Engine:
    """Create a SQLAlchemy engine for the given database URL."""
    engine_kwargs.setdefault("pool_pre_ping", True)
    return create_engine(database_url, **engine_kwargs)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a session factory bound to the given engine."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# The engine and session factory are created lazily on first use so that
# importing the application (e.g. for the /health endpoint) does not require
# DATABASE_URL to be set or a database server to be running.
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the application engine, creating it from DATABASE_URL if needed."""
    global _engine
    if _engine is None:
        _engine = create_db_engine(get_database_url())
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the application session factory, creating it if needed."""
    global _session_factory
    if _session_factory is None:
        _session_factory = create_session_factory(get_engine())
    return _session_factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency that provides a database session and closes it after use."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
