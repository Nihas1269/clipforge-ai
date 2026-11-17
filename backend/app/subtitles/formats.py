"""SubRip (SRT) and WebVTT formatting, as pure functions.

Cue times are seconds in source-video time, exactly as stored on the
transcript segments; they are only rounded to whole milliseconds. Cues are
written in start-time order (stable, so equal starts keep their order) and
numbered 1, 2, 3, ... in the order they are written.

Text handling, the same for both formats:

- Control characters are removed and all whitespace (including line breaks)
  is collapsed, so a caption can never contain the blank line that ends a
  cue. Cues whose text is empty after this are skipped (the numbering stays
  sequential).
- Long captions are wrapped at word boundaries into lines of at most
  MAX_LINE_LENGTH characters (a single longer word stays on its own line);
  no text is dropped and the cue is never split, so its times stay exact.
- WebVTT text is escaped (`&`, `<`, `>`), which also keeps the reserved
  `-->` out of cue text. SRT has no escaping, so `-->` is written as `->`.

Overlapping cues are kept as they are: both formats allow them, and changing
the times would break the link back to the source timestamps. A cue whose
rounded end is not after its rounded start is given a length of 1 ms.
"""

import math
import re
import textwrap
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

SRT_MEDIA_TYPE = "application/x-subrip; charset=utf-8"
VTT_MEDIA_TYPE = "text/vtt; charset=utf-8"
VTT_HEADER = "WEBVTT"

MAX_LINE_LENGTH = 42
FILENAME_STEM_MAX_LENGTH = 100
DEFAULT_FILENAME_STEM = "transcript"

_MS_PER_HOUR = 3_600_000
_MS_PER_MINUTE = 60_000
_MS_PER_SECOND = 1000

# Everything below U+0020 except tab/newline/carriage return (whitespace,
# collapsed separately), DEL, and the C1 controls.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_UNSAFE_FILENAME_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Cue:
    """A caption shown from `start` to `end` (seconds in source-video time)."""

    start: float
    end: float
    text: str


class _TimedText(Protocol):
    start_seconds: float
    end_seconds: float
    text: str


def cues_from_segments(segments: Iterable[_TimedText]) -> list[Cue]:
    """Return one cue per transcript segment, with the segment's own times."""
    return [
        Cue(start=segment.start_seconds, end=segment.end_seconds, text=segment.text)
        for segment in segments
    ]


def _to_milliseconds(seconds: float) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("A subtitle time must be a number.")
    if not math.isfinite(seconds):
        raise ValueError("A subtitle time must be finite.")
    return max(0, math.floor(seconds * 1000 + 0.5))


def _format_milliseconds(total_ms: int, separator: str) -> str:
    hours, rest = divmod(total_ms, _MS_PER_HOUR)
    minutes, rest = divmod(rest, _MS_PER_MINUTE)
    seconds, milliseconds = divmod(rest, _MS_PER_SECOND)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{milliseconds:03d}"


def format_srt_timestamp(seconds: float) -> str:
    """Return `HH:MM:SS,mmm` (negative times are clamped to zero)."""
    return _format_milliseconds(_to_milliseconds(seconds), ",")


def format_vtt_timestamp(seconds: float) -> str:
    """Return `HH:MM:SS.mmm` (negative times are clamped to zero)."""
    return _format_milliseconds(_to_milliseconds(seconds), ".")


def _caption_lines(text: str) -> list[str]:
    words = _CONTROL_CHARACTERS.sub("", text or "").split()
    if not words:
        return []
    return textwrap.wrap(
        " ".join(words),
        width=MAX_LINE_LENGTH,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _prepared(cues: Iterable[Cue]) -> list[tuple[int, int, list[str]]]:
    """Return (start_ms, end_ms, lines) for every non-empty cue, in order."""
    prepared = []
    for cue in sorted(cues, key=lambda cue: _to_milliseconds(cue.start)):
        lines = _caption_lines(cue.text)
        if not lines:
            continue
        start = _to_milliseconds(cue.start)
        end = max(_to_milliseconds(cue.end), start + 1)
        prepared.append((start, end, lines))
    return prepared


def to_srt(cues: Sequence[Cue]) -> str:
    """Return the cues as a SubRip file (empty for no cues)."""
    blocks = []
    for number, (start, end, lines) in enumerate(_prepared(cues), start=1):
        text = "\n".join(line.replace("-->", "->") for line in lines)
        timing = (
            f"{_format_milliseconds(start, ',')} --> {_format_milliseconds(end, ',')}"
        )
        blocks.append(f"{number}\n{timing}\n{text}\n")
    return "\n".join(blocks)


def _escape_vtt(line: str) -> str:
    return line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_vtt(cues: Sequence[Cue]) -> str:
    """Return the cues as a WebVTT file (just the header for no cues)."""
    blocks = [f"{VTT_HEADER}\n"]
    for number, (start, end, lines) in enumerate(_prepared(cues), start=1):
        text = "\n".join(_escape_vtt(line) for line in lines)
        timing = (
            f"{_format_milliseconds(start, '.')} --> {_format_milliseconds(end, '.')}"
        )
        blocks.append(f"{number}\n{timing}\n{text}\n")
    return "\n".join(blocks)


def subtitle_filename(original_filename: str | None, extension: str) -> str:
    """Return a download file name such as `talk.srt` for a video's subtitles.

    Only letters, digits, ".", "_" and "-" are kept from the video's file
    name (others become "_"), so the result is safe in a quoted
    Content-Disposition header; DEFAULT_FILENAME_STEM is used if nothing is
    left.
    """
    name = original_filename or ""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    safe = _UNSAFE_FILENAME_CHARACTERS.sub("_", stem).strip("._-")
    safe = safe[:FILENAME_STEM_MAX_LENGTH].rstrip("._-")
    return f"{safe or DEFAULT_FILENAME_STEM}.{extension}"
