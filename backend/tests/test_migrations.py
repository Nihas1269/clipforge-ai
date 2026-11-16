import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.database import Base, create_db_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

INSERT_USER = text(
    "INSERT INTO users (id, email, hashed_password) "
    "VALUES (:id, :email, :hashed_password)"
)


@pytest.fixture
def database_url(tmp_path):
    """URL of an isolated, empty SQLite database file for one test."""
    return f"sqlite:///{(tmp_path / 'migrations.db').as_posix()}"


@pytest.fixture
def engine(database_url):
    engine = create_db_engine(database_url)
    yield engine
    engine.dispose()


def make_config(database_url: str | None = None) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.attributes["configure_logger"] = False
    if database_url is not None:
        config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_alembic_config_points_at_migrations_directory():
    config = make_config()
    script = ScriptDirectory.from_config(config)

    assert Path(script.dir).resolve() == (BACKEND_DIR / "migrations").resolve()
    # The URL is never hardcoded in alembic.ini; env.py reads Settings.
    assert config.get_main_option("sqlalchemy.url") is None


def test_migrations_have_a_single_head():
    script = ScriptDirectory.from_config(make_config())

    assert len(script.get_heads()) == 1


def test_upgrade_creates_users_table(database_url, engine):
    command.upgrade(make_config(database_url), "head")

    inspector = inspect(engine)
    assert "users" in inspector.get_table_names()
    columns = {column["name"]: column for column in inspector.get_columns("users")}
    assert set(columns) == {
        "id",
        "email",
        "hashed_password",
        "full_name",
        "is_active",
        "is_admin",
        "created_at",
        "updated_at",
    }
    assert columns["full_name"]["nullable"] is True
    assert columns["email"]["nullable"] is False
    assert columns["hashed_password"]["nullable"] is False
    indexes = {index["name"]: index for index in inspector.get_indexes("users")}
    assert indexes["ix_users_email"]["column_names"] == ["email"]
    assert indexes["ix_users_email"]["unique"]


def test_migrated_schema_matches_models(database_url, engine):
    command.upgrade(make_config(database_url), "head")

    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": False}
        )
        assert compare_metadata(context, Base.metadata) == []


def test_migrated_schema_enforces_case_insensitive_unique_email(
    database_url, engine
):
    command.upgrade(make_config(database_url), "head")

    with engine.begin() as connection:
        connection.execute(
            INSERT_USER,
            {"id": uuid.uuid4().hex, "email": "alice@example.com", "hashed_password": "x"},
        )
        row = connection.execute(
            text("SELECT is_active, is_admin, created_at, updated_at FROM users")
        ).one()
    assert bool(row.is_active) is True
    assert bool(row.is_admin) is False
    assert row.created_at is not None
    assert row.updated_at is not None

    for email in ("alice@example.com", "Alice@Example.com"):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    INSERT_USER,
                    {"id": uuid.uuid4().hex, "email": email, "hashed_password": "x"},
                )


def test_downgrade_removes_users_table(database_url, engine):
    config = make_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    assert "users" not in inspect(engine).get_table_names()


INSERT_VIDEO = text(
    "INSERT INTO videos "
    "(id, user_id, original_filename, content_type, size_bytes, storage_key) "
    "VALUES (:id, :user_id, 'talk.mp4', 'video/mp4', 1024, 'key')"
)


def test_upgrade_creates_videos_table(database_url, engine):
    command.upgrade(make_config(database_url), "head")

    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("videos")}
    assert set(columns) == {
        "id",
        "user_id",
        "original_filename",
        "content_type",
        "size_bytes",
        "storage_key",
        "status",
        "error_message",
        "duration_seconds",
        "width",
        "height",
        "fps",
        "created_at",
        "updated_at",
    }
    for name in (
        "user_id",
        "original_filename",
        "content_type",
        "size_bytes",
        "storage_key",
        "status",
        "created_at",
        "updated_at",
    ):
        assert columns[name]["nullable"] is False, name
    for name in ("error_message", "duration_seconds", "width", "height", "fps"):
        assert columns[name]["nullable"] is True, name

    indexes = {index["name"]: index for index in inspector.get_indexes("videos")}
    assert indexes["ix_videos_user_id"]["column_names"] == ["user_id"]
    assert not indexes["ix_videos_user_id"]["unique"]

    (foreign_key,) = inspector.get_foreign_keys("videos")
    assert foreign_key["referred_table"] == "users"
    assert foreign_key["constrained_columns"] == ["user_id"]
    assert foreign_key["options"].get("ondelete") == "CASCADE"


def test_migrated_videos_table_applies_defaults_and_constraints(
    database_url, engine
):
    command.upgrade(make_config(database_url), "head")
    user_id = uuid.uuid4().hex

    with engine.begin() as connection:
        connection.execute(
            INSERT_USER,
            {"id": user_id, "email": "alice@example.com", "hashed_password": "x"},
        )
        connection.execute(INSERT_VIDEO, {"id": uuid.uuid4().hex, "user_id": user_id})
        row = connection.execute(
            text("SELECT status, created_at, updated_at FROM videos")
        ).one()
    assert row.status == "uploaded"
    assert row.created_at is not None
    assert row.updated_at is not None

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO videos (id, user_id, original_filename, "
                    "content_type, size_bytes, storage_key, status) VALUES "
                    "(:id, :user_id, 'a.mp4', 'video/mp4', 1, 'k', 'archived')"
                ),
                {"id": uuid.uuid4().hex, "user_id": user_id},
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO videos (id, user_id, original_filename, "
                    "content_type, size_bytes, storage_key) VALUES "
                    "(:id, :user_id, 'a.mp4', 'video/mp4', -1, 'k')"
                ),
                {"id": uuid.uuid4().hex, "user_id": user_id},
            )


def test_downgrade_of_videos_migration_keeps_users(database_url, engine):
    config = make_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0001")

    table_names = inspect(engine).get_table_names()
    assert "videos" not in table_names
    assert "users" in table_names


def test_env_reads_database_url_from_settings(database_url, engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)

    command.upgrade(make_config(), "head")

    assert "users" in inspect(engine).get_table_names()
