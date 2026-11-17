"""ASS (Advanced SubStation Alpha) subtitles for burning captions into renders.

`render_ass(captions, style, width, height)` returns a complete ASS file with
`[Script Info]` (PlayResX/PlayResY = the output resolution), one `Default`
style in `[V4+ Styles]` and one `Dialogue` per caption in `[Events]`.

- **Times** are caption times (seconds from the clip start) plus
  `time_offset` (0 by default; pass the clip start only when the subtitles are
  burned into the uncut source video). They are written as `H:MM:SS.cc`
  (centiseconds, rounded); a caption is always at least 1 cs long, and
  negative times are clamped to 0.
- **Colors** `#RRGGBB` / `#RRGGBBAA` (CSS alpha, `FF` opaque) become
  `&HAABBGGRR` with ASS alpha (`00` opaque), i.e. the alpha is inverted.
- **Sizes**: font size, outline, shadow and margins are given for a
  1080-pixel shorter side and scaled to the output.
- **Text** is cleaned like stored captions, upper-cased when the style asks
  for it, and wrapped to `max_chars_per_line`. Characters with a special
  meaning in ASS event text are neutralized so caption text can never inject
  override tags: `{` and `}` (override blocks) and `\\` (escapes such as
  `\\N`, `\\h`, `\\n`) are replaced with their full-width look-alikes
  (U+FF5B, U+FF5D, U+FF3C). Line breaks are written as `\\N`. Control
  characters (including raw newlines inside a line) are removed, so the text
  cannot start a new line of the file.
- The font name must be a safe name (letters, digits, space, `_`, `-`); the
  caller checks it against the CAPTION_FONTS allowlist.
"""

import math
from collections.abc import Iterable
from typing import Protocol

from app.captions.generate import wrap_caption_text
from app.captions.styles import (
    PRESET_LOOKS,
    REFERENCE_SHORT_SIDE,
    CaptionPosition,
    CaptionPreset,
    StyleValues,
    normalize_hex_color,
)
from app.core.config import CAPTION_FONT_NAME_PATTERN

ASS_MEDIA_TYPE = "text/x-ssa; charset=utf-8"
STYLE_NAME = "Default"
MIN_RESOLUTION = 16
MAX_RESOLUTION = 8192
# Semi-transparent black behind shadows.
BACK_COLOUR = "&H80000000"
SECONDARY_COLOUR = "&H00FFFFFF"
# Vertical margin (fraction of the height) for top and bottom captions, and
# the side margins (fraction of the width).
VERTICAL_MARGIN_FRACTION = 0.08
SIDE_MARGIN_FRACTION = 0.05

_ALIGNMENT = {
    CaptionPosition.BOTTOM: 2,
    CaptionPosition.MIDDLE: 5,
    CaptionPosition.TOP: 8,
}
_ESCAPES = str.maketrans({"\\": "＼", "{": "｛", "}": "｝"})

STYLE_FORMAT = (
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
    "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
    "MarginR, MarginV, Encoding"
)
EVENT_FORMAT = (
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
)


class _TimedText(Protocol):
    start_seconds: float
    end_seconds: float
    text: str


def hex_to_ass_color(value: str) -> str:
    """Return `&HAABBGGRR` for `#RRGGBB` / `#RRGGBBAA` (CSS alpha, FF opaque).

    Raises ValueError for anything that is not a hex color.
    """
    color = normalize_hex_color(value)
    red, green, blue = color[1:3], color[3:5], color[5:7]
    ass_alpha = 255 - int(color[7:9], 16)
    return f"&H{ass_alpha:02X}{blue}{green}{red}"


def _to_centiseconds(seconds: float) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("A subtitle time must be a number.")
    if not math.isfinite(seconds):
        raise ValueError("A subtitle time must be finite.")
    return max(0, math.floor(seconds * 100 + 0.5))


def _format_centiseconds(total: int) -> str:
    hours, rest = divmod(total, 360_000)
    minutes, rest = divmod(rest, 6000)
    seconds, centiseconds = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def format_ass_timestamp(seconds: float) -> str:
    """Return `H:MM:SS.cc` (negative times are clamped to zero)."""
    return _format_centiseconds(_to_centiseconds(seconds))


def escape_ass_line(line: str) -> str:
    """Neutralize ASS override characters in one line of text."""
    return line.translate(_ESCAPES)


def ass_event_text(text: str, max_chars_per_line: int, uppercase: bool) -> str:
    """Return caption text as safe ASS event text (empty if nothing is left)."""
    if uppercase:
        text = text.upper()
    lines = wrap_caption_text(text, max_chars_per_line)
    return "\\N".join(escape_ass_line(line) for line in lines)


def _check_resolution(value: int, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not MIN_RESOLUTION <= value <= MAX_RESOLUTION
    ):
        raise ValueError(
            f"{name} must be an integer from {MIN_RESOLUTION} to {MAX_RESOLUTION}."
        )


def _number(value: float) -> str:
    return f"{value:g}"


def render_ass(
    captions: Iterable[_TimedText],
    style: StyleValues,
    width: int,
    height: int,
    *,
    time_offset: float = 0.0,
) -> str:
    """Return the captions as an ASS file for a `width` x `height` output.

    Captions are written in start order; captions without text are skipped.
    Raises ValueError for an invalid resolution, font name, color or time.
    """
    _check_resolution(width, "width")
    _check_resolution(height, "height")
    if not CAPTION_FONT_NAME_PATTERN.fullmatch(style.font_family):
        raise ValueError("The font name contains characters that are not allowed.")
    preset = CaptionPreset(style.preset)
    look = PRESET_LOOKS[preset]
    scale = min(width, height) / REFERENCE_SHORT_SIDE
    position = CaptionPosition(style.position)
    margin_side = round(width * SIDE_MARGIN_FRACTION)
    margin_vertical = (
        0
        if position is CaptionPosition.MIDDLE
        else round(height * VERTICAL_MARGIN_FRACTION)
    )
    style_fields = [
        STYLE_NAME,
        style.font_family,
        str(max(1, round(style.font_size * scale))),
        hex_to_ass_color(style.primary_color),
        SECONDARY_COLOUR,
        hex_to_ass_color(style.outline_color),
        BACK_COLOUR,
        "-1" if look.bold else "0",
        "0",
        "0",
        "0",
        "100",
        "100",
        "0",
        "0",
        "1",
        _number(round(look.outline * scale, 2)),
        _number(round(look.shadow * scale, 2)),
        str(_ALIGNMENT[position]),
        str(margin_side),
        str(margin_side),
        str(margin_vertical),
        "1",
    ]

    events = []
    for caption in sorted(captions, key=lambda c: _to_centiseconds(c.start_seconds)):
        text = ass_event_text(caption.text, style.max_chars_per_line, style.uppercase)
        if not text:
            continue
        start = _to_centiseconds(caption.start_seconds + time_offset)
        end = max(_to_centiseconds(caption.end_seconds + time_offset), start + 1)
        events.append(
            f"Dialogue: 0,{_format_centiseconds(start)},{_format_centiseconds(end)},"
            f"{STYLE_NAME},,0,0,0,,{text}"
        )

    lines = [
        "[Script Info]",
        "; Generated by ClipForge AI",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        # Lines are wrapped here; libass must not re-wrap them.
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: None",
        "",
        "[V4+ Styles]",
        STYLE_FORMAT,
        "Style: " + ",".join(style_fields),
        "",
        "[Events]",
        EVENT_FORMAT,
        *events,
    ]
    return "\n".join(lines) + "\n"
