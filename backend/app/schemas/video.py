import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.video import VideoStatus


class VideoRead(BaseModel):
    """A video as returned by the API.

    The storage key is internal and is deliberately not exposed.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    original_filename: str
    content_type: str
    size_bytes: int
    status: VideoStatus
    error_message: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    created_at: datetime
    updated_at: datetime


class UploadLimits(BaseModel):
    """The upload limits enforced by `POST /videos`, for client-side checks."""

    max_upload_size_bytes: int = Field(gt=0)
    allowed_extensions: list[str]
    allowed_content_types: list[str]


class VideoList(BaseModel):
    """A list of videos with the total number available."""

    items: list[VideoRead]
    total: int = Field(ge=0)
