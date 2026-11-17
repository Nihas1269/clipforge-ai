"""Clip detector interface, candidate type and the shared candidate validation.

A detector reads a video's transcript segments and suggests clips as
ClipCandidate values. Every detector passes its candidates through
validate_candidates(), so all of them obey the same rules:

- Times are seconds of source-video time. A candidate must satisfy
  0 <= start < end <= video duration. Timestamps that are out of bounds by at
  most TIME_TOLERANCE_SECONDS (floating-point noise) are snapped to the bound;
  anything further out is rejected (dropped), never clamped.
- Its length must be within [min_seconds, max_seconds] of the options (with
  the same tolerance).
- Clips never overlap. Touching clips (one ends exactly where the next
  starts) are allowed.
- Candidates are ranked by score (highest first); ties are broken by the
  earlier start, then the earlier end, then title and reason, so the result
  never depends on input order. Ranked candidates are taken greedily: one
  that overlaps an already selected candidate is dropped. At most
  `max_clips` are returned, in rank order.
"""

import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from app.core.config import Settings, get_settings
from app.models.clip import TITLE_MAX_LENGTH

logger = logging.getLogger(__name__)

# Floating-point tolerance for clip bounds and lengths, in seconds (1 ms).
TIME_TOLERANCE_SECONDS = 1e-3


class InvalidClipCandidateError(ValueError):
    """A clip candidate violates the candidate schema."""


def _check_number(value: object, name: str) -> float:
    # bool is an int subclass, but True is never a time or score.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidClipCandidateError(f"{name} must be a number.")
    value = float(value)
    if not math.isfinite(value):
        raise InvalidClipCandidateError(f"{name} must be finite.")
    return value


def check_video_duration(video_duration: object) -> float:
    """Return the duration as a float; raise ValueError unless finite and > 0."""
    if isinstance(video_duration, bool) or not isinstance(video_duration, (int, float)):
        raise ValueError("The video duration must be a number.")
    duration = float(video_duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("The video duration must be a finite, positive number.")
    return duration


@dataclass(frozen=True)
class ClipCandidate:
    """A suggested clip: [start, end) in seconds of source-video time.

    `score` is in [0, 1]; higher means a stronger suggestion. Creation checks
    types, that the times are finite and the score is in range, and that the
    title (at most TITLE_MAX_LENGTH characters) and reason are non-empty. The
    position in the video is checked by validate_candidates().
    """

    start: float
    end: float
    score: float
    title: str
    reason: str

    def __post_init__(self) -> None:
        start = _check_number(self.start, "Clip start")
        end = _check_number(self.end, "Clip end")
        score = _check_number(self.score, "Clip score")
        if not 0.0 <= score <= 1.0:
            raise InvalidClipCandidateError("Clip score must be between 0 and 1.")
        if not isinstance(self.title, str) or not self.title.strip():
            raise InvalidClipCandidateError("Clip title must be a non-empty string.")
        title = self.title.strip()
        if len(title) > TITLE_MAX_LENGTH:
            raise InvalidClipCandidateError(
                f"Clip title must be at most {TITLE_MAX_LENGTH} characters."
            )
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise InvalidClipCandidateError("Clip reason must be a non-empty string.")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "reason", self.reason.strip())

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlaps(self, other: "ClipCandidate") -> bool:
        """True if the clips share time; clips that only touch do not overlap."""
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class ClipDetectionOptions:
    """Limits for detected clips: length in seconds and number of clips."""

    min_seconds: float = 15.0
    max_seconds: float = 90.0
    max_clips: int = 10

    def __post_init__(self) -> None:
        min_seconds = _check_number(self.min_seconds, "min_seconds")
        max_seconds = _check_number(self.max_seconds, "max_seconds")
        if min_seconds <= 0:
            raise ValueError("min_seconds must be positive.")
        if max_seconds < min_seconds:
            raise ValueError("max_seconds must not be less than min_seconds.")
        if (
            isinstance(self.max_clips, bool)
            or not isinstance(self.max_clips, int)
            or self.max_clips < 1
        ):
            raise ValueError("max_clips must be a positive integer.")
        object.__setattr__(self, "min_seconds", min_seconds)
        object.__setattr__(self, "max_seconds", max_seconds)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "ClipDetectionOptions":
        """Options from CLIP_MIN_SECONDS, CLIP_MAX_SECONDS and CLIP_MAX_CLIPS."""
        settings = settings or get_settings()
        return cls(
            min_seconds=settings.CLIP_MIN_SECONDS,
            max_seconds=settings.CLIP_MAX_SECONDS,
            max_clips=settings.CLIP_MAX_CLIPS,
        )


class ClipSegment(Protocol):
    """A timed transcript span, such as a TranscriptionSegment."""

    @property
    def start(self) -> float: ...

    @property
    def end(self) -> float: ...

    @property
    def text(self) -> str: ...


@runtime_checkable
class ClipDetector(Protocol):
    """Suggests clips from a video's transcript segments.

    Implementations return the result of validate_candidates(), so every
    clip is within the video and the option limits, and clips never overlap.
    An empty transcript gives an empty list.
    """

    name: str

    def detect(
        self,
        segments: Sequence[ClipSegment],
        video_duration: float,
        options: ClipDetectionOptions,
    ) -> list[ClipCandidate]: ...


def _rank_key(candidate: ClipCandidate) -> tuple[float, float, float, str, str]:
    return (
        -candidate.score,
        candidate.start,
        candidate.end,
        candidate.title,
        candidate.reason,
    )


def validate_candidates(
    candidates: Iterable[ClipCandidate],
    video_duration: float,
    options: ClipDetectionOptions,
) -> list[ClipCandidate]:
    """Return the valid, non-overlapping top candidates, best first.

    See the module docstring for the rules. Raises ValueError for an invalid
    video duration and TypeError for an item that is not a ClipCandidate.
    """
    duration = check_video_duration(video_duration)
    received = 0
    dropped = {"out_of_bounds": 0, "too_short": 0, "too_long": 0, "overlap": 0}
    valid: list[ClipCandidate] = []
    for candidate in candidates:
        received += 1
        if not isinstance(candidate, ClipCandidate):
            raise TypeError("Clip candidates must be ClipCandidate values.")
        start, end = candidate.start, candidate.end
        # Snap floating-point noise at the bounds; anything further out is
        # rejected below.
        if -TIME_TOLERANCE_SECONDS <= start < 0:
            start = 0.0
        if duration < end <= duration + TIME_TOLERANCE_SECONDS:
            end = duration
        if start < 0 or end > duration or end <= start:
            dropped["out_of_bounds"] += 1
            continue
        length = end - start
        if length < options.min_seconds - TIME_TOLERANCE_SECONDS:
            dropped["too_short"] += 1
            continue
        if length > options.max_seconds + TIME_TOLERANCE_SECONDS:
            dropped["too_long"] += 1
            continue
        if (start, end) != (candidate.start, candidate.end):
            candidate = replace(candidate, start=start, end=end)
        valid.append(candidate)

    selected: list[ClipCandidate] = []
    for candidate in sorted(valid, key=_rank_key):
        if len(selected) >= options.max_clips:
            break
        if any(candidate.overlaps(chosen) for chosen in selected):
            dropped["overlap"] += 1
            continue
        selected.append(candidate)

    logger.info(
        "clip_candidates_validated",
        extra={
            "received": received,
            "selected": len(selected),
            **{f"dropped_{reason}": count for reason, count in dropped.items()},
        },
    )
    return selected
