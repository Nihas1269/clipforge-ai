"""Tests for face tracking and plan_reframe (app/reframe/face_tracking.py, service.py).

Detection and frame reading are injected, so most tests use synthetic numpy
frames and a fake detector and need neither OpenCV nor a video. The tests
that exercise the real OpenCV code are skipped when OpenCV is not installed.
"""

import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.media.ffmpeg import MediaError
from app.reframe import face_tracking, service
from app.reframe.face_tracking import (
    FaceTrackingError,
    detector_name,
    fill_missing_centers,
    sample_times,
    track_faces,
)
from app.reframe.planner import Keyframe
from app.reframe.service import ReframeError, plan_reframe

SOURCE = (1920, 1080)
DURATION = 60.0
# Frames are a tenth of the source size, which also checks the scaling of
# detections from frame pixels to source pixels.
FRAME_SHAPE = (108, 192, 3)


class FakeDetector:
    """Returns the queued boxes for each call and records the frames seen."""

    def __init__(self, results):
        self.results = list(results)
        self.frames = []

    def detect(self, frame):
        self.frames.append(frame)
        return self.results.pop(0) if self.results else []


class FailingDetector:
    def detect(self, frame):
        raise RuntimeError("detector crashed")


def synthetic_reader(video_path, times):
    for time_seconds in times:
        yield time_seconds, np.zeros(FRAME_SHAPE, dtype=np.uint8)


def fail_if_called(*args, **kwargs):
    raise AssertionError("must not be called")


def test_sample_times_default_two_fps():
    assert sample_times(10.0, 12.0) == [10.0, 10.5, 11.0, 11.5]
    assert sample_times(0.0, 0.2) == [0.0]
    assert sample_times(0.0, 1.0, sample_fps=4) == [0.0, 0.25, 0.5, 0.75]


def test_sample_times_are_capped_for_long_clips():
    times = sample_times(0.0, 7200.0)
    assert len(times) == face_tracking.MAX_SAMPLES
    assert times[-1] < 7200.0


@pytest.mark.parametrize(
    ("start", "end", "fps"),
    [
        (5.0, 5.0, 2),
        (5.0, 4.0, 2),
        (-1.0, 2.0, 2),
        (0.0, float("inf"), 2),
        (0, 1, 0),
        (0, 1, 31),
    ],
)
def test_sample_times_rejects_invalid_input(start, end, fps):
    with pytest.raises(ValueError):
        sample_times(start, end, fps)


def test_fill_missing_centers_center_interpolation_and_carry_forward():
    times = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    detections = [None, (100.0, 50.0), None, (300.0, 150.0), None, None]
    assert fill_missing_centers(times, detections, (960.0, 540.0)) == [
        (0.0, 960.0, 540.0),  # no face seen yet: frame center
        (0.5, 100.0, 50.0),
        (1.0, 200.0, 100.0),  # interpolated between the two faces
        (1.5, 300.0, 150.0),
        (2.0, 300.0, 150.0),  # carried forward
        (2.5, 300.0, 150.0),
    ]


def test_face_mode_follows_largest_face_through_injected_detector():
    small = (10, 10, 20, 20)
    large = (140, 34, 20, 40)  # center (150, 54) -> source (1500, 540)
    detector = FakeDetector([[], [small, large], [], []])

    plan = plan_reframe(
        "clip.mp4",
        0.0,
        2.0,
        "9:16",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=detector,
        frame_reader=synthetic_reader,
        max_step_px=10_000,
    )

    assert len(detector.frames) == 4  # one call per sample at 2 fps
    assert all(frame.shape == FRAME_SHAPE for frame in detector.frames)
    assert plan.mode == "face"
    assert plan.fallback_reason is None
    assert plan.detector == "FakeDetector"
    assert (plan.crop_width, plan.crop_height) == (608, 1080)
    assert plan.keyframes == (
        Keyframe(0.0, 656, 0),  # center until the first face
        Keyframe(0.5, 1196, 0),  # 1500 - 608 / 2
        Keyframe(1.0, 1196, 0),  # carried forward
        Keyframe(1.5, 1196, 0),
    )


def test_face_plan_records_detector_name(monkeypatch):
    events = {}

    def record(event, extra):
        events[event] = extra

    for module in (face_tracking, service):
        monkeypatch.setattr(module.logger, "info", record)
    detector = FakeDetector([[(140, 34, 20, 40)]])
    detector.name = "mediapipe:short-range(min_confidence=0.5)"
    plan = plan_reframe(
        "clip.mp4",
        0.0,
        0.5,
        "9:16",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=detector,
        frame_reader=synthetic_reader,
    )
    assert plan.detector == "mediapipe:short-range(min_confidence=0.5)"
    assert events["face_tracking_sampled"]["detector"] == plan.detector
    assert events["reframe_planned"]["detector"] == plan.detector


def test_detector_name_is_sanitized_or_falls_back_to_class_name():
    assert detector_name(SimpleNamespace(name="a\nb<c>" + "x" * 300)) == (
        "a_b_c_" + "x" * 194
    )
    assert detector_name(SimpleNamespace(name="  ")) == "SimpleNamespace"
    assert detector_name(SimpleNamespace(name=42)) == "SimpleNamespace"
    assert detector_name(FakeDetector([])) == "FakeDetector"


def test_face_mode_smooths_and_clamps_keyframes():
    # A face at the far right edge: the crop is clamped to the frame and the
    # move from the center is spread over several samples.
    detector = FakeDetector([[(180, 40, 12, 20)]] * 8)
    plan = plan_reframe(
        "clip.mp4",
        0.0,
        4.0,
        "9:16",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=detector,
        frame_reader=synthetic_reader,
        max_step_px=200,
    )
    xs = [keyframe.x for keyframe in plan.keyframes]
    assert xs == [1312] * 8  # the first sample has the face, clamped right

    detector = FakeDetector([[]] + [[(180, 40, 12, 20)]] * 7)
    plan = plan_reframe(
        "clip.mp4",
        0.0,
        4.0,
        "9:16",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=detector,
        frame_reader=synthetic_reader,
        max_step_px=200,
    )
    xs = [keyframe.x for keyframe in plan.keyframes]
    assert xs == [656, 856, 1056, 1256, 1312, 1312, 1312, 1312]
    for keyframe in plan.keyframes:
        assert 0 <= keyframe.x <= 1920 - 608 and keyframe.y == 0


def test_face_mode_portrait_source_tracks_vertically():
    # 1080x1920 source -> 16:9 crop of 1080x608; frames are a tenth the size.
    def portrait_reader(video_path, times):
        for time_seconds in times:
            yield time_seconds, np.zeros((192, 108, 3), dtype=np.uint8)

    detector = FakeDetector([[(40, 20, 20, 20)]])  # center (50, 30) -> (500, 300)
    plan = plan_reframe(
        "clip.mp4",
        0.0,
        0.5,
        "16:9",
        mode="face",
        source_size=(1080, 1920),
        media_duration=DURATION,
        detector=detector,
        frame_reader=portrait_reader,
    )
    assert (plan.crop_width, plan.crop_height) == (1080, 608)
    assert plan.keyframes == (Keyframe(0.0, 0, 0),)  # 300 - 304 clamped to 0


def test_rotated_frames_fall_back_to_center():
    # Portrait frames for a source probed as landscape (rotation metadata).
    def rotated_reader(video_path, times):
        for time_seconds in times:
            yield time_seconds, np.zeros((192, 108, 3), dtype=np.uint8)

    plan = plan_reframe(
        "clip.mp4",
        0.0,
        1.0,
        "9:16",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=FakeDetector([[(40, 20, 20, 20)]] * 2),
        frame_reader=rotated_reader,
    )
    assert plan.mode == "center"
    assert plan.fallback_reason == "detection_failed:FrameOrientationError"
    assert plan.detector is None


def test_face_tracking_time_limit_falls_back_to_center():
    def slow_reader(video_path, times):
        for time_seconds in times:
            time.sleep(0.05)
            yield time_seconds, np.zeros(FRAME_SHAPE, dtype=np.uint8)

    detector = FakeDetector([])
    plan = plan_reframe(
        "clip.mp4",
        0.0,
        2.0,
        "9:16",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=detector,
        frame_reader=slow_reader,
        timeout_seconds=0.01,
    )
    assert detector.frames == []
    assert plan.mode == "center"
    assert plan.fallback_reason == "detection_failed:FaceTrackingTimeoutError"


def test_no_faces_falls_back_to_center():
    detector = FakeDetector([])
    plan = plan_reframe(
        "clip.mp4",
        3.0,
        5.0,
        "1:1",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=detector,
        frame_reader=synthetic_reader,
    )
    assert len(detector.frames) == 4
    assert plan.mode == "center"
    assert plan.fallback_reason == "no_faces"
    assert plan.detector is None
    assert plan.keyframes == (Keyframe(3.0, 420, 0),)


def test_invalid_detections_are_ignored():
    detector = FakeDetector([[(0, 0, 0, 10), (5, 5, -3, 4), (float("nan"), 0, 5, 5)]])
    plan = plan_reframe(
        "clip.mp4",
        0.0,
        0.5,
        "9:16",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=detector,
        frame_reader=synthetic_reader,
    )
    assert plan.fallback_reason == "no_faces"


def test_detector_error_falls_back_to_center():
    plan = plan_reframe(
        "clip.mp4",
        0.0,
        2.0,
        "9:16",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=FailingDetector(),
        frame_reader=synthetic_reader,
    )
    assert plan.mode == "center"
    assert plan.fallback_reason == "detection_failed:RuntimeError"
    assert plan.keyframes == (Keyframe(0.0, 656, 0),)


def test_unreadable_frames_fall_back_to_center():
    def unreadable_reader(video_path, times):
        for time_seconds in times:
            yield time_seconds, None

    detector = FakeDetector([])
    plan = plan_reframe(
        "clip.mp4",
        0.0,
        2.0,
        "9:16",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=detector,
        frame_reader=unreadable_reader,
    )
    assert detector.frames == []
    assert plan.fallback_reason == "detection_failed:FaceTrackingError"


def test_reader_error_falls_back_to_center():
    def broken_reader(video_path, times):
        raise FaceTrackingError("cannot open")

    plan = plan_reframe(
        "clip.mp4",
        0.0,
        2.0,
        "9:16",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        detector=FakeDetector([]),
        frame_reader=broken_reader,
    )
    assert plan.mode == "center"
    assert plan.fallback_reason == "detection_failed:FaceTrackingError"


def test_missing_opencv_falls_back_to_center(monkeypatch):
    # A None entry in sys.modules makes `import cv2` raise ImportError.
    monkeypatch.setitem(sys.modules, "cv2", None)
    plan = plan_reframe(
        "clip.mp4",
        0.0,
        2.0,
        "4:5",
        mode="face",
        source_size=SOURCE,
        media_duration=DURATION,
        frame_reader=synthetic_reader,
    )
    assert plan.mode == "center"
    assert plan.fallback_reason == "detection_failed:FaceDetectionUnavailableError"
    assert plan.keyframes == (Keyframe(0.0, 528, 0),)


def test_center_mode_never_runs_detection():
    plan = plan_reframe(
        "clip.mp4",
        1.0,
        9.0,
        "9:16",
        source_size=(1280, 720),
        media_duration=DURATION,
        detector=SimpleNamespace(detect=fail_if_called),
        frame_reader=fail_if_called,
    )
    assert plan.mode == "center"
    assert plan.fallback_reason is None
    assert plan.detector is None
    assert (plan.crop_width, plan.crop_height) == (406, 720)
    assert plan.keyframes == (Keyframe(1.0, 437, 0),)


@pytest.mark.parametrize(
    ("start", "end", "aspect", "mode"),
    [
        (2.0, 1.0, "9:16", "center"),
        (-1.0, 1.0, "9:16", "center"),
        (0.0, float("nan"), "9:16", "face"),
        (False, 1.0, "9:16", "center"),
        (0.0, True, "9:16", "face"),
        (0.0, 1.0, "2:1", "face"),
        (0.0, 1.0, "9:16", "auto"),
        # Past the end of the media.
        (0.0, DURATION + 0.5, "9:16", "center"),
        (DURATION + 1.0, DURATION + 2.0, "9:16", "center"),
        (DURATION - 1.0, DURATION + 0.01, "9:16", "face"),
    ],
)
def test_plan_reframe_rejects_invalid_arguments(start, end, aspect, mode):
    with pytest.raises(ValueError):
        plan_reframe(
            "clip.mp4",
            start,
            end,
            aspect,
            mode=mode,
            source_size=SOURCE,
            media_duration=DURATION,
            detector=SimpleNamespace(detect=fail_if_called),
            frame_reader=fail_if_called,
        )


def test_clip_may_end_exactly_at_media_end():
    plan = plan_reframe(
        "clip.mp4",
        DURATION - 2.0,
        DURATION,
        "9:16",
        source_size=SOURCE,
        media_duration=DURATION,
    )
    assert plan.keyframes == (Keyframe(DURATION - 2.0, 656, 0),)


@pytest.mark.parametrize(
    "options",
    [
        {"source_size": SOURCE},
        {"media_duration": DURATION},
        {"source_size": SOURCE, "media_duration": 0.0},
        {"source_size": SOURCE, "media_duration": float("inf")},
        {"source_size": SOURCE, "media_duration": True},
    ],
)
def test_plan_reframe_requires_valid_size_and_duration_together(options):
    with pytest.raises(ValueError):
        plan_reframe("clip.mp4", 0.0, 1.0, "9:16", **options)


@pytest.mark.parametrize(
    "options",
    [
        {"sample_fps": 0},
        {"sample_fps": 100},
        {"max_step_px": 0},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
    ],
)
def test_face_mode_rejects_invalid_options_instead_of_falling_back(options):
    with pytest.raises(ValueError):
        plan_reframe(
            "clip.mp4",
            0.0,
            1.0,
            "9:16",
            mode="face",
            source_size=SOURCE,
            media_duration=DURATION,
            detector=SimpleNamespace(detect=fail_if_called),
            frame_reader=fail_if_called,
            **options,
        )


def probe_result(width=1280, height=720, duration_seconds=10.0):
    return SimpleNamespace(
        width=width, height=height, duration_seconds=duration_seconds
    )


def test_plan_reframe_probes_size_when_not_given(monkeypatch):
    monkeypatch.setattr(service, "probe", lambda path: probe_result())
    plan = plan_reframe("clip.mp4", 0.0, 1.0, "1:1")
    assert (plan.source_width, plan.source_height) == (1280, 720)
    assert plan.keyframes == (Keyframe(0.0, 280, 0),)


def test_plan_reframe_checks_probed_duration(monkeypatch):
    monkeypatch.setattr(
        service, "probe", lambda path: probe_result(duration_seconds=0.8)
    )
    with pytest.raises(ValueError):
        plan_reframe("clip.mp4", 0.0, 1.0, "1:1")


@pytest.mark.parametrize(
    "info", [probe_result(width=None), probe_result(duration_seconds=None)]
)
def test_plan_reframe_without_dimensions_or_duration_raises(monkeypatch, info):
    monkeypatch.setattr(service, "probe", lambda path: info)
    with pytest.raises(ReframeError):
        plan_reframe("clip.mp4", 0.0, 1.0, "1:1")


def test_plan_reframe_probe_failure_raises_reframe_error(monkeypatch):
    def failing_probe(path):
        raise MediaError("ffprobe could not read the file.")

    monkeypatch.setattr(service, "probe", failing_probe)
    with pytest.raises(ReframeError):
        plan_reframe("clip.mp4", 0.0, 1.0, "1:1")


def test_track_faces_raises_without_faces():
    with pytest.raises(face_tracking.NoFacesDetectedError):
        track_faces(
            "clip.mp4",
            0.0,
            1.0,
            "9:16",
            SOURCE,
            detector=FakeDetector([]),
            frame_reader=synthetic_reader,
        )


@pytest.mark.parametrize("previous", [None, "rw_timeout;5000000"])
def test_opencv_reader_forces_ffmpeg_and_restores_environment(monkeypatch, previous):
    env = face_tracking.OPENCV_CAPTURE_OPTIONS_ENV
    if previous is None:
        monkeypatch.delenv(env, raising=False)
    else:
        monkeypatch.setenv(env, previous)
    opened = []

    class FakeCapture:
        def __init__(self, path, api):
            opened.append((api, os.environ.get(env)))

        def isOpened(self):
            return False

        def release(self):
            pass

    fake_cv2 = SimpleNamespace(VideoCapture=FakeCapture, CAP_FFMPEG=1900)
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)

    with pytest.raises(FaceTrackingError):
        list(face_tracking.read_frames_opencv("clip.mp4", [0.0]))
    assert opened == [(1900, face_tracking.OPENCV_CAPTURE_OPTIONS)]
    assert os.environ.get(env) == previous


# --- Real OpenCV (skipped when opencv-python-headless is not installed) ---


@pytest.fixture
def cv2_module():
    return pytest.importorskip("cv2")


@pytest.fixture
def blank_video(tmp_path, cv2_module):
    """A 2-second, 10 fps, 160x120 MJPEG AVI with no faces."""
    path = tmp_path / "blank.avi"
    writer = cv2_module.VideoWriter(
        str(path), cv2_module.VideoWriter_fourcc(*"MJPG"), 10.0, (160, 120)
    )
    if not writer.isOpened():
        pytest.skip("OpenCV cannot write MJPEG video here")
    try:
        for index in range(20):
            writer.write(np.full((120, 160, 3), index * 10, dtype=np.uint8))
    finally:
        writer.release()
    return path


def test_haar_detector_finds_no_face_in_blank_frame(cv2_module):
    detector = face_tracking.HaarFaceDetector()
    assert detector.detect(np.zeros((120, 160, 3), dtype=np.uint8)) == []
    assert detector.detect(np.zeros((120, 160), dtype=np.uint8)) == []


def test_haar_detector_name_records_model_and_parameters(cv2_module):
    detector = face_tracking.HaarFaceDetector(min_neighbors=4)
    assert detector.name.startswith("opencv-haar:haarcascade_frontalface_default(")
    assert f"opencv={cv2_module.__version__}" in detector.name
    assert "min_neighbors=4" in detector.name
    assert detector_name(detector) == detector.name


def test_opencv_reader_reads_sampled_frames(blank_video):
    frames = list(face_tracking.read_frames_opencv(blank_video, [0.0, 0.5, 1.0]))
    assert [time_seconds for time_seconds, _ in frames] == [0.0, 0.5, 1.0]
    assert all(
        frame is not None and frame.shape == (120, 160, 3) for _, frame in frames
    )


def test_face_mode_on_real_video_without_faces_falls_back(blank_video):
    plan = plan_reframe(
        blank_video,
        0.0,
        2.0,
        "9:16",
        mode="face",
        source_size=(160, 120),
        media_duration=2.0,
    )
    assert plan.mode == "center"
    assert plan.fallback_reason == "no_faces"
    assert (plan.crop_width, plan.crop_height) == (68, 120)
    assert plan.keyframes == (Keyframe(0.0, 46, 0),)


def test_unopenable_video_falls_back_to_center(tmp_path, cv2_module):
    plan = plan_reframe(
        tmp_path / "missing.mp4",
        0.0,
        2.0,
        "9:16",
        mode="face",
        source_size=(160, 120),
        media_duration=2.0,
    )
    assert plan.mode == "center"
    assert plan.fallback_reason == "detection_failed:FaceTrackingError"
