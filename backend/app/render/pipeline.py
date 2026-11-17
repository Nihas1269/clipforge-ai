"""Pure building blocks of the clip render: FFmpeg arguments and output checks.

`build_ffmpeg_args(source, output, start=..., end=..., plan=...)` returns the
argument list (for app.media.ffmpeg.run_ffmpeg, never a shell) that renders
the clip `start`-`end` of `source` as an H.264 + AAC MP4:

1. **Seek and trim.** `-ss <start>` is an input option with `-accurate_seek`,
   so FFmpeg decodes from the keyframe before `start` and drops every frame
   before it (frame-accurate), and `-t <end - start>` limits the output. The
   output starts at timestamp 0.
2. **Crop.** The plan's keyframes are in **source-video seconds** (like every
   time in CropPlan). They are converted to **clip-relative** seconds
   (`time - start`, at least 0) for the filter, whose `t` starts at 0 because
   of the input seek. One keyframe (or a crop that never moves) gives a
   constant `crop=w=W:h=H:x=X:y=Y`; several give piecewise-linear `x` (and
   `y`) expressions that interpolate between keyframes and hold the first and
   last positions before and after them. Every interpolated position lies
   between two valid keyframe positions, so the crop always stays inside the
   frame. At most MAX_CROP_KEYFRAMES keyframes are used (evenly chosen,
   keeping the first and last) to bound the expression size, and segments
   are selected with a balanced if() tree to keep the nesting shallow.
3. **Scale** to the output resolution of the aspect ratio
   (OUTPUT_RESOLUTIONS, all even) with `setsar=1`.
4. **Captions** (optional): the `ass` filter with the generated ASS file,
   applied after scaling so the ASS PlayRes equals the output size. The
   path is escaped for FFmpeg's filter syntax (escape_filter_path), including
   Windows drive letters and backslashes.
5. **Output**: `libx264` (yuv420p) + `aac`, `-movflags +faststart`, metadata
   of the source dropped, MP4 container, and `-fs` when a maximum output
   size is given.

`check_render_output(info, ...)` validates what ffprobe reported about the
rendered file: video stream, exact output size, duration within
DURATION_TOLERANCE_SECONDS of the clip length, and an audio stream when the
source has one.
"""

import math
import os
from collections.abc import Sequence
from pathlib import Path

from app.media.ffmpeg import INPUT_PROTOCOLS, MediaInfo
from app.reframe.planner import CropPlan

# Output size for each aspect ratio (width, height); every value is even.
OUTPUT_RESOLUTIONS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "16:9": (1920, 1080),
}

# The rendered duration may differ from `end - start` by at most this much:
# the last video frame and the last AAC frame (1024 samples) can each run a
# little past the cut, and container durations are rounded.
DURATION_TOLERANCE_SECONDS = 0.25

# Upper bound on the keyframes turned into crop expressions. Face tracking
# can produce thousands; FFmpeg evaluates the expression for every frame, so
# its size is bounded.
MAX_CROP_KEYFRAMES = 240

VIDEO_CODEC = "libx264"
VIDEO_PRESET = "veryfast"
VIDEO_CRF = "20"
PIXEL_FORMAT = "yuv420p"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "160k"
SCALE_FLAGS = "lanczos"

# Characters escaped at each level of FFmpeg's filter syntax: first inside a
# filter option value, then in the filtergraph description.
_OPTION_SPECIAL = "\\':"
_GRAPH_SPECIAL = "\\'[],;"


class RenderValidationError(Exception):
    """The rendered file is not the expected video.

    `reason` is a short machine-readable code (`missing`, `empty`,
    `no_video`, `wrong_size`, `no_duration`, `wrong_duration`, `no_audio`).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def output_resolution(aspect: str) -> tuple[int, int]:
    """Return the `(width, height)` rendered for `aspect` or raise ValueError."""
    if not isinstance(aspect, str) or aspect not in OUTPUT_RESOLUTIONS:
        supported = ", ".join(OUTPUT_RESOLUTIONS)
        raise ValueError(f"Unsupported aspect ratio; use one of {supported}.")
    return OUTPUT_RESOLUTIONS[aspect]


def _is_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _format_number(value: float) -> str:
    """Return `value` with at most 6 decimals and no trailing zeros."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _escape(text: str, special: str) -> str:
    return "".join("\\" + char if char in special else char for char in text)


def escape_filter_value(value: str) -> str:
    """Escape `value` for use as a filter option value inside a filtergraph."""
    return _escape(_escape(value, _OPTION_SPECIAL), _GRAPH_SPECIAL)


def escape_filter_path(
    path: str | os.PathLike[str], *, windows: bool | None = None
) -> str:
    """Return `path` escaped for a filter option such as `ass=filename=...`.

    On Windows (`windows` defaults to the current platform) backslashes are
    turned into forward slashes first, which FFmpeg accepts, so only the
    drive letter's `:` needs escaping. Then `\\`, `'` and `:` are escaped for
    the option parser and `\\`, `'`, `[`, `]`, `,` and `;` for the
    filtergraph parser.
    """
    text = os.fspath(path)
    if windows is None:
        windows = os.name == "nt"
    if windows:
        text = text.replace("\\", "/")
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ValueError("The path contains characters that cannot be escaped.")
    return escape_filter_value(text)


def _absolute_path(path: str | os.PathLike[str], name: str) -> str:
    text = os.fspath(path)
    # An absolute path never starts with "-", so it cannot be read as an option.
    if not isinstance(text, str) or not Path(text).is_absolute():
        raise ValueError(f"{name} must be an absolute path.")
    if "\x00" in text:
        raise ValueError(f"{name} must not contain NUL characters.")
    return text


# Crop expressions


def _crop_points(
    plan: CropPlan, start: float, duration: float
) -> list[tuple[float, int, int]]:
    """Return `(clip_time, x, y)` points for the clip, simplified and bounded."""
    points: list[tuple[float, int, int]] = []
    for keyframe in plan.keyframes:
        time = max(0.0, round(keyframe.time_seconds - start, 6))
        if points and time <= points[-1][0]:
            # The same (clamped) time: the later keyframe wins.
            points[-1] = (points[-1][0], keyframe.x, keyframe.y)
        else:
            points.append((time, keyframe.x, keyframe.y))
        if time >= duration:
            # Later keyframes are never shown; this one still sets the
            # direction of the last pan.
            break

    # Interior points equal to both neighbours change nothing.
    simplified = [
        point
        for index, point in enumerate(points)
        if index in (0, len(points) - 1)
        or point[1:] != points[index - 1][1:]
        or point[1:] != points[index + 1][1:]
    ]
    if len(simplified) > MAX_CROP_KEYFRAMES:
        last = len(simplified) - 1
        indexes = sorted(
            {
                round(i * last / (MAX_CROP_KEYFRAMES - 1))
                for i in range(MAX_CROP_KEYFRAMES)
            }
        )
        simplified = [simplified[i] for i in indexes]
    return simplified


def _segment_expression(
    times: Sequence[float], values: Sequence[int], index: int
) -> str:
    t0, t1 = times[index], times[index + 1]
    v0, v1 = values[index], values[index + 1]
    if v0 == v1:
        return str(v0)
    return f"{v0}+({v1 - v0})*(t-{_format_number(t0)})/{_format_number(t1 - t0)}"


def _segment_tree(
    times: Sequence[float], values: Sequence[int], first: int, stop: int
) -> str:
    """Select segment `first` .. `stop - 1` by `t` with a balanced if() tree."""
    if stop - first == 1:
        return _segment_expression(times, values, first)
    middle = (first + stop) // 2
    return (
        f"if(lt(t,{_format_number(times[middle])}),"
        f"{_segment_tree(times, values, first, middle)},"
        f"{_segment_tree(times, values, middle, stop)})"
    )


def piecewise_linear_expression(times: Sequence[float], values: Sequence[int]) -> str:
    """Return an FFmpeg expression of `t` interpolating `values` at `times`.

    Before the first time the first value holds, after the last time the last
    one. `times` must be strictly increasing. A constant gives just the
    number. Segments are chosen with a balanced tree of if() calls, so the
    nesting depth grows with log2 of the number of keyframes (FFmpeg's
    expression parser refuses more than about 100 nested levels).
    """
    if not values or len(times) != len(values):
        raise ValueError("times and values must be non-empty and the same length.")
    if any(t1 <= t0 for t0, t1 in zip(times, times[1:], strict=False)):
        raise ValueError("times must be strictly increasing.")
    if all(value == values[0] for value in values):
        return str(values[0])
    tree = _segment_tree(times, values, 0, len(values) - 1)
    expression = f"if(lt(t,{_format_number(times[-1])}),{tree},{values[-1]})"
    if times[0] > 0:
        expression = f"if(lt(t,{_format_number(times[0])}),{values[0]},{expression})"
    return expression


def crop_filter(plan: CropPlan, start: float, duration: float) -> str:
    """Return the (filtergraph-escaped) crop filter for the clip."""
    points = _crop_points(plan, start, duration)
    times = [point[0] for point in points]
    x = piecewise_linear_expression(times, [point[1] for point in points])
    y = piecewise_linear_expression(times, [point[2] for point in points])
    return (
        f"crop=w={plan.crop_width}:h={plan.crop_height}"
        f":x={escape_filter_value(x)}:y={escape_filter_value(y)}"
    )


def build_ffmpeg_args(
    source_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str],
    *,
    start: float,
    end: float,
    plan: CropPlan,
    subtitles_path: str | os.PathLike[str] | None = None,
    max_output_bytes: int | None = None,
) -> list[str]:
    """Return the FFmpeg arguments that render the clip `start`-`end`.

    `start` and `end` are source-video seconds; `plan` is the crop plan for
    the clip (its keyframes in source-video seconds) and decides the aspect
    ratio and so the output resolution. `subtitles_path` is the ASS file to
    burn in (its times relative to the clip start), or None.
    `max_output_bytes` adds `-fs`, so FFmpeg stops writing once the output
    reaches that size instead of filling the local disk (the file is then
    cut short, at least that size, and must be rejected). All paths must be
    absolute. Raises ValueError for invalid arguments.
    """
    if not (_is_number(start) and _is_number(end) and 0 <= start < end):
        raise ValueError("start and end must be finite with 0 <= start < end.")
    if max_output_bytes is not None and (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or max_output_bytes <= 0
    ):
        raise ValueError("max_output_bytes must be a positive integer.")
    width, height = output_resolution(plan.aspect)
    source = _absolute_path(source_path, "source_path")
    output = _absolute_path(output_path, "output_path")
    if source == output:
        raise ValueError("output_path must differ from source_path.")
    duration = end - start

    filters = [
        crop_filter(plan, start, duration),
        f"scale={width}:{height}:flags={SCALE_FLAGS}",
        "setsar=1",
    ]
    if subtitles_path is not None:
        subtitles = _absolute_path(subtitles_path, "subtitles_path")
        filters.append(f"ass=filename={escape_filter_path(subtitles)}")
    filters.append(f"format={PIXEL_FORMAT}")
    size_limit = ["-fs", str(max_output_bytes)] if max_output_bytes is not None else []

    return [
        "-accurate_seek",
        "-ss",
        _format_number(start),
        "-protocol_whitelist",
        INPUT_PROTOCOLS,
        "-i",
        source,
        "-t",
        _format_number(duration),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        ",".join(filters),
        "-c:v",
        VIDEO_CODEC,
        "-preset",
        VIDEO_PRESET,
        "-crf",
        VIDEO_CRF,
        "-pix_fmt",
        PIXEL_FORMAT,
        "-c:a",
        AUDIO_CODEC,
        "-b:a",
        AUDIO_BITRATE,
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        *size_limit,
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        "-y",
        output,
    ]


# Output validation


def check_render_output(
    info: MediaInfo,
    *,
    size_bytes: int,
    width: int,
    height: int,
    duration: float,
    expect_audio: bool,
    tolerance: float = DURATION_TOLERANCE_SECONDS,
) -> None:
    """Raise RenderValidationError unless `info` describes the expected render.

    The file must be non-empty and have a video stream of exactly
    `width` x `height` (and so the expected aspect ratio), a duration within
    `tolerance` seconds of `duration`, and an audio stream if `expect_audio`.
    """
    if size_bytes <= 0:
        raise RenderValidationError("empty", "The rendered file is empty.")
    if not info.has_video:
        raise RenderValidationError("no_video", "The rendered file has no video.")
    if (info.width, info.height) != (width, height):
        raise RenderValidationError(
            "wrong_size",
            f"The rendered video is {info.width}x{info.height}, "
            f"expected {width}x{height}.",
        )
    if info.duration_seconds is None:
        raise RenderValidationError("no_duration", "The rendered file has no duration.")
    if abs(info.duration_seconds - duration) > tolerance:
        raise RenderValidationError(
            "wrong_duration",
            f"The rendered video lasts {info.duration_seconds:.3f} s, "
            f"expected {duration:.3f} s.",
        )
    if expect_audio and not info.has_audio:
        raise RenderValidationError("no_audio", "The rendered file has no audio.")


__all__ = [
    "DURATION_TOLERANCE_SECONDS",
    "MAX_CROP_KEYFRAMES",
    "OUTPUT_RESOLUTIONS",
    "RenderValidationError",
    "build_ffmpeg_args",
    "check_render_output",
    "crop_filter",
    "escape_filter_path",
    "escape_filter_value",
    "output_resolution",
    "piecewise_linear_expression",
]
