import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.captions.generate import CAPTION_TEXT_MAX_LENGTH, clean_caption_text
from app.captions.styles import (
    FONT_FAMILY_MAX_LENGTH,
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    MAX_CHARS_PER_LINE_MAX,
    MAX_CHARS_PER_LINE_MIN,
    CaptionPosition,
    CaptionPreset,
    normalize_hex_color,
)

# Most captions per clip; a 10-minute clip with a caption every second fits.
MAX_CAPTIONS_PER_CLIP = 600
# Raw input bounds; text is cleaned before its length is checked.
_TEXT_INPUT_MAX_LENGTH = CAPTION_TEXT_MAX_LENGTH * 4
_COLOR_INPUT_MAX_LENGTH = 32


class CaptionRead(BaseModel):
    """A caption; times are seconds from the clip start, lines split by `\\n`."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    index: int
    start_seconds: float
    end_seconds: float
    text: str


class CaptionList(BaseModel):
    """A clip's captions in time order."""

    clip_id: uuid.UUID
    items: list[CaptionRead]
    total: int = Field(ge=0)


class CaptionInput(BaseModel):
    """One caption of `PUT /clips/{id}/captions` (seconds from the clip start)."""

    model_config = ConfigDict(extra="forbid")

    start_seconds: float = Field(ge=0, allow_inf_nan=False, strict=True)
    end_seconds: float = Field(ge=0, allow_inf_nan=False, strict=True)
    text: str = Field(max_length=_TEXT_INPUT_MAX_LENGTH)

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        return clean_caption_text(value)

    @model_validator(mode="after")
    def _check_times(self) -> "CaptionInput":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("A caption must end after it starts.")
        return self


class CaptionsReplace(BaseModel):
    """Body of `PUT /clips/{id}/captions`: the complete new caption list."""

    model_config = ConfigDict(extra="forbid")

    captions: list[CaptionInput] = Field(max_length=MAX_CAPTIONS_PER_CLIP)


class CaptionStyleRead(BaseModel):
    """A clip's caption style; `is_default` is true until one is saved."""

    clip_id: uuid.UUID
    preset: CaptionPreset
    font_family: str
    font_size: int
    primary_color: str
    outline_color: str
    position: CaptionPosition
    uppercase: bool
    max_chars_per_line: int
    is_default: bool
    allowed_fonts: list[str]


class CaptionStyleUpdate(BaseModel):
    """Body of `PUT /clips/{id}/caption-style`.

    The whole style is replaced: fields that are left out (or null) take the
    preset's default (the first allowed font for `font_family`).
    """

    model_config = ConfigDict(extra="forbid")

    preset: CaptionPreset
    font_family: str | None = Field(default=None, max_length=FONT_FAMILY_MAX_LENGTH)
    font_size: int | None = Field(
        default=None, ge=FONT_SIZE_MIN, le=FONT_SIZE_MAX, strict=True
    )
    primary_color: str | None = Field(default=None, max_length=_COLOR_INPUT_MAX_LENGTH)
    outline_color: str | None = Field(default=None, max_length=_COLOR_INPUT_MAX_LENGTH)
    position: CaptionPosition | None = None
    uppercase: bool | None = Field(default=None, strict=True)
    max_chars_per_line: int | None = Field(
        default=None,
        ge=MAX_CHARS_PER_LINE_MIN,
        le=MAX_CHARS_PER_LINE_MAX,
        strict=True,
    )

    @field_validator("primary_color", "outline_color")
    @classmethod
    def _normalize_color(cls, value: str | None) -> str | None:
        return None if value is None else normalize_hex_color(value)
