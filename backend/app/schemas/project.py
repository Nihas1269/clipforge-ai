import unicodedata
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.project import PROJECT_NAME_MAX_LENGTH


def normalize_project_name(value: Any) -> Any:
    """Strip the name and reject blank names, control characters and long names."""
    if not isinstance(value, str):
        return value
    name = value.strip()
    if not name:
        raise ValueError("The project name must not be blank.")
    if len(name) > PROJECT_NAME_MAX_LENGTH:
        raise ValueError("The project name is too long.")
    if any(unicodedata.category(character) in ("Cc", "Cf") for character in name):
        raise ValueError("The project name contains invalid characters.")
    return name


class ProjectCreate(BaseModel):
    """Body of `POST /projects`."""

    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: Any) -> Any:
        return normalize_project_name(value)


class ProjectUpdate(BaseModel):
    """Body of `PATCH /projects/{id}`: rename and/or archive or restore.

    Omitted fields are left unchanged; neither field may be null.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    archived: bool | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _normalize_name(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("The project name must not be null.")
        return normalize_project_name(value)

    @field_validator("archived", mode="before")
    @classmethod
    def _archived_not_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("archived must not be null.")
        return value


class ProjectRef(BaseModel):
    """The project a video is filed under, as embedded in `VideoRead`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    archived: bool


class StatusCounts(BaseModel):
    """Number of videos in each processing status."""

    uploaded: int = Field(default=0, ge=0)
    processing: int = Field(default=0, ge=0)
    ready: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class ProjectRead(BaseModel):
    """A project with the number of its videos in each status."""

    id: uuid.UUID
    name: str
    archived: bool
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    video_count: int = Field(ge=0)
    status_counts: StatusCounts


class ProjectList(BaseModel):
    items: list[ProjectRead]
    total: int = Field(ge=0)
