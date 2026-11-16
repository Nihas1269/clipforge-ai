import pytest
from pydantic import ValidationError

from app import config as legacy_config
from app.core.config import (
    DEFAULT_SECRET_KEY,
    KNOWN_DEVELOPMENT_SECRET_KEYS,
    Settings,
    get_database_url,
    get_settings,
)
from app.main import create_app

CONFIG_ENV_VARS = (
    "APP_ENV",
    "APP_NAME",
    "DATABASE_URL",
    "REDIS_URL",
    "SECRET_KEY",
    "ACCESS_TOKEN_EXPIRE_MINUTES",
    "CORS_ORIGINS",
    "MAX_UPLOAD_SIZE_MB",
    "STORAGE_BACKEND",
)

STRONG_SECRET = "a-long-random-production-secret-0123456789abcdef"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_load_in_development():
    settings = Settings()

    assert settings.APP_ENV == "development"
    assert settings.APP_NAME == "ClipForge AI"
    assert settings.DATABASE_URL is None
    assert settings.REDIS_URL is None
    assert settings.SECRET_KEY.get_secret_value() == DEFAULT_SECRET_KEY
    assert settings.ACCESS_TOKEN_EXPIRE_MINUTES == 30
    assert settings.CORS_ORIGINS == []
    assert settings.MAX_UPLOAD_SIZE_MB == 500
    assert settings.STORAGE_BACKEND == "local"


def test_environment_variables_override_defaults(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_NAME", "ClipForge Test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret")
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15")
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "50")
    monkeypatch.setenv("STORAGE_BACKEND", "s3")

    settings = Settings()

    assert settings.APP_ENV == "test"
    assert settings.APP_NAME == "ClipForge Test"
    assert settings.DATABASE_URL == "sqlite:///:memory:"
    assert settings.REDIS_URL == "redis://localhost:6379/1"
    assert settings.SECRET_KEY.get_secret_value() == "test-only-secret"
    assert settings.ACCESS_TOKEN_EXPIRE_MINUTES == 15
    assert settings.MAX_UPLOAD_SIZE_MB == 50
    assert settings.STORAGE_BACKEND == "s3"


def test_cors_origins_parse_from_comma_separated_string(monkeypatch):
    monkeypatch.setenv(
        "CORS_ORIGINS", " http://localhost:3000, ,https://app.example.com,* "
    )

    assert Settings().CORS_ORIGINS == [
        "http://localhost:3000",
        "https://app.example.com",
    ]


def test_blank_database_url_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "   ")

    assert Settings().DATABASE_URL is None
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        get_database_url()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("APP_ENV", "staging"),
        ("STORAGE_BACKEND", "ftp"),
        ("ACCESS_TOKEN_EXPIRE_MINUTES", "0"),
        ("MAX_UPLOAD_SIZE_MB", "-1"),
    ],
)
def test_invalid_values_raise_validation_error(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()


def test_production_with_default_secret_raises_validation_error(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings()


@pytest.mark.parametrize("secret", ["", "   "])
def test_production_with_missing_secret_raises_validation_error(monkeypatch, secret):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", secret)

    with pytest.raises(ValidationError, match="SECRET_KEY must be set"):
        Settings()


@pytest.mark.parametrize("secret", sorted(KNOWN_DEVELOPMENT_SECRET_KEYS))
def test_production_with_known_development_secret_raises_validation_error(
    monkeypatch, secret
):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", f"  {secret.upper()}  ")

    with pytest.raises(ValidationError, match="known development default"):
        Settings()


def test_production_secret_error_does_not_echo_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", DEFAULT_SECRET_KEY)

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    assert DEFAULT_SECRET_KEY not in str(exc_info.value)


def test_production_with_strong_secret_loads(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", STRONG_SECRET)

    settings = Settings()

    assert settings.APP_ENV == "production"
    assert settings.SECRET_KEY.get_secret_value() == STRONG_SECRET
    assert STRONG_SECRET not in repr(settings)


def test_production_startup_fails_without_valid_secret(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(ValidationError, match="SECRET_KEY"):
        create_app()


def test_settings_load_from_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_NAME=From Env File\n"
        "CORS_ORIGINS=http://localhost:3000,https://app.example.com\n"
        "S3_REGION=us-east-1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_NAME", "From Environment")

    settings = Settings(_env_file=env_file)

    # Environment variables take precedence over the .env file, and unrelated
    # variables in the file are ignored.
    assert settings.APP_NAME == "From Environment"
    assert settings.CORS_ORIGINS == [
        "http://localhost:3000",
        "https://app.example.com",
    ]


def test_get_settings_is_cached(monkeypatch):
    first = get_settings()
    monkeypatch.setenv("APP_NAME", "Changed")

    assert get_settings() is first
    assert get_settings().APP_NAME == "ClipForge AI"

    get_settings.cache_clear()

    assert get_settings().APP_NAME == "Changed"


def test_get_database_url_reads_from_settings(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    assert get_database_url() == get_settings().DATABASE_URL == "sqlite:///:memory:"


def test_legacy_config_module_reexports_single_source_of_truth():
    assert legacy_config.Settings is Settings
    assert legacy_config.get_settings is get_settings
    assert legacy_config.get_database_url is get_database_url


def test_create_app_uses_app_name_from_settings(monkeypatch):
    monkeypatch.setenv("APP_NAME", "ClipForge Custom")

    assert create_app().title == "ClipForge Custom"
    assert create_app(Settings(APP_NAME="Explicit Name")).title == "Explicit Name"
