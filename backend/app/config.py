import os
from collections.abc import Iterable
from dataclasses import dataclass, field

DATABASE_URL_ENV_VAR = "DATABASE_URL"
CORS_ORIGINS_ENV_VAR = "CORS_ORIGINS"


def get_database_url() -> str:
    """Return the database URL from the DATABASE_URL environment variable."""
    database_url = os.environ.get(DATABASE_URL_ENV_VAR, "").strip()
    if not database_url:
        raise RuntimeError(
            f"The {DATABASE_URL_ENV_VAR} environment variable is not set. "
            "See backend/README.md for database configuration instructions."
        )
    return database_url


WILDCARD_ORIGIN = "*"


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


@dataclass(frozen=True)
class Settings:
    """Application settings read from the environment."""

    CORS_ORIGINS: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Enforce the wildcard restriction for every construction path, not only
        # when settings are loaded from environment variables.
        object.__setattr__(
            self, "CORS_ORIGINS", sanitize_cors_origins(self.CORS_ORIGINS)
        )


def get_settings() -> Settings:
    """Build application settings from environment variables."""
    return Settings(
        CORS_ORIGINS=parse_cors_origins(os.environ.get(CORS_ORIGINS_ENV_VAR, "")),
    )
