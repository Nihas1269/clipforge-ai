import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import make_url, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import database
from app.core.config import get_database_url
from app.database import (
    Base,
    create_db_engine,
    create_session_factory,
    get_db,
)

SQLITE_IN_MEMORY_URL = "sqlite:///:memory:"


class TrackingSession(Session):
    """Session that records whether close() was called."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


@pytest.fixture
def engine():
    engine = create_db_engine(
        SQLITE_IN_MEMORY_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    yield engine
    engine.dispose()


@pytest.fixture
def tracking_session_factory(engine, monkeypatch):
    factory = sessionmaker(bind=engine, class_=TrackingSession)
    monkeypatch.setattr(database, "_session_factory", factory)
    return factory


def test_get_database_url_reads_environment_variable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", SQLITE_IN_MEMORY_URL)

    assert get_database_url() == SQLITE_IN_MEMORY_URL


def test_get_database_url_raises_when_not_set(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        get_database_url()


def test_create_db_engine_connects_to_sqlite_in_memory(engine):
    assert engine.url.get_backend_name() == "sqlite"

    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_get_engine_uses_database_url_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", SQLITE_IN_MEMORY_URL)
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)

    engine = database.get_engine()
    try:
        # Compare the parsed URL, not its string form: newer SQLAlchemy
        # releases percent-encode ":" in the database name when rendering.
        assert engine.url == make_url(SQLITE_IN_MEMORY_URL)
        assert engine.url.database == ":memory:"
        assert database.get_engine() is engine
        assert database.get_session_factory().kw["bind"] is engine
    finally:
        engine.dispose()


def test_base_metadata_is_available_for_models():
    assert Base.metadata is not None


def test_create_session_factory_creates_bound_session(engine):
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        assert isinstance(session, Session)
        assert session.get_bind() is engine
        assert session.execute(text("SELECT 1")).scalar_one() == 1


def test_get_db_yields_session_and_closes_it(tracking_session_factory):
    generator = get_db()
    session = next(generator)

    assert isinstance(session, TrackingSession)
    assert session.execute(text("SELECT 1")).scalar_one() == 1
    assert session.was_closed is False

    with pytest.raises(StopIteration):
        next(generator)

    assert session.was_closed is True


def test_get_db_closes_session_on_error(tracking_session_factory):
    generator = get_db()
    session = next(generator)

    with pytest.raises(ValueError):
        generator.throw(ValueError("boom"))

    assert session.was_closed is True


def test_get_db_works_as_fastapi_dependency(tracking_session_factory):
    app = FastAPI()
    sessions: list[TrackingSession] = []

    @app.get("/db-check")
    def db_check(db: Session = Depends(get_db)) -> dict[str, int]:
        sessions.append(db)
        return {"result": db.execute(text("SELECT 1")).scalar_one()}

    with TestClient(app) as client:
        response = client.get("/db-check")

    assert response.status_code == 200
    assert response.json() == {"result": 1}
    assert len(sessions) == 1
    assert sessions[0].was_closed is True
