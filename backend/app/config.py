"""Backward-compatible import path for the backend settings.

Configuration is defined only in app.core.config; import from there. This
module re-exports it so older imports keep working without a second source of
truth.
"""

from app.core.config import (  # noqa: F401
    Settings,
    get_database_url,
    get_settings,
    parse_cors_origins,
    sanitize_cors_origins,
)
