import unicodedata
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.clip import TITLE_MAX_LENGTH, ClipSource, ClipStatus
from app.models.processing_job import JobStatus

# Clip times are seconds of source-video time: finite, non-negative numbers
# (strict, so no strings or booleans). The position within the video is
# checked by the endpoint. Titles are cleaned before their length is
# checked; this bounds the raw input.
_TITLE_INPUT_MAX_LENGTH = TITLE_MAX_LENGTH * 4


def clean_clip_title(value: str) -> str:
    """Remove control and formatting characters and collapse whitespace.

    Raises ValueError for a title that is empty afterwards or longer than
    TITLE_MAX_LENGTH characters.
    """
    value = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in value
    )
    value = " ".join(value.split())
    if not value:
        raise ValueError("The title must not be blank.")
    if len(value) > TITLE_MAX_LENGTH:
        raise ValueError(f"The title must be at most {TITLE_MAX_LENGTH} characters.")
    return value


class ClipDetectionQueued(BaseModel):
    """The clip detection job that was queued (see `GET /jobs/{job_id}`)."""

    job_id: uuid.UUID
    status: JobStatus


class ClipRead(BaseModel):
    """A clip of a video.

    Times are seconds of source-video time. `source` tells detected
    (`heuristic`, `llm`) and manual clips apart; `detector_version`,
    `detector_model` and `prompt_version` record what produced a detected
    clip and are null for manual clips.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    video_id: uuid.UUID
    start_seconds: float
    end_seconds: float
    title: str
    reason: str
    score: float
    status: ClipStatus
    source: ClipSource
    detector_version: str | None = None
    detector_model: str | None = None
    prompt_version: str | None = None
    created_at: datetime
    updated_at: datetime


class ClipList(BaseModel):
    """A video's clips, best score first, then earlier start."""

    items: list[ClipRead]
    total: int = Field(ge=0)


class ClipCreate(BaseModel):
    """Body of `POST /videos/{id}/clips`: a manually chosen clip."""

    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0, allow_inf_nan=False, strict=True)
    end_seconds: float = Field(ge=0, allow_inf_nan=False, strict=True)
    title: str | None = Field(default=None, max_length=_TITLE_INPUT_MAX_LENGTH)

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str | None) -> str | None:
        return None if value is None else clean_clip_title(value)


class ClipUpdate(BaseModel):
    """Body of `PATCH /clips/{id}`; only the fields sent are changed.

    At least one field is required, and none of them may be null.
    """

    model_config = ConfigDict(extra="forbid")

    start_seconds: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, strict=True
    )
    end_seconds: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, strict=True
    )
    title: str | None = Field(default=None, max_length=_TITLE_INPUT_MAX_LENGTH)
    status: ClipStatus | None = None

    @field_validator("title")
    @classmethod
    def _clean_title(cls, value: str | None) -> str | None:
        return None if value is None else clean_clip_title(value)

    @model_validator(mode="after")
    def _check_fields(self) -> "ClipUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one field must be given.")
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError("Fields must not be null.")
        return self
