import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.processing_job import JobStatus
from app.transcription.base import LANGUAGE_MAX_LENGTH, normalize_language


class TranscriptionRequest(BaseModel):
    """Body of `POST /videos/{id}/transcription`.

    `language` is an ISO 639 code with optional BCP 47 subtags ("en",
    "hi-IN"); omitted, null, "" or "auto" ask the provider to detect it.
    """

    model_config = ConfigDict(extra="forbid")

    language: str | None = Field(default=None, max_length=LANGUAGE_MAX_LENGTH)

    @field_validator("language")
    @classmethod
    def _normalize_language(cls, value: str | None) -> str | None:
        return normalize_language(value)


class TranscriptionQueued(BaseModel):
    """The transcription job that was queued (see `GET /jobs/{job_id}`)."""

    job_id: uuid.UUID
    status: JobStatus


class WordTimingRead(BaseModel):
    """A word and when it is spoken, in seconds of source-video time."""

    word: str
    start: float
    end: float


class TranscriptSegmentRead(BaseModel):
    """A transcript segment.

    `id` identifies the segment for as long as the transcript exists (a new
    transcription creates new segments); `index` is its position. Times are
    seconds of source-video time. `words` is null when the provider reported
    only segment-level timings.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    index: int
    start_seconds: float
    end_seconds: float
    text: str
    words: list[WordTimingRead] | None = None


class TranscriptRead(BaseModel):
    """A video's transcript with its segments in order.

    `timing_granularity` is "word" only when every segment has word timings;
    otherwise only segment boundaries are reliable for selecting ranges.
    """

    id: uuid.UUID
    video_id: uuid.UUID
    language: str | None = None
    requested_language: str | None = None
    provider: str
    model: str | None = None
    full_text: str
    timing_granularity: Literal["word", "segment"]
    created_at: datetime
    segments: list[TranscriptSegmentRead]


class TranscriptSearchResult(BaseModel):
    """Segments whose text contains the query, in transcript order."""

    query: str
    total: int = Field(ge=0)
    items: list[TranscriptSegmentRead]
