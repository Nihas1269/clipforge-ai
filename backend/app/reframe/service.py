"""Choose the crop plan for a clip: center crop, or face tracking with fallback."""

import logging
import math
import os
from dataclasses import replace
from pathlib import Path

from app.media.ffmpeg import MediaError, probe
from app.reframe.face_tracking import (
    DEFAULT_SAMPLE_FPS,
    DEFAULT_TIMEOUT_SECONDS,
    FaceDetector,
    FrameReader,
    NoFacesDetectedError,
    check_timeout,
    sample_times,
    track_faces,
)
from app.reframe.planner import (
    CropPlan,
    center_crop_plan,
    compute_crop_size,
    smooth_keyframes,
)

logger = logging.getLogger(__name__)

REFRAME_MODES = ("center", "face")


class ReframeError(Exception):
    """The clip cannot be reframed at all (invalid input or unknown size)."""


def _is_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _probe_source(video_path: Path) -> tuple[tuple[int, int], float]:
    """Return the video's `(width, height)` and duration, read with ffprobe."""
    try:
        info = probe(video_path)
    except MediaError as exc:
        raise ReframeError("The video could not be probed.") from exc
    if not info.width or not info.height:
        raise ReframeError("The video's dimensions could not be determined.")
    if not info.duration_seconds:
        raise ReframeError("The video's duration could not be determined.")
    return (info.width, info.height), info.duration_seconds


def plan_reframe(
    video_path: str | os.PathLike[str],
    start: float,
    end: float,
    aspect: str,
    mode: str = "center",
    *,
    source_size: tuple[int, int] | None = None,
    media_duration: float | None = None,
    detector: FaceDetector | None = None,
    frame_reader: FrameReader | None = None,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
    max_step_px: float | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> CropPlan:
    """Return the crop plan for the clip `start`-`end` (source seconds).

    `source_size` is the video's `(width, height)` and `media_duration` its
    duration in seconds; they must be given together, and when both are
    omitted they are read with ffprobe. The clip must lie within the media
    (`end <= media_duration`). In `face` mode, any failure of face tracking
    (OpenCV or the Haar cascade missing, unreadable or rotated video,
    detector error, time limit exceeded, no face found) falls back to a
    center crop, with `fallback_reason` set on the plan. Invalid arguments
    raise ValueError; a video that cannot be probed or whose size or duration
    is unknown raises ReframeError.
    """
    if mode not in REFRAME_MODES:
        raise ValueError("mode must be 'center' or 'face'.")
    if not (_is_number(start) and _is_number(end) and 0 <= start < end):
        raise ValueError("start and end must be finite with 0 <= start < end.")
    if (source_size is None) != (media_duration is None):
        raise ValueError("source_size and media_duration must be given together.")
    path = Path(video_path)
    if source_size is None:
        source_size, media_duration = _probe_source(path)
    if media_duration is None or not (
        _is_number(media_duration) and media_duration > 0
    ):
        raise ValueError("media_duration must be a positive, finite number.")
    if end > media_duration:
        raise ValueError("The clip must end within the video's duration.")
    src_w, src_h = source_size
    # Validates the size and aspect before any detection work.
    compute_crop_size(src_w, src_h, aspect)

    if mode == "center":
        plan = center_crop_plan(src_w, src_h, aspect, start=start)
    else:
        # Invalid arguments are errors, not detection failures to fall back from.
        sample_times(start, end, sample_fps)
        check_timeout(timeout_seconds)
        if max_step_px is not None:
            smooth_keyframes([], max_step_px)
        try:
            plan = track_faces(
                path,
                start,
                end,
                aspect,
                (src_w, src_h),
                detector=detector,
                frame_reader=frame_reader,
                sample_fps=sample_fps,
                max_step_px=max_step_px,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            reason = (
                "no_faces"
                if isinstance(exc, NoFacesDetectedError)
                else f"detection_failed:{type(exc).__name__}"
            )
            logger.warning(
                "reframe_face_tracking_fallback",
                extra={"reason": reason, "aspect": aspect},
            )
            plan = replace(
                center_crop_plan(src_w, src_h, aspect, start=start),
                fallback_reason=reason,
            )

    logger.info(
        "reframe_planned",
        extra={
            "requested_mode": mode,
            "mode": plan.mode,
            "aspect": aspect,
            "source_width": src_w,
            "source_height": src_h,
            "crop_width": plan.crop_width,
            "crop_height": plan.crop_height,
            "keyframes": len(plan.keyframes),
            "fallback_reason": plan.fallback_reason,
            "detector": plan.detector,
        },
    )
    return plan
