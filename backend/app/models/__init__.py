"""Database models.

Import every model here so that `Base.metadata` is complete for Alembic.
"""

from app.models.user import User

__all__ = ["User"]
