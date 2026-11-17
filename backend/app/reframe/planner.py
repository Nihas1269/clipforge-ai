"""Pure crop-window planning for reframing video to another aspect ratio.

Coordinates are integer pixels in the source frame. A keyframe `(t, x, y)`
places the crop window's **top-left corner** at `(x, y)` from `t` seconds of
source-video time on; the window is `crop_width` x `crop_height` pixels and
always lies entirely inside the source frame:

    0 <= x <= source_width - crop_width
    0 <= y <= source_height - crop_height

Crop sizes are even numbers (required by the common yuv420p encoders) and
within 2 pixels of the exact requested aspect ratio. Nothing in this module
reads files or imports OpenCV.
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import NamedTuple

# Requested output aspect ratio -> (width units, height units).
ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "9:16": (9, 16),
    "1:1": (1, 1),
    "4:5": (4, 5),
    "16:9": (16, 9),
}

# Below this size a crop cannot keep the aspect ratio with even dimensions.
MIN_SOURCE_DIMENSION = 16
MAX_SOURCE_DIMENSION = 65535


class Keyframe(NamedTuple):
    """Top-left crop position `(x, y)` from `time_seconds` (source time) on."""

    time_seconds: float
    x: int
    y: int


@dataclass(frozen=True)
class CropPlan:
    """A crop window size and where it sits over time.

    `mode` is the mode actually used (`center` or `face`); `fallback_reason`
    says why a face-mode request produced a center plan, otherwise None.
    `detector` identifies the face detector (and its parameters) that
    produced a face plan; it is None for center plans.
    """

    source_width: int
    source_height: int
    aspect: str
    crop_width: int
    crop_height: int
    keyframes: tuple[Keyframe, ...]
    mode: str = "center"
    fallback_reason: str | None = None
    detector: str | None = None

    def __post_init__(self) -> None:
        if not 0 < self.crop_width <= self.source_width:
            raise ValueError("The crop width must fit inside the source frame.")
        if not 0 < self.crop_height <= self.source_height:
            raise ValueError("The crop height must fit inside the source frame.")
        if not self.keyframes:
            raise ValueError("A crop plan needs at least one keyframe.")
        max_x = self.source_width - self.crop_width
        max_y = self.source_height - self.crop_height
        previous = -math.inf
        for keyframe in self.keyframes:
            if not math.isfinite(keyframe.time_seconds) or keyframe.time_seconds < 0:
                raise ValueError("Keyframe times must be finite and non-negative.")
            if keyframe.time_seconds < previous:
                raise ValueError("Keyframes must be in chronological order.")
            if not (0 <= keyframe.x <= max_x and 0 <= keyframe.y <= max_y):
                raise ValueError("Every keyframe must keep the crop inside the frame.")
            previous = keyframe.time_seconds


def parse_aspect(aspect: str) -> tuple[int, int]:
    """Return the `(width, height)` units of a supported aspect such as `9:16`."""
    if not isinstance(aspect, str) or aspect not in ASPECT_RATIOS:
        supported = ", ".join(ASPECT_RATIOS)
        raise ValueError(f"Unsupported aspect ratio; use one of {supported}.")
    return ASPECT_RATIOS[aspect]


def _check_dimension(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer number of pixels.")
    if not MIN_SOURCE_DIMENSION <= value <= MAX_SOURCE_DIMENSION:
        raise ValueError(
            f"{name} must be between {MIN_SOURCE_DIMENSION} and "
            f"{MAX_SOURCE_DIMENSION} pixels."
        )
    return value


def _even_floor(value: int) -> int:
    return value - value % 2


def compute_crop_size(src_w: int, src_h: int, aspect: str) -> tuple[int, int]:
    """Return the largest even `(width, height)` crop of the given aspect.

    The side that limits the crop uses the full (even) source size; the other
    side is the nearest even number to the exact ratio, so the result is
    within 2 pixels of the requested aspect ratio.
    """
    src_w = _check_dimension(src_w, "The source width")
    src_h = _check_dimension(src_h, "The source height")
    ratio_w, ratio_h = parse_aspect(aspect)

    if src_w * ratio_h >= src_h * ratio_w:
        # Source is wider than (or as wide as) the target: keep the height.
        crop_h = _even_floor(src_h)
        crop_w = (crop_h * ratio_w + ratio_h) // (2 * ratio_h) * 2
        crop_w = min(crop_w, _even_floor(src_w))
    else:
        # Source is taller than the target: keep the width.
        crop_w = _even_floor(src_w)
        crop_h = (crop_w * ratio_h + ratio_w) // (2 * ratio_w) * 2
        crop_h = min(crop_h, _even_floor(src_h))
    return max(crop_w, 2), max(crop_h, 2)


def clamp_position(
    x: float, y: float, crop_w: int, crop_h: int, src_w: int, src_h: int
) -> tuple[int, int]:
    """Round a top-left position to pixels and keep the crop inside the frame."""
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError("Crop positions must be finite numbers.")
    clamped_x = min(max(round(x), 0), src_w - crop_w)
    clamped_y = min(max(round(y), 0), src_h - crop_h)
    return clamped_x, clamped_y


def center_crop_plan(
    src_w: int, src_h: int, aspect: str, start: float = 0.0
) -> CropPlan:
    """Return a plan with one keyframe placing the crop in the frame's center."""
    crop_w, crop_h = compute_crop_size(src_w, src_h, aspect)
    keyframe = Keyframe(float(start), (src_w - crop_w) // 2, (src_h - crop_h) // 2)
    return CropPlan(
        source_width=src_w,
        source_height=src_h,
        aspect=aspect,
        crop_width=crop_w,
        crop_height=crop_h,
        keyframes=(keyframe,),
        mode="center",
    )


def smooth_keyframes(
    keyframes: Sequence[Keyframe] | Iterable[tuple[float, int, int]],
    max_step_px: float,
) -> list[Keyframe]:
    """Limit how far the crop moves between consecutive keyframes.

    Each keyframe moves from the previous (smoothed) position toward its own
    position by at most `max_step_px` (straight-line distance), so a sudden
    jump becomes a gradual pan. Every output position lies between the
    previous output and the input position on each axis, so if the inputs are
    inside the frame the outputs are too. Times are kept unchanged.
    """
    if (
        isinstance(max_step_px, bool)
        or not isinstance(max_step_px, (int, float))
        or not math.isfinite(max_step_px)
        or max_step_px <= 0
    ):
        raise ValueError("max_step_px must be a positive, finite number.")

    smoothed: list[Keyframe] = []
    for time_seconds, x, y in keyframes:
        if smoothed and time_seconds < smoothed[-1].time_seconds:
            raise ValueError("Keyframes must be in chronological order.")
        if not smoothed:
            smoothed.append(Keyframe(float(time_seconds), int(x), int(y)))
            continue
        previous = smoothed[-1]
        dx = int(x) - previous.x
        dy = int(y) - previous.y
        distance = math.hypot(dx, dy)
        if distance > max_step_px:
            scale = max_step_px / distance
            # int() truncates toward zero, i.e. toward the previous position,
            # so the step never exceeds max_step_px.
            dx = int(dx * scale)
            dy = int(dy * scale)
        smoothed.append(Keyframe(float(time_seconds), previous.x + dx, previous.y + dy))
    return smoothed


def crop_plan_from_centers(
    src_w: int,
    src_h: int,
    aspect: str,
    centers: Sequence[tuple[float, float, float]],
    max_step_px: float,
    detector: str | None = None,
) -> CropPlan:
    """Build a face-mode plan whose crop follows `(t, center_x, center_y)` points.

    Each center is turned into a top-left position clamped inside the frame,
    then the positions are smoothed with `smooth_keyframes`. `detector` names
    the detector that found the centers and is recorded on the plan.
    """
    if not centers:
        raise ValueError("At least one center is needed.")
    crop_w, crop_h = compute_crop_size(src_w, src_h, aspect)
    raw = []
    for time_seconds, center_x, center_y in centers:
        x, y = clamp_position(
            center_x - crop_w / 2, center_y - crop_h / 2, crop_w, crop_h, src_w, src_h
        )
        raw.append(Keyframe(float(time_seconds), x, y))
    return CropPlan(
        source_width=src_w,
        source_height=src_h,
        aspect=aspect,
        crop_width=crop_w,
        crop_height=crop_h,
        keyframes=tuple(smooth_keyframes(raw, max_step_px)),
        mode="face",
        detector=detector,
    )
