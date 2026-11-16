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
        "email_verified_at",
        "token_version",
    }
    assert columns["full_name"]["nullable"] is True
    assert columns["email_verified_at"]["nullable"] is True
    assert columns["token_version"]["nullable"] is False
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
            text(
                "SELECT is_active, is_admin, created_at, updated_at, "
                "email_verified_at, token_version FROM users"
            )
        ).one()
    assert bool(row.is_active) is True
    assert bool(row.is_admin) is False
    assert row.created_at is not None
    assert row.updated_at is not None
    assert row.email_verified_at is None
    assert row.token_version == 0

    for email in ("alice@example.com", "Alice@Example.com"):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    INSERT_USER,
                    {"id": uuid.uuid4().hex, "email": email, "hashed_password": "x"},
                )


def test_upgrade_creates_auth_tokens_table(database_url, engine):
    command.upgrade(make_config(database_url), "head")

    inspector = inspect(engine)
    columns = {
        column["name"]: column for column in inspector.get_columns("auth_tokens")
    }
    assert set(columns) == {
        "id",
        "user_id",
        "purpose",
        "token_hash",
        "created_at",
        "expires_at",
        "used_at",
    }
    assert columns["used_at"]["nullable"] is True
    assert columns["expires_at"]["nullable"] is False
    indexes = {index["name"]: index for index in inspector.get_indexes("auth_tokens")}
    assert indexes["ix_auth_tokens_token_hash"]["unique"]
    assert indexes["ix_auth_tokens_user_id"]["column_names"] == ["user_id"]
    foreign_keys = inspector.get_foreign_keys("auth_tokens")
    assert foreign_keys[0]["referred_table"] == "users"


def test_downgrade_one_step_removes_auth_tokens_and_keeps_users(database_url, engine):
    config = make_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0002")

    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    assert "auth_tokens" not in table_names
    # Only the auth migration is rolled back; the earlier videos migration stays.
    assert "videos" in table_names
    columns = {column["name"] for column in inspector.get_columns("users")}
    assert "token_version" not in columns
    assert "email_verified_at" not in columns
    # The lowercase CHECK constraint survives the column changes.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                INSERT_USER,
                {"id": uuid.uuid4().hex, "email": "Alice@Example.com", "hashed_password": "x"},
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


def test_upgrade_creates_processing_jobs_table(database_url, engine):
    command.upgrade(make_config(database_url), "head")

    inspector = inspect(engine)
    columns = {
        column["name"]: column for column in inspector.get_columns("processing_jobs")
    }
    assert set(columns) == {
        "id",
        "user_id",
        "video_id",
        "job_type",
        "status",
        "idempotency_key",
        "active_key",
        "celery_task_id",
        "attempts",
        "max_attempts",
        "error_message",
        "payload",
        "request_id",
        "created_at",
        "started_at",
        "finished_at",
    }
    for name in ("job_type", "status", "attempts", "max_attempts", "created_at"):
        assert columns[name]["nullable"] is False, name
    for name in (
        "user_id",
        "video_id",
        "celery_task_id",
        "error_message",
        "payload",
        "started_at",
        "finished_at",
    ):
        assert columns[name]["nullable"] is True, name

    indexes = {
        index["name"]: index for index in inspector.get_indexes("processing_jobs")
    }
    assert indexes["ix_processing_jobs_active_key"]["unique"]
    assert not indexes["ix_processing_jobs_user_id"]["unique"]
    referred = {fk["referred_table"] for fk in inspector.get_foreign_keys("processing_jobs")}
    assert referred == {"users", "videos"}


def test_migrated_processing_jobs_table_enforces_constraints(database_url, engine):
    command.upgrade(make_config(database_url), "head")
    insert_job = text(
        "INSERT INTO processing_jobs (id, job_type, status, active_key) "
        "VALUES (:id, :job_type, :status, :active_key)"
    )

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO processing_jobs (id, job_type) VALUES (:id, 'metadata')"),
            {"id": uuid.uuid4().hex},
        )
        row = connection.execute(
            text("SELECT status, attempts, max_attempts, created_at FROM processing_jobs")
        ).one()
    assert row.status == "queued"
    assert row.attempts == 0
    assert row.max_attempts == 1
    assert row.created_at is not None

    invalid_rows = [
        {"job_type": "thumbnail", "status": "queued", "active_key": None},
        {"job_type": "render", "status": "paused", "active_key": None},
    ]
    for values in invalid_rows:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(insert_job, {"id": uuid.uuid4().hex, **values})

    # Only one active job per operation.
    with engine.begin() as connection:
        connection.execute(
            insert_job,
            {"id": uuid.uuid4().hex, "job_type": "render", "status": "queued", "active_key": "render:k"},
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                insert_job,
                {"id": uuid.uuid4().hex, "job_type": "render", "status": "queued", "active_key": "render:k"},
            )


def test_downgrade_of_processing_jobs_migration_keeps_auth_tokens(database_url, engine):
    config = make_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0003")

    table_names = inspect(engine).get_table_names()
    assert "processing_jobs" not in table_names
    assert "auth_tokens" in table_names


def test_upgrade_creates_pending_storage_deletions_table(database_url, engine):
    command.upgrade(make_config(database_url), "head")

    inspector = inspect(engine)
    columns = {
        column["name"]: column
        for column in inspector.get_columns("pending_storage_deletions")
    }
    assert set(columns) == {
        "id",
        "storage_key",
        "attempts",
        "last_error_type",
        "created_at",
        "last_attempt_at",
    }
    for name in ("storage_key", "attempts", "created_at"):
        assert columns[name]["nullable"] is False, name
    for name in ("last_error_type", "last_attempt_at"):
        assert columns[name]["nullable"] is True, name

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pending_storage_deletions (id, storage_key) "
                "VALUES (:id, 'users/u/videos/v/source.mp4')"
            ),
            {"id": uuid.uuid4().hex},
        )
        row = connection.execute(
            text("SELECT attempts, created_at FROM pending_storage_deletions")
        ).one()
    assert row.attempts == 0
    assert row.created_at is not None

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO pending_storage_deletions (id, storage_key, attempts) "
                    "VALUES (:id, 'k', -1)"
                ),
                {"id": uuid.uuid4().hex},
            )


def test_downgrade_of_pending_storage_deletions_keeps_processing_jobs(
    database_url, engine
):
    config = make_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0004")

    table_names = inspect(engine).get_table_names()
    assert "pending_storage_deletions" not in table_names
    assert "processing_jobs" in table_names


def test_upgrade_creates_transcript_tables(database_url, engine):
    command.upgrade(make_config(database_url), "head")

    inspector = inspect(engine)
    columns = {column["name"]: column for column in inspector.get_columns("transcripts")}
    assert set(columns) == {
        "id",
        "video_id",
        "language",
        "requested_language",
        "provider",
        "model",
        "full_text",
        "created_at",
    }
    for name in ("video_id", "provider", "full_text", "created_at"):
        assert columns[name]["nullable"] is False, name
    for name in ("language", "requested_language", "model"):
        assert columns[name]["nullable"] is True, name
    indexes = {index["name"]: index for index in inspector.get_indexes("transcripts")}
    assert indexes["ix_transcripts_video_id"]["column_names"] == ["video_id"]
    assert indexes["ix_transcripts_video_id"]["unique"]
    (foreign_key,) = inspector.get_foreign_keys("transcripts")
    assert foreign_key["referred_table"] == "videos"
    assert foreign_key["options"].get("ondelete") == "CASCADE"

    columns = {
        column["name"]: column for column in inspector.get_columns("transcript_segments")
    }
    assert set(columns) == {
        "id",
        "transcript_id",
        "index",
        "start_seconds",
        "end_seconds",
        "text",
        "words",
    }
    assert columns["words"]["nullable"] is True
    for name in ("transcript_id", "index", "start_seconds", "end_seconds", "text"):
        assert columns[name]["nullable"] is False, name
    (unique,) = inspector.get_unique_constraints("transcript_segments")
    assert unique["column_names"] == ["transcript_id", "index"]
    (foreign_key,) = inspector.get_foreign_keys("transcript_segments")
    assert foreign_key["referred_table"] == "transcripts"
    assert foreign_key["options"].get("ondelete") == "CASCADE"


def test_migrated_transcript_tables_enforce_constraints(database_url, engine):
    command.upgrade(make_config(database_url), "head")
    user_id = uuid.uuid4().hex
    video_id = uuid.uuid4().hex
    transcript_id = uuid.uuid4().hex
    insert_transcript = text(
        "INSERT INTO transcripts (id, video_id, provider, full_text) "
        "VALUES (:id, :video_id, 'fake', '')"
    )
    insert_segment = text(
        'INSERT INTO transcript_segments (id, transcript_id, "index", '
        "start_seconds, end_seconds, text) "
        "VALUES (:id, :transcript_id, :index, :start, :end, 'x')"
    )

    with engine.begin() as connection:
        connection.execute(
            INSERT_USER,
            {"id": user_id, "email": "alice@example.com", "hashed_password": "x"},
        )
        connection.execute(INSERT_VIDEO, {"id": video_id, "user_id": user_id})
        connection.execute(insert_transcript, {"id": transcript_id, "video_id": video_id})
        connection.execute(
            insert_segment,
            {"id": uuid.uuid4().hex, "transcript_id": transcript_id, "index": 0, "start": 0.0, "end": 1.5},
        )
        row = connection.execute(
            text("SELECT language, created_at FROM transcripts")
        ).one()
    assert row.language is None
    assert row.created_at is not None

    # One transcript per video.
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                insert_transcript, {"id": uuid.uuid4().hex, "video_id": video_id}
            )

    invalid_segments = [
        {"index": 0, "start": 2.0, "end": 3.0},  # duplicate index
        {"index": -1, "start": 2.0, "end": 3.0},
        {"index": 1, "start": -1.0, "end": 3.0},
        {"index": 1, "start": 3.0, "end": 3.0},
        {"index": 1, "start": 4.0, "end": 3.0},
    ]
    for values in invalid_segments:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert_segment,
                    {"id": uuid.uuid4().hex, "transcript_id": transcript_id, **values},
                )


def test_downgrade_of_transcripts_migration_keeps_storage_deletions(
    database_url, engine
):
    config = make_config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "0005")

    table_names = inspect(engine).get_table_names()
    assert "transcripts" not in table_names
    assert "transcript_segments" not in table_names
    assert "pending_storage_deletions" in table_names
    assert "videos" in table_names


def test_env_reads_database_url_from_settings(database_url, engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)

    command.upgrade(make_config(), "head")

    assert "users" in inspect(engine).get_table_names()
