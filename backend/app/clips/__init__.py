"""Clip detection: finding short, self-contained moments in a transcript.

Use get_clip_detector() to obtain the detector selected by
Settings.CLIP_DETECTOR.
"""

import logging

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
from app.clips.llm import ClipDetectionRun, LLMClipDetector
from app.core.config import Settings, get_settings

__all__ = [
    "TIME_TOLERANCE_SECONDS",
    "ClipCandidate",
    "ClipDetectionOptions",
    "ClipDetectionRun",
    "ClipDetector",
    "ClipSegment",
    "HeuristicClipDetector",
    "InvalidClipCandidateError",
    "LLMClipDetector",
    "get_clip_detector",
    "validate_candidates",
]

logger = logging.getLogger(__name__)

HEURISTIC_DETECTOR = "heuristic"
LLM_DETECTOR = "llm"


def get_clip_detector(settings: Settings | None = None) -> ClipDetector:
    """Return the clip detector selected by CLIP_DETECTOR.

    "heuristic" (default) gives HeuristicClipDetector. "llm" gives
    LLMClipDetector, which falls back to the heuristic detector (and logs a
    warning) when the LLM settings are missing or invalid or a request fails.
    An unknown name logs a warning and gives the heuristic detector; this
    never raises for configuration problems.
    """
    if settings is None:
        settings = get_settings()
    heuristic = HeuristicClipDetector(keywords=settings.CLIP_KEYWORDS)
    name = settings.CLIP_DETECTOR
    if name == LLM_DETECTOR:
        return LLMClipDetector.from_settings(settings, fallback=heuristic)
    if name != HEURISTIC_DETECTOR:
        logger.warning(
            "clip_detector_unknown",
            extra={"clip_detector": name[:50], "fallback": heuristic.name},
        )
    return heuristic
