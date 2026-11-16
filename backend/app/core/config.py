from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]

# backend/.env, resolved from this file so it is found regardless of the
# current working directory.
ENV_FILE = BACKEND_DIR / ".env"

# backend/storage, git-ignored. Used by the local storage backend.
DEFAULT_LOCAL_STORAGE_DIR = BACKEND_DIR / "storage"

DATABASE_URL_ENV_VAR = "DATABASE_URL"
WILDCARD_ORIGIN = "*"

# Development-only default for SECRET_KEY. It is public, so it is rejected in
# production.
DEFAULT_SECRET_KEY = "dev-insecure-secret-key-change-me"

# Secret values that are public (code defaults and documented placeholders)
# and must never be used in production. Compared case-insensitively after
# stripping whitespace.
KNOWN_DEVELOPMENT_SECRET_KEYS = frozenset(
    {
        DEFAULT_SECRET_KEY,
        # Placeholder in backend/.env.example.
        "<generate-a-long-random-secret>",
        # Earlier backend/.env.example placeholder.
        "change-me-to-a-long-random-string",
        "change-me",
        "changeme",
        "secret",
        "dev",
        "development",
        "test",
    }
)


def sanitize_cors_origins(origins: Iterable[str]) -> list[str]:
    """Return a cleaned list of allowed CORS origins.

    Entries are stripped and blank or non-string entries are dropped. The
    wildcard "*" is always removed because CORS is configured with credentials
    enabled, and a wildcard would let any origin make credentialed requests.
    """
    cleaned = []
    for origin in origins:
        if not isinstance(origin, str):
            continue
        origin = origin.strip()
        if origin and origin != WILDCARD_ORIGIN:
            cleaned.append(origin)
    return cleaned


def parse_cors_origins(raw: str) -> list[str]:
    """Parse a comma-separated list of allowed CORS origins.

    Blank entries and the wildcard "*" are ignored (see sanitize_cors_origins).
    """
    return sanitize_cors_origins(raw.split(","))


def is_known_development_secret(secret: str) -> bool:
    """Return True if the secret is a public development default or placeholder."""
    normalized = secret.strip().lower()
    return any(normalized == known.lower() for known in KNOWN_DEVELOPMENT_SECRET_KEYS)


class Settings(BaseSettings):
    """Application settings loaded from environment variables and backend/.env.

    Environment variables take precedence over values in the .env file.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        # backend/.env may hold variables for features that do not read them
        # through Settings yet (for example S3_*).
        extra="ignore",
        # Never include submitted values (such as SECRET_KEY) in error messages.
        hide_input_in_errors=True,
    )

    APP_ENV: Literal["development", "test", "production"] = "development"
    APP_NAME: str = "ClipForge AI"
    # Optional so the app starts (and /health responds) without a database;
    # get_database_url() raises a clear error when it is needed but unset.
    DATABASE_URL: str | None = None
    REDIS_URL: str | None = None
    SECRET_KEY: SecretStr = SecretStr(DEFAULT_SECRET_KEY)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=30, gt=0)
    # NoDecode lets CORS_ORIGINS be a comma-separated string in the
    # environment instead of JSON.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    MAX_UPLOAD_SIZE_MB: int = Field(default=500, gt=0)
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    # Root directory of the local storage backend. A relative path is
    # resolved against the current working directory.
    LOCAL_STORAGE_DIR: Path = DEFAULT_LOCAL_STORAGE_DIR

    @field_validator("LOCAL_STORAGE_DIR", mode="before")
    @classmethod
    def _reject_blank_storage_dir(cls, value: Any) -> Any:
        # Path("") is the current directory; never store files there by
        # accident.
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("LOCAL_STORAGE_DIR must not be empty.")
        return value

    @field_validator("DATABASE_URL", "REDIS_URL", mode="before")
    @classmethod
    def _blank_url_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.split(",")
        return value

    @field_validator("CORS_ORIGINS", mode="after")
    @classmethod
    def _sanitize_cors_origins(cls, value: list[str]) -> list[str]:
        # Enforce the wildcard restriction for every construction path, not only
        # when settings are loaded from environment variables.
        return sanitize_cors_origins(value)

    @model_validator(mode="after")
    def _require_production_secret(self) -> "Settings":
        if self.APP_ENV != "production":
            return self
        secret = self.SECRET_KEY.get_secret_value()
        if not secret.strip():
            raise ValueError(
                "SECRET_KEY must be set when APP_ENV is 'production'. "
                "See backend/README.md for configuration instructions."
            )
        if is_known_development_secret(secret):
            raise ValueError(
                "SECRET_KEY is a known development default or placeholder and "
                "must not be used when APP_ENV is 'production'. Set it to a "
                "long random value. See backend/README.md."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the application settings, loaded once and cached.

    Every backend module reads configuration through this accessor. Call
    get_settings.cache_clear() to reload (for example in tests).
    """
    return Settings()


def get_database_url() -> str:
    """Return the configured database URL or raise a clear error if it is unset."""
    database_url = get_settings().DATABASE_URL
    if not database_url:
        raise RuntimeError(
            f"The {DATABASE_URL_ENV_VAR} environment variable is not set. "
            "See backend/README.md for database configuration instructions."
        )
    return database_url
