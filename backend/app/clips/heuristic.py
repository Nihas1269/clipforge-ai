"""A deterministic, credential-free clip detector working from the transcript.

Candidate windows start at a segment's start and end at a later (or the same)
segment's end, so clips always align with segment boundaries. Every window
whose length is within [min_seconds, max_seconds] and that contains at least
one word is scored; the windows are then passed to validate_candidates(),
which keeps the best non-overlapping ones.

Score (0-1) = 0.4 * density + 0.3 * emphasis + 0.3 * keywords, where each
signal is a rate over the window's length, capped at 1:

- density: words per second, full marks at 3 words/s (fast, continuous
  speech). Silences and pauses inside a window lower it.
- emphasis: questions and exclamations per minute (a run such as "?!" counts
  once), full marks at 6 per minute.
- keywords: keyword matches per minute, full marks at 4 per minute.
  Keywords (CLIP_KEYWORDS by default) match case-insensitively as whole words
  or phrases within one segment.

The score is rounded to 6 decimals. Because the signals are rates, a longer
window does not win just by containing more text. Equal scores are ranked by
validate_candidates()' tie-break (earlier start, then earlier end). The same
segments and options always give the same result.

Segments with invalid times (not finite, negative, or not ending after they
start) or ending more than TIME_TOLERANCE_SECONDS past the video's end are
ignored.
"""

import logging
import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.clips.base import (
    TIME_TOLERANCE_SECONDS,
    ClipCandidate,
    ClipDetectionOptions,
    ClipSegment,
    check_video_duration,
    validate_candidates,
)
from app.core.config import get_settings

logger = logging.getLogger(__name__)

DENSITY_WEIGHT = 0.4
EMPHASIS_WEIGHT = 0.3
KEYWORD_WEIGHT = 0.3
FULL_DENSITY_WORDS_PER_SECOND = 3.0
FULL_EMPHASIS_PER_MINUTE = 6.0
FULL_KEYWORDS_PER_MINUTE = 4.0
SCORE_DECIMALS = 6

TITLE_MAX_WORDS = 8
TITLE_MAX_CHARS = 80

_WORD = re.compile(r"\w+")
# "?" and "!", including their full-width forms (U+FF1F, U+FF01).
_EMPHASIS = re.compile("[?!？！]+")


def normalize_keywords(keywords: Iterable[str]) -> list[str]:
    """Casefold keywords, collapse inner whitespace, drop blanks and duplicates."""
    normalized: list[str] = []
    for keyword in keywords:
        if not isinstance(keyword, str):
            raise TypeError("Keywords must be strings.")
        keyword = " ".join(keyword.split()).casefold()
        if keyword and keyword not in normalized:
            normalized.append(keyword)
    return normalized


def _clean_text(text: str) -> str:
    # Control and formatting characters removed, whitespace collapsed.
    text = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in text
    )
    return " ".join(text.split())


@dataclass(frozen=True)
class _Segment:
    start: float
    end: float
    text: str
    words: int
    emphasis: int
    keywords: int


def _format_time(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class HeuristicClipDetector:
    """Scores segment-aligned windows by speech density, emphasis and keywords.

    `keywords` defaults to the CLIP_KEYWORDS setting; pass an empty list to
    disable the keyword signal.
    """

    name = "heuristic"
    # Stored with every clip it suggests; bump it when the scoring changes.
    version = "heuristic-1"

    def __init__(self, keywords: Iterable[str] | None = None) -> None:
        if keywords is None:
            keywords = get_settings().CLIP_KEYWORDS
        self.keywords = tuple(normalize_keywords(keywords))
        self._keyword_pattern: re.Pattern[str] | None = None
        if self.keywords:
            # Longest first, so "how to" wins over "how" at the same position.
            ordered = sorted(
                self.keywords, key=lambda keyword: (-len(keyword), keyword)
            )
            alternatives = "|".join(
                r"\s+".join(re.escape(part) for part in keyword.split())
                for keyword in ordered
            )
            self._keyword_pattern = re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)")

    def _count_keywords(self, text: str) -> int:
        if self._keyword_pattern is None:
            return 0
        return len(self._keyword_pattern.findall(text.casefold()))

    def _prepare(
        self, segments: Sequence[ClipSegment], duration: float
    ) -> list[_Segment]:
        prepared: list[tuple[float, float, int, _Segment]] = []
        for position, segment in enumerate(segments):
            start, end, text = segment.start, segment.end, segment.text
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or not isinstance(text, str)
            ):
                continue
            start, end = float(start), float(end)
            if not (math.isfinite(start) and math.isfinite(end)):
                continue
            if start < 0 or end <= start or end > duration + TIME_TOLERANCE_SECONDS:
                continue
            text = _clean_text(text)
            item = _Segment(
                start=start,
                end=min(end, duration),
                text=text,
                words=len(_WORD.findall(text)),
                emphasis=len(_EMPHASIS.findall(text)),
                keywords=self._count_keywords(text),
            )
            prepared.append((item.start, item.end, position, item))
        # Sorted by time; the input position breaks ties deterministically.
        prepared.sort(key=lambda entry: entry[:3])
        return [entry[3] for entry in prepared]

    @staticmethod
    def _score(length: float, words: int, emphasis: int, keywords: int) -> float:
        minutes = length / 60.0
        density = min(1.0, words / length / FULL_DENSITY_WORDS_PER_SECOND)
        emphasis_rate = min(1.0, emphasis / minutes / FULL_EMPHASIS_PER_MINUTE)
        keyword_rate = min(1.0, keywords / minutes / FULL_KEYWORDS_PER_MINUTE)
        score = (
            DENSITY_WEIGHT * density
            + EMPHASIS_WEIGHT * emphasis_rate
            + KEYWORD_WEIGHT * keyword_rate
        )
        return min(1.0, max(0.0, round(score, SCORE_DECIMALS)))

    @staticmethod
    def _title(window: Sequence[_Segment], start: float) -> str:
        for segment in window:
            words = segment.text.split()
            if not words:
                continue
            title = " ".join(words[:TITLE_MAX_WORDS])
            truncated = len(words) > TITLE_MAX_WORDS
            if len(title) > TITLE_MAX_CHARS:
                title = title[:TITLE_MAX_CHARS].rstrip()
                truncated = True
            return f"{title}..." if truncated else title
        return f"Clip at {_format_time(start)}"

    def detect(
        self,
        segments: Sequence[ClipSegment],
        video_duration: float,
        options: ClipDetectionOptions,
    ) -> list[ClipCandidate]:
        duration = check_video_duration(video_duration)
        if not isinstance(options, ClipDetectionOptions):
            raise TypeError("options must be ClipDetectionOptions.")
        prepared = self._prepare(segments, duration)

        # (score, start, end, first index, last index) of every window.
        windows: list[tuple[float, float, float, int, int]] = []
        for first, opening in enumerate(prepared):
            words = emphasis = keywords = 0
            window_end = opening.end
            for last in range(first, len(prepared)):
                segment = prepared[last]
                window_end = max(window_end, segment.end)
                length = window_end - opening.start
                # window_end only grows, so no later window can be short enough.
                if length > options.max_seconds + TIME_TOLERANCE_SECONDS:
                    break
                words += segment.words
                emphasis += segment.emphasis
                keywords += segment.keywords
                if length < options.min_seconds - TIME_TOLERANCE_SECONDS or not words:
                    continue
                score = self._score(length, words, emphasis, keywords)
                windows.append((score, opening.start, window_end, first, last))

        # Pre-select with the same ranking and overlap rule as
        # validate_candidates() so only a few windows become candidates; the
        # validation below still checks every one of them.
        windows.sort(key=lambda window: (-window[0], window[1], window[2], window[4]))
        chosen: list[tuple[float, float, float, int, int]] = []
        for window in windows:
            if len(chosen) >= options.max_clips:
                break
            if any(window[1] < other[2] and other[1] < window[2] for other in chosen):
                continue
            chosen.append(window)

        candidates = [
            self._candidate(prepared[first : last + 1], score, start, end)
            for score, start, end, first, last in chosen
        ]
        clips = validate_candidates(candidates, duration, options)
        logger.info(
            "clip_detection_completed",
            extra={
                "detector": self.name,
                "segments": len(prepared),
                "windows": len(windows),
                "clips": len(clips),
            },
        )
        return clips

    def _candidate(
        self, window: Sequence[_Segment], score: float, start: float, end: float
    ) -> ClipCandidate:
        length = end - start
        words = sum(segment.words for segment in window)
        emphasis = sum(segment.emphasis for segment in window)
        keywords = sum(segment.keywords for segment in window)
        reason = (
            f"{words} words in {length:.1f} s ({words / length:.1f} words/s); "
            f"{emphasis} questions or exclamations; {keywords} keyword matches."
        )
        return ClipCandidate(
            start=start,
            end=end,
            score=score,
            title=self._title(window, start),
            reason=reason,
        )
