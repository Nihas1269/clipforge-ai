import os

DATABASE_URL_ENV_VAR = "DATABASE_URL"


def get_database_url() -> str:
    """Return the database URL from the DATABASE_URL environment variable."""
    database_url = os.environ.get(DATABASE_URL_ENV_VAR, "").strip()
    if not database_url:
        raise RuntimeError(
            f"The {DATABASE_URL_ENV_VAR} environment variable is not set. "
            "See backend/README.md for database configuration instructions."
        )
    return database_url
