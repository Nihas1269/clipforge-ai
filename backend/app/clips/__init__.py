"""Clip detection: finding short, self-contained moments in a transcript."""

from app.clips.base import (
    TIME_TOLERANCE_SECONDS,
    ClipCandidate,
    ClipDetectionOptions,
    ClipDetector,
    ClipSegment,
    InvalidClipCandidateError,
    validate_candidates,
)
from app.clips.heuristic import HeuristicClipDetector

__all__ = [
    "TIME_TOLERANCE_SECONDS",
    "ClipCandidate",
    "ClipDetectionOptions",
    "ClipDetector",
    "ClipSegment",
    "HeuristicClipDetector",
    "InvalidClipCandidateError",
    "validate_candidates",
]
