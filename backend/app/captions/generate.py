"""Building clip captions from a transcript, as pure functions.

`generate_captions()` turns the transcript segments that fall in a clip's
range into caption lines whose times are seconds **relative to the clip
start** (0 is the first frame of the clip), rounded to milliseconds.

- **Word timings.** When a segment has valid word timings, the words whose
  midpoint lies in `[clip_start, clip_end)` are kept, their times clamped to
  the clip, and they are grouped in order into lines of at most
  `max_chars_per_line` characters (words joined by single spaces). Each line
  is one caption, from its first word's start to its last word's end. A
  segment boundary always ends a line. A single word longer than the limit
  is its own caption, wrapped over several lines.
- **Segment fallback.** When a segment has no word timings (or any of them is
  invalid), no word timing is invented: the segment becomes one caption over
  the part of the segment inside the clip, with its whole text wrapped into
  lines of at most `max_chars_per_line` characters (joined by `\\n`). A
  segment that only partly overlaps the clip therefore keeps its whole text.
- Captions are made non-overlapping: a caption that starts before the
  previous one ends cuts the previous one short (or, starting less than
  MIN_CAPTION_SECONDS after it, is merged into it). Captions last at least MIN_CAPTION_SECONDS (one
  ASS centisecond) and always satisfy `0 <= start < end <= clip length`.

Text is cleaned with `clean_caption_lines()`: control and formatting
characters are removed and whitespace inside a line is collapsed.
"""

import math
import textwrap
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, TypeGuard

# Longest caption text (all lines, joined by "\n") accepted from users.
CAPTION_TEXT_MAX_LENGTH = 1000
MIN_CAPTION_SECONDS = 0.01

_Word = tuple[str, float, float]


@dataclass(frozen=True)
class CaptionLine:
    """A caption shown from `start` to `end` (seconds from the clip start)."""

    start: float
    end: float
    text: str


class _Segment(Protocol):
    start_seconds: float
    end_seconds: float
    text: str
    words: list[dict[str, Any]] | None


def clean_caption_lines(text: str) -> list[str]:
    """Return the non-empty lines of `text`, cleaned.

    Line breaks (`\\n`, `\\r\\n`, `\\r`) separate lines; other control and
    formatting characters become spaces, and whitespace is collapsed.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = []
    for raw in text.split("\n"):
        line = "".join(
            " " if unicodedata.category(char).startswith("C") else char for char in raw
        )
        line = " ".join(line.split())
        if line:
            lines.append(line)
    return lines


def clean_caption_text(text: str) -> str:
    """Return the cleaned caption text (lines joined by `\\n`).

    Raises ValueError when nothing is left or the result is longer than
    CAPTION_TEXT_MAX_LENGTH characters.
    """
    lines = clean_caption_lines(text)
    if not lines:
        raise ValueError("Caption text must not be blank.")
    cleaned = "\n".join(lines)
    if len(cleaned) > CAPTION_TEXT_MAX_LENGTH:
        raise ValueError(
            f"Caption text must be at most {CAPTION_TEXT_MAX_LENGTH} characters."
        )
    return cleaned


def wrap_caption_text(text: str, max_chars_per_line: int) -> list[str]:
    """Wrap every line of `text` at word boundaries to `max_chars_per_line`.

    Words longer than the limit are broken, so no line is ever longer.
    """
    lines: list[str] = []
    for line in clean_caption_lines(text):
        lines.extend(
            textwrap.wrap(
                line,
                width=max_chars_per_line,
                break_long_words=True,
                break_on_hyphens=False,
            )
        )
    return lines


def _is_time(value: object) -> TypeGuard[float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _segment_words(segment: _Segment) -> list[_Word] | None:
    """Return the segment's valid words, or None to use the segment text."""
    words = segment.words
    if not isinstance(words, list) or not words:
        return None
    parsed: list[_Word] = []
    for item in words:
        if not isinstance(item, dict):
            return None
        text, start, end = item.get("word"), item.get("start"), item.get("end")
        if not isinstance(text, str):
            return None
        if not _is_time(start) or not _is_time(end):
            return None
        if end < start:
            return None
        cleaned = " ".join(clean_caption_lines(text))
        if cleaned:
            parsed.append((cleaned, float(start), float(end)))
    return parsed or None


def _relative(seconds: float, clip_start: float, clip_end: float) -> float:
    return round(min(max(seconds, clip_start), clip_end) - clip_start, 3)


def _word_captions(
    words: list[_Word], clip_start: float, clip_end: float, max_chars: int
) -> list[CaptionLine]:
    selected = [
        word for word in words if clip_start <= (word[1] + word[2]) / 2 < clip_end
    ]
    captions: list[CaptionLine] = []
    current: list[_Word] = []

    def caption(group: list[_Word], text: str) -> CaptionLine:
        return CaptionLine(
            start=_relative(group[0][1], clip_start, clip_end),
            end=_relative(group[-1][2], clip_start, clip_end),
            text=text,
        )

    def flush() -> None:
        if current:
            captions.append(caption(current, " ".join(w[0] for w in current)))
            current.clear()

    for word in selected:
        if len(word[0]) > max_chars:
            flush()
            captions.append(
                caption([word], "\n".join(wrap_caption_text(word[0], max_chars)))
            )
            continue
        length = len(word[0])
        if current:
            length += 1 + len(" ".join(w[0] for w in current))
        if length > max_chars:
            flush()
        current.append(word)
    flush()
    return captions


def _segment_caption(
    segment: _Segment, clip_start: float, clip_end: float, max_chars: int
) -> CaptionLine | None:
    lines = wrap_caption_text(segment.text or "", max_chars)
    if not lines:
        return None
    return CaptionLine(
        start=_relative(segment.start_seconds, clip_start, clip_end),
        end=_relative(segment.end_seconds, clip_start, clip_end),
        text="\n".join(lines),
    )


def _normalize(captions: list[CaptionLine], duration: float) -> list[CaptionLine]:
    """Give every caption a minimum length and remove overlaps."""
    duration = round(duration, 3)
    sized: list[CaptionLine] = []
    for item in sorted(captions, key=lambda c: (c.start, c.end)):
        start = min(max(item.start, 0.0), duration)
        end = min(item.end, duration)
        if end - start < MIN_CAPTION_SECONDS:
            end = min(round(start + MIN_CAPTION_SECONDS, 3), duration)
            if end - start < MIN_CAPTION_SECONDS:
                start = max(round(end - MIN_CAPTION_SECONDS, 3), 0.0)
        sized.append(CaptionLine(start, end, item.text))

    result: list[CaptionLine] = []
    for item in sized:
        if result and item.start < result[-1].end:
            previous = result[-1]
            # Trim only when the previous caption keeps its minimum length;
            # otherwise merge the two.
            if round(item.start - previous.start, 3) >= MIN_CAPTION_SECONDS:
                result[-1] = CaptionLine(previous.start, item.start, previous.text)
            else:
                result[-1] = CaptionLine(
                    previous.start,
                    max(previous.end, item.end),
                    f"{previous.text}\n{item.text}",
                )
                continue
        result.append(item)
    return result


def generate_captions(
    segments: Iterable[_Segment],
    clip_start: float,
    clip_end: float,
    max_chars_per_line: int,
) -> list[CaptionLine]:
    """Return the clip's captions, in time order, with clip-relative times.

    `clip_start` / `clip_end` are source-video seconds. Segments with invalid
    times or outside the clip are ignored. Raises ValueError for an invalid
    clip range or line length.
    """
    if not _is_time(clip_start) or not _is_time(clip_end) or clip_end <= clip_start:
        raise ValueError("The clip range must satisfy 0 <= start < end.")
    if (
        not isinstance(max_chars_per_line, int)
        or isinstance(max_chars_per_line, bool)
        or max_chars_per_line < 1
    ):
        raise ValueError("max_chars_per_line must be a positive integer.")

    usable = [
        segment
        for segment in segments
        if _is_time(segment.start_seconds)
        and _is_time(segment.end_seconds)
        and segment.end_seconds > segment.start_seconds
        and segment.start_seconds < clip_end
        and segment.end_seconds > clip_start
    ]
    captions: list[CaptionLine] = []
    for segment in sorted(usable, key=lambda s: (s.start_seconds, s.end_seconds)):
        words = _segment_words(segment)
        if words is not None:
            captions.extend(
                _word_captions(words, clip_start, clip_end, max_chars_per_line)
            )
            continue
        caption = _segment_caption(segment, clip_start, clip_end, max_chars_per_line)
        if caption is not None:
            captions.append(caption)
    return _normalize(captions, clip_end - clip_start)
