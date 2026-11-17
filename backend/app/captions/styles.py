"""Caption style presets, bounds and colors.

A style has a `preset` (`classic`, `bold`, `karaoke`, `minimal`), which sets
the defaults of the other fields and how the captions are drawn (weight,
outline and shadow), plus:

- `font_family`: one of the CAPTION_FONTS setting (an allowlist);
- `font_size`: FONT_SIZE_MIN to FONT_SIZE_MAX pixels for an output whose
  shorter side is REFERENCE_SHORT_SIDE pixels (scaled for other sizes);
- `primary_color` (text) and `outline_color`: hex colors `#RRGGBB` or
  `#RRGGBBAA`, stored as upper-case `#RRGGBBAA`. The alpha is CSS-style
  (`FF` opaque, `00` transparent); the text must be at least
  MIN_PRIMARY_ALPHA opaque so captions stay visible;
- `position`: `bottom`, `middle` or `top`;
- `uppercase`: whether captions are drawn in capitals (the stored text is not
  changed);
- `max_chars_per_line`: MAX_CHARS_PER_LINE_MIN to MAX_CHARS_PER_LINE_MAX.
"""

import enum
import re
from collections.abc import Sequence
from dataclasses import dataclass

FONT_SIZE_MIN = 16
FONT_SIZE_MAX = 160
MAX_CHARS_PER_LINE_MIN = 10
MAX_CHARS_PER_LINE_MAX = 80
# Font sizes are given for this shorter output side (1080x1920 portrait).
REFERENCE_SHORT_SIDE = 1080
# Text must be at least this opaque (CSS-style alpha, FF = opaque).
MIN_PRIMARY_ALPHA = 0x80
FONT_FAMILY_MAX_LENGTH = 64
COLOR_MAX_LENGTH = 9

_HEX_COLOR = re.compile(r"#([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?")


class CaptionPreset(str, enum.Enum):
    CLASSIC = "classic"
    BOLD = "bold"
    KARAOKE = "karaoke"
    MINIMAL = "minimal"


class CaptionPosition(str, enum.Enum):
    BOTTOM = "bottom"
    MIDDLE = "middle"
    TOP = "top"


@dataclass(frozen=True)
class PresetDefaults:
    font_size: int
    primary_color: str
    outline_color: str
    position: CaptionPosition
    uppercase: bool
    max_chars_per_line: int


@dataclass(frozen=True)
class PresetLook:
    """How a preset is drawn; sizes are pixels at REFERENCE_SHORT_SIDE."""

    bold: bool
    outline: float
    shadow: float


PRESET_DEFAULTS: dict[CaptionPreset, PresetDefaults] = {
    CaptionPreset.CLASSIC: PresetDefaults(
        64, "#FFFFFFFF", "#000000FF", CaptionPosition.BOTTOM, False, 32
    ),
    CaptionPreset.BOLD: PresetDefaults(
        80, "#FFFFFFFF", "#000000FF", CaptionPosition.BOTTOM, True, 24
    ),
    CaptionPreset.KARAOKE: PresetDefaults(
        72, "#FFE14DFF", "#000000FF", CaptionPosition.BOTTOM, True, 24
    ),
    CaptionPreset.MINIMAL: PresetDefaults(
        52, "#FFFFFFFF", "#00000080", CaptionPosition.BOTTOM, False, 36
    ),
}

PRESET_LOOKS: dict[CaptionPreset, PresetLook] = {
    CaptionPreset.CLASSIC: PresetLook(bold=False, outline=3.0, shadow=1.0),
    CaptionPreset.BOLD: PresetLook(bold=True, outline=5.0, shadow=0.0),
    CaptionPreset.KARAOKE: PresetLook(bold=True, outline=4.0, shadow=2.0),
    CaptionPreset.MINIMAL: PresetLook(bold=False, outline=1.5, shadow=0.0),
}

DEFAULT_PRESET = CaptionPreset.CLASSIC


@dataclass(frozen=True)
class StyleValues:
    """A complete, validated caption style."""

    preset: CaptionPreset
    font_family: str
    font_size: int
    primary_color: str
    outline_color: str
    position: CaptionPosition
    uppercase: bool
    max_chars_per_line: int


def normalize_hex_color(value: str) -> str:
    """Return `#RRGGBBAA` (upper case) for `#RRGGBB` or `#RRGGBBAA`.

    `#RRGGBB` is opaque. Raises ValueError for anything else (names, `rgb()`,
    3-digit forms, missing `#`).
    """
    if not isinstance(value, str):
        raise ValueError("A color must be a string.")
    match = _HEX_COLOR.fullmatch(value.strip())
    if match is None:
        raise ValueError("A color must be #RRGGBB or #RRGGBBAA.")
    rgb, alpha = match.groups()
    return f"#{rgb}{alpha or 'FF'}".upper()


def color_alpha(color: str) -> int:
    """Return the CSS-style alpha (0-255) of a normalized color."""
    return int(normalize_hex_color(color)[7:9], 16)


def match_allowed_font(name: str, allowed: Sequence[str]) -> str | None:
    """Return the allowlist's spelling of `name` (case-insensitive), or None."""
    wanted = " ".join(name.split()).casefold()
    for font in allowed:
        if font.casefold() == wanted:
            return font
    return None


def default_style(
    font_family: str, preset: CaptionPreset = DEFAULT_PRESET
) -> StyleValues:
    """Return the preset's default style with the given font."""
    defaults = PRESET_DEFAULTS[preset]
    return StyleValues(
        preset=preset,
        font_family=font_family,
        font_size=defaults.font_size,
        primary_color=defaults.primary_color,
        outline_color=defaults.outline_color,
        position=defaults.position,
        uppercase=defaults.uppercase,
        max_chars_per_line=defaults.max_chars_per_line,
    )
