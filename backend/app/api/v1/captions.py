"""Clip captions and caption styles.

Every endpoint requires authentication and works only on the current user's
clips (get_owned_clip checks ownership through the clip's video; another
user's clip gets the same 404 as a missing one).

- `POST /clips/{id}/captions/generate` rebuilds the captions from the
  video's transcript (app.captions.generate) with the clip's
  `max_chars_per_line`. It **replaces** every caption, including user edits;
  409 `transcript_required` when the video has no transcript.
- `GET /clips/{id}/captions` lists the captions in time order.
- `PUT /clips/{id}/captions` replaces the list. Times are seconds from the
  clip start, rounded to milliseconds; captions must be in time order, must
  not overlap (touching is allowed), must lie within the clip
  (`0 <= start < end <= clip length`) and every line must fit the style's
  `max_chars_per_line` (422 otherwise).
- `GET /clips/{id}/caption-style` returns the stored style, or the default
  (`is_default: true`) when none was saved.
- `PUT /clips/{id}/caption-style` replaces the style (see
  app.schemas.caption.CaptionStyleUpdate); the font must be in the
  CAPTION_FONTS allowlist.

Changing a clip's range does not move its captions: regenerate them.
"""

import logging

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_owned_clip
from app.captions.generate import CaptionLine, generate_captions
from app.captions.styles import (
    MIN_PRIMARY_ALPHA,
    PRESET_DEFAULTS,
    StyleValues,
    color_alpha,
    default_style,
    match_allowed_font,
)
from app.clips.base import TIME_TOLERANCE_SECONDS
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.database import get_db
from app.models.caption import CaptionStyle
from app.models.clip import Clip
from app.repositories.captions import get_caption_style, list_captions, replace_captions
from app.repositories.transcripts import get_transcript_for_video
from app.schemas.caption import (
    CaptionList,
    CaptionRead,
    CaptionsReplace,
    CaptionStyleRead,
    CaptionStyleUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clips", tags=["captions"])


def _log(clip: Clip, **extra: object) -> dict[str, object]:
    return {
        "user_id": str(clip.video.user_id),
        "video_id": str(clip.video_id),
        "clip_id": str(clip.id),
        **extra,
    }


def _lock_clip(db: Session, clip: Clip) -> None:
    """Lock the clip row so caption changes to one clip are serialized.

    A clip deleted meanwhile is 404 (the lock has no effect on SQLite).
    """
    locked = db.scalar(select(Clip.id).where(Clip.id == clip.id).with_for_update())
    if locked is None:
        raise AppError(status.HTTP_404_NOT_FOUND, "not_found", "Clip not found.")


def _commit(
    db: Session,
    event: str,
    log_extra: dict[str, object],
    code: str = "caption_update_failed",
    message: str = "The captions could not be saved.",
) -> None:
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        # Only the type: database errors carry bound parameters.
        logger.error(event, extra={**log_extra, "error_type": type(exc).__name__})
        raise AppError(status.HTTP_500_INTERNAL_SERVER_ERROR, code, message) from None


def _current_style(
    db: Session, clip: Clip, settings: Settings
) -> tuple[StyleValues, bool]:
    """Return the clip's style and whether it is the (unsaved) default."""
    stored = get_caption_style(db, clip.id)
    if stored is None:
        return default_style(settings.CAPTION_FONTS[0]), True
    return (
        StyleValues(
            preset=stored.preset,
            font_family=stored.font_family,
            font_size=stored.font_size,
            primary_color=stored.primary_color,
            outline_color=stored.outline_color,
            position=stored.position,
            uppercase=stored.uppercase,
            max_chars_per_line=stored.max_chars_per_line,
        ),
        False,
    )


def _caption_list(db: Session, clip: Clip) -> CaptionList:
    captions = list_captions(db, clip.id)
    return CaptionList(
        clip_id=clip.id,
        items=[CaptionRead.model_validate(caption) for caption in captions],
        total=len(captions),
    )


def _style_read(
    clip: Clip, style: StyleValues, is_default: bool, settings: Settings
) -> CaptionStyleRead:
    return CaptionStyleRead(
        clip_id=clip.id,
        preset=style.preset,
        font_family=style.font_family,
        font_size=style.font_size,
        primary_color=style.primary_color,
        outline_color=style.outline_color,
        position=style.position,
        uppercase=style.uppercase,
        max_chars_per_line=style.max_chars_per_line,
        is_default=is_default,
        allowed_fonts=list(settings.CAPTION_FONTS),
    )


# Captions


@router.get(
    "/{clip_id}/captions",
    response_model=CaptionList,
    responses={404: {"description": "Unknown clip, or another user's clip."}},
)
def read_captions(
    clip: Clip = Depends(get_owned_clip),
    db: Session = Depends(get_db),
) -> CaptionList:
    return _caption_list(db, clip)


@router.post(
    "/{clip_id}/captions/generate",
    response_model=CaptionList,
    responses={
        404: {"description": "Unknown clip, or another user's clip."},
        409: {"description": "The video has no transcript."},
    },
)
def generate_clip_captions(
    clip: Clip = Depends(get_owned_clip),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CaptionList:
    """Rebuild the captions from the transcript, replacing every caption."""
    _lock_clip(db, clip)
    log_extra = _log(clip)
    transcript = get_transcript_for_video(db, clip.video_id)
    if transcript is None:
        logger.info(
            "captions_generation_rejected",
            extra={**log_extra, "reason": "no_transcript"},
        )
        raise AppError(
            status.HTTP_409_CONFLICT,
            "transcript_required",
            "The video has no transcript. Transcribe it first.",
        )
    style, _ = _current_style(db, clip, settings)
    lines = generate_captions(
        transcript.segments,
        clip.start_seconds,
        clip.end_seconds,
        style.max_chars_per_line,
    )
    replace_captions(db, clip.id, lines)
    _commit(db, "captions_generation_failed", log_extra)
    word_level = sum(1 for s in transcript.segments if s.words)
    logger.info(
        "captions_generated",
        extra={
            **log_extra,
            "caption_count": len(lines),
            "segment_count": len(transcript.segments),
            "word_timed_segment_count": word_level,
        },
    )
    return _caption_list(db, clip)


def _invalid_caption(
    code: str, message: str, index: int, **details: object
) -> AppError:
    return AppError(422, code, message, {"index": index, **details})


@router.put(
    "/{clip_id}/captions",
    response_model=CaptionList,
    responses={
        404: {"description": "Unknown clip, or another user's clip."},
        422: {
            "description": "Invalid caption times, order, overlap, text or line length."
        },
    },
)
def update_captions(
    body: CaptionsReplace,
    clip: Clip = Depends(get_owned_clip),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CaptionList:
    """Replace the clip's captions with the given list."""
    _lock_clip(db, clip)
    log_extra = _log(clip)
    style, _ = _current_style(db, clip, settings)
    duration = round(clip.end_seconds - clip.start_seconds, 3)

    # Everything is checked before anything is changed.
    lines: list[CaptionLine] = []
    for index, caption in enumerate(body.captions):
        start = round(caption.start_seconds, 3)
        end = round(caption.end_seconds, 3)
        if end > duration and end - duration <= TIME_TOLERANCE_SECONDS:
            end = duration
        if end > duration or start >= duration:
            raise _invalid_caption(
                "caption_outside_clip",
                "Captions must start and end within the clip.",
                index,
                clip_duration_seconds=duration,
            )
        if end <= start:
            raise _invalid_caption(
                "invalid_caption_timing",
                "A caption must end after it starts.",
                index,
            )
        if lines and start < lines[-1].end:
            raise _invalid_caption(
                "captions_overlap",
                "Captions must be in time order and must not overlap.",
                index,
            )
        if any(
            len(line) > style.max_chars_per_line for line in caption.text.split("\n")
        ):
            raise _invalid_caption(
                "caption_line_too_long",
                f"Caption lines must be at most {style.max_chars_per_line} characters.",
                index,
                max_chars_per_line=style.max_chars_per_line,
            )
        lines.append(CaptionLine(start=start, end=end, text=caption.text))

    replace_captions(db, clip.id, lines)
    _commit(db, "captions_update_failed", log_extra)
    logger.info("captions_updated", extra={**log_extra, "caption_count": len(lines)})
    return _caption_list(db, clip)


# Style


@router.get(
    "/{clip_id}/caption-style",
    response_model=CaptionStyleRead,
    responses={404: {"description": "Unknown clip, or another user's clip."}},
)
def read_caption_style(
    clip: Clip = Depends(get_owned_clip),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CaptionStyleRead:
    style, is_default = _current_style(db, clip, settings)
    return _style_read(clip, style, is_default, settings)


@router.put(
    "/{clip_id}/caption-style",
    response_model=CaptionStyleRead,
    responses={
        404: {"description": "Unknown clip, or another user's clip."},
        422: {"description": "Invalid preset, font, size, color or position."},
    },
)
def update_caption_style(
    body: CaptionStyleUpdate,
    clip: Clip = Depends(get_owned_clip),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CaptionStyleRead:
    """Replace the clip's caption style; omitted fields take preset defaults."""
    _lock_clip(db, clip)
    log_extra = _log(clip)
    defaults = PRESET_DEFAULTS[body.preset]

    font = settings.CAPTION_FONTS[0]
    if body.font_family is not None:
        matched = match_allowed_font(body.font_family, settings.CAPTION_FONTS)
        if matched is None:
            raise AppError(
                422,
                "font_not_allowed",
                "This font is not available for captions.",
                {"allowed_fonts": list(settings.CAPTION_FONTS)},
            )
        font = matched
    primary_color = body.primary_color or defaults.primary_color
    if color_alpha(primary_color) < MIN_PRIMARY_ALPHA:
        raise AppError(
            422,
            "caption_color_too_transparent",
            "The caption text color must be at least "
            f"{MIN_PRIMARY_ALPHA / 255:.0%} opaque.",
            {"min_alpha": f"{MIN_PRIMARY_ALPHA:02X}"},
        )

    values = StyleValues(
        preset=body.preset,
        font_family=font,
        font_size=body.font_size if body.font_size is not None else defaults.font_size,
        primary_color=primary_color,
        outline_color=body.outline_color or defaults.outline_color,
        position=body.position if body.position is not None else defaults.position,
        uppercase=body.uppercase if body.uppercase is not None else defaults.uppercase,
        max_chars_per_line=(
            body.max_chars_per_line
            if body.max_chars_per_line is not None
            else defaults.max_chars_per_line
        ),
    )
    stored = get_caption_style(db, clip.id)
    if stored is None:
        stored = CaptionStyle(clip_id=clip.id)
        db.add(stored)
    stored.preset = values.preset
    stored.font_family = values.font_family
    stored.font_size = values.font_size
    stored.primary_color = values.primary_color
    stored.outline_color = values.outline_color
    stored.position = values.position
    stored.uppercase = values.uppercase
    stored.max_chars_per_line = values.max_chars_per_line
    _commit(
        db,
        "caption_style_update_failed",
        log_extra,
        code="caption_style_update_failed",
        message="The caption style could not be saved.",
    )
    logger.info(
        "caption_style_updated",
        extra={**log_extra, "preset": values.preset.value},
    )
    return _style_read(clip, values, False, settings)
