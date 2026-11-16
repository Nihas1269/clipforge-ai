"""Database models.

Import every model here so that `Base.metadata` is complete for Alembic.
"""

from app.models.user import User
from app.models.video import Video

__all__ = ["User", "Video"]
