"""Face tracking for face-mode reframing (optional, OpenCV based).

Frames are sampled at `sample_fps` (default 2) between the clip's start and
end, faces are detected in each sample, and the largest face's center becomes
the crop center for that sample. Samples without a face are filled in:

- before the first detected face: the frame center;
- between two detected faces: linearly interpolated between them;
- after the last detected face: the last face position, carried forward.

The resulting keyframes are smoothed with `smooth_keyframes`.

Detection and frame reading are injectable (`FaceDetector`, `FrameReader`) so
they can be replaced, for example with a MediaPipe detector or with fakes in
tests. The default implementations use `opencv-python-headless` and the Haar
cascade bundled with it (no download). OpenCV is imported only when they are
used, so the rest of the backend does not need it.
"""

import logging
import math
import os
import re
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from app.reframe.planner import CropPlan, crop_plan_from_centers

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_FPS = 2.0
MAX_SAMPLE_FPS = 30.0
# Longer clips are sampled less often so that no more than this many frames
# are decoded and analysed.
MAX_SAMPLES = 3600
# Default smoothing: the crop moves at most this fraction of the larger
# source dimension from one sample to the next.
DEFAULT_MAX_STEP_FRACTION = 0.05
# Face tracking gives up (and the caller falls back to center) once reading
# and analysing frames has taken this long.
DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_TIMEOUT_SECONDS = 3600.0

HAAR_CASCADE_FILE = "haarcascade_frontalface_default.xml"
# Only local files may be opened by OpenCV's FFmpeg backend, so a crafted
# media file (for example an HLS playlist) cannot make it open URLs.
OPENCV_CAPTURE_OPTIONS_ENV = "OPENCV_FFMPEG_CAPTURE_OPTIONS"
OPENCV_CAPTURE_OPTIONS = "protocol_whitelist;file"

MAX_DETECTOR_NAME_CHARS = 200
_UNSAFE_NAME_CHARACTERS = re.compile(r"[^A-Za-z0-9 .,:;=_()/+-]")


class FaceTrackingError(Exception):
    """Face tracking could not produce a plan (the caller falls back to center)."""


class FaceDetectionUnavailableError(FaceTrackingError):
    """OpenCV or its bundled Haar cascade is not available."""


class NoFacesDetectedError(FaceTrackingError):
    """No sample contained a face."""


class FrameOrientationError(FaceTrackingError):
    """Decoded frames are rotated relative to the probed size (e.g. rotation metadata)."""


class FaceTrackingTimeoutError(FaceTrackingError):
    """Face tracking took longer than its time limit."""


class FaceBox(NamedTuple):
    """A detected face: top-left corner and size in frame pixels."""

    x: float
    y: float
    width: float
    height: float


class FaceDetector(Protocol):
    """Finds faces in one frame (a numpy array, BGR or grayscale).

    A detector may also have a `name` string identifying the model and its
    parameters; it is recorded on face plans (see `detector_name`).
    """

    def detect(self, frame: Any) -> Sequence[tuple[float, float, float, float]]:
        """Return `(x, y, width, height)` boxes of the faces in `frame`."""
        ...


class FrameReader(Protocol):
    """Reads the frames closest to the given source times."""

    def __call__(
        self, video_path: Path, times: Sequence[float]
    ) -> Iterator[tuple[float, Any]]:
        """Yield `(time, frame)` per requested time; `frame` is None if unreadable."""
        ...


def _import_cv2() -> Any:
    try:
        import cv2
    except ImportError:
        raise FaceDetectionUnavailableError(
            "OpenCV (opencv-python-headless) is not installed."
        ) from None
    return cv2


class HaarFaceDetector:
    """Frontal face detector using OpenCV's bundled Haar cascade."""

    def __init__(
        self,
        scale_factor: float = 1.1,
        min_neighbors: int = 5,
        min_size_fraction: float = 0.05,
    ) -> None:
        cv2 = _import_cv2()
        data = getattr(cv2, "data", None)
        cascade_dir = getattr(data, "haarcascades", None)
        if not cascade_dir:
            raise FaceDetectionUnavailableError("OpenCV has no bundled Haar cascades.")
        cascade_path = Path(cascade_dir) / HAAR_CASCADE_FILE
        if not cascade_path.is_file():
            raise FaceDetectionUnavailableError("The bundled Haar cascade is missing.")
        classifier = cv2.CascadeClassifier(str(cascade_path))
        if classifier.empty():
            raise FaceDetectionUnavailableError(
                "The bundled Haar cascade could not be loaded."
            )
        self._cv2 = cv2
        self._classifier = classifier
        self._scale_factor = scale_factor
        self._min_neighbors = min_neighbors
        self._min_size_fraction = min_size_fraction
        version = getattr(cv2, "__version__", None)
        self.name = (
            f"opencv-haar:{Path(HAAR_CASCADE_FILE).stem}"
            f"(opencv={version if isinstance(version, str) else 'unknown'},"
            f"scale_factor={scale_factor:g},min_neighbors={min_neighbors},"
            f"min_size_fraction={min_size_fraction:g})"
        )

    def detect(self, frame: Any) -> list[FaceBox]:
        cv2 = self._cv2
        if frame.ndim == 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        else:
            gray = frame
        gray = cv2.equalizeHist(gray)
        min_side = max(24, int(min(gray.shape[:2]) * self._min_size_fraction))
        faces = self._classifier.detectMultiScale(
            gray,
            scaleFactor=self._scale_factor,
            minNeighbors=self._min_neighbors,
            minSize=(min_side, min_side),
        )
        return [FaceBox(*(float(value) for value in face)) for face in faces]


def read_frames_opencv(
    video_path: Path, times: Sequence[float]
) -> Iterator[tuple[float, Any]]:
    """Yield `(time, frame)` for each time, seeking with OpenCV (None if unreadable).

    The FFmpeg backend is forced (other backends ignore the capture options)
    and the protocol whitelist is set only while the capture is opened, then
    the previous value of the environment variable is restored.
    """
    cv2 = _import_cv2()
    previous = os.environ.get(OPENCV_CAPTURE_OPTIONS_ENV)
    os.environ[OPENCV_CAPTURE_OPTIONS_ENV] = OPENCV_CAPTURE_OPTIONS
    try:
        capture = cv2.VideoCapture(str(video_path), cv2.CAP_FFMPEG)
    finally:
        if previous is None:
            os.environ.pop(OPENCV_CAPTURE_OPTIONS_ENV, None)
        else:
            os.environ[OPENCV_CAPTURE_OPTIONS_ENV] = previous
    try:
        if not capture.isOpened():
            raise FaceTrackingError("The video could not be opened for face tracking.")
        for time_seconds in times:
            capture.set(cv2.CAP_PROP_POS_MSEC, time_seconds * 1000.0)
            ok, frame = capture.read()
            yield time_seconds, frame if ok else None
    finally:
        capture.release()


def sample_times(
    start: float, end: float, sample_fps: float = DEFAULT_SAMPLE_FPS
) -> list[float]:
    """Return the sample times from `start` (inclusive) to `end` (exclusive)."""
    if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end <= start:
        raise ValueError("start and end must be finite with 0 <= start < end.")
    if (
        isinstance(sample_fps, bool)
        or not isinstance(sample_fps, (int, float))
        or not math.isfinite(sample_fps)
        or not 0 < sample_fps <= MAX_SAMPLE_FPS
    ):
        raise ValueError(f"sample_fps must be above 0 and at most {MAX_SAMPLE_FPS:g}.")
    duration = end - start
    interval = max(1.0 / sample_fps, duration / MAX_SAMPLES)
    count = max(1, math.ceil(duration / interval - 1e-9))
    return [start + index * interval for index in range(count)]


def check_timeout(timeout_seconds: float) -> float:
    """Return `timeout_seconds` if it is a valid face-tracking time limit."""
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"timeout_seconds must be above 0 and at most {MAX_TIMEOUT_SECONDS:g}."
        )
    return float(timeout_seconds)


def detector_name(detector: object) -> str:
    """Return the detector's `name` (made log-safe), or its class name."""
    name = getattr(detector, "name", None)
    if isinstance(name, str):
        name = _UNSAFE_NAME_CHARACTERS.sub("_", name.strip())[:MAX_DETECTOR_NAME_CHARS]
        if name:
            return name
    return type(detector).__name__


def _largest_face(
    boxes: Sequence[tuple[float, float, float, float]],
) -> tuple[float, float] | None:
    """Return the center of the largest valid box, or None."""
    best: tuple[float, float] | None = None
    best_area = 0.0
    for box in boxes:
        x, y, width, height = (float(value) for value in box)
        if not all(math.isfinite(v) for v in (x, y, width, height)):
            continue
        if width <= 0 or height <= 0:
            continue
        area = width * height
        if area > best_area:
            best_area = area
            best = (x + width / 2, y + height / 2)
    return best


def fill_missing_centers(
    times: Sequence[float],
    detections: Sequence[tuple[float, float] | None],
    default_center: tuple[float, float],
) -> list[tuple[float, float, float]]:
    """Turn per-sample detections (None = no face) into `(t, cx, cy)` centers."""
    if len(times) != len(detections):
        raise ValueError("There must be one detection entry per sample time.")
    # The next sample with a face (its time and center), for each sample.
    next_known: list[tuple[float, tuple[float, float]] | None] = [None] * len(
        detections
    )
    upcoming: tuple[float, tuple[float, float]] | None = None
    for index in range(len(detections) - 1, -1, -1):
        next_known[index] = upcoming
        detection = detections[index]
        if detection is not None:
            upcoming = (times[index], detection)

    centers: list[tuple[float, float, float]] = []
    last_known: tuple[float, tuple[float, float]] | None = None
    for index, time_seconds in enumerate(times):
        detection = detections[index]
        center: tuple[float, float]
        if detection is not None:
            center = detection
            last_known = (time_seconds, detection)
        else:
            following = next_known[index]
            if last_known is None:
                center = default_center
            elif following is None:
                center = last_known[1]
            else:
                (left_time, left_center), (right_time, right_center) = (
                    last_known,
                    following,
                )
                span = right_time - left_time
                weight = (time_seconds - left_time) / span if span > 0 else 0.0
                center = (
                    left_center[0] + (right_center[0] - left_center[0]) * weight,
                    left_center[1] + (right_center[1] - left_center[1]) * weight,
                )
        centers.append((time_seconds, center[0], center[1]))
    return centers


def track_faces(
    video_path: Path,
    start: float,
    end: float,
    aspect: str,
    source_size: tuple[int, int],
    *,
    detector: FaceDetector | None = None,
    frame_reader: FrameReader | None = None,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
    max_step_px: float | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> CropPlan:
    """Return a face-following crop plan for `start`-`end` of the video.

    Raises FaceTrackingError (or a subclass) when OpenCV is unavailable, the
    video cannot be read, its frames are rotated relative to `source_size`,
    reading and analysing frames takes longer than `timeout_seconds`, or no
    face is found in any sample. The time limit is checked between frames,
    so a single frame that is very slow to decode can overrun it.
    """
    src_w, src_h = source_size
    times = sample_times(start, end, sample_fps)
    deadline = time.monotonic() + check_timeout(timeout_seconds)
    if detector is None:
        detector = HaarFaceDetector()
    if frame_reader is None:
        frame_reader = read_frames_opencv
    if max_step_px is None:
        max_step_px = max(2.0, DEFAULT_MAX_STEP_FRACTION * max(src_w, src_h))
    name = detector_name(detector)

    # Frames are analysed as they are read and not kept, so memory use does
    # not grow with the clip length.
    found: dict[float, tuple[float, float]] = {}
    frames_read = 0
    for time_seconds, frame in frame_reader(video_path, times):
        if time.monotonic() > deadline:
            raise FaceTrackingTimeoutError("Face tracking took too long.")
        if frame is None:
            continue
        frames_read += 1
        frame_h, frame_w = frame.shape[:2]
        # Probed sizes are before rotation metadata is applied, but decoders
        # may rotate frames; mapping across swapped axes would be wrong.
        if (frame_w - frame_h) * (src_w - src_h) < 0:
            raise FrameOrientationError(
                "The decoded frames are rotated relative to the video's size."
            )
        center = _largest_face(detector.detect(frame))
        if center is None:
            continue
        # Map frame pixels to source pixels in case the decoder's frame size
        # differs from the probed size.
        found[time_seconds] = (center[0] * src_w / frame_w, center[1] * src_h / frame_h)

    detections = [found.get(time_seconds) for time_seconds in times]
    faces_found = sum(1 for center in detections if center is not None)
    logger.info(
        "face_tracking_sampled",
        extra={
            "detector": name,
            "samples": len(times),
            "frames_read": frames_read,
            "faces_found": faces_found,
        },
    )
    if frames_read == 0:
        raise FaceTrackingError("No frame of the clip could be read.")
    if faces_found == 0:
        raise NoFacesDetectedError("No face was detected in the clip.")

    centers = fill_missing_centers(times, detections, (src_w / 2, src_h / 2))
    return crop_plan_from_centers(
        src_w, src_h, aspect, centers, max_step_px, detector=name
    )
