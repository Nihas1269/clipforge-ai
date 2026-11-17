"""Tests for the pure crop planner (app/reframe/planner.py)."""

import math
import random

import pytest

from app.reframe.planner import (
    ASPECT_RATIOS,
    CropPlan,
    Keyframe,
    center_crop_plan,
    clamp_position,
    compute_crop_size,
    crop_plan_from_centers,
    smooth_keyframes,
)


@pytest.mark.parametrize(
    ("src_w", "src_h", "aspect", "expected"),
    [
        (1920, 1080, "9:16", (608, 1080)),
        (1920, 1080, "1:1", (1080, 1080)),
        (1920, 1080, "4:5", (864, 1080)),
        (1920, 1080, "16:9", (1920, 1080)),
        (1280, 720, "9:16", (406, 720)),
        (1280, 720, "1:1", (720, 720)),
        (1280, 720, "4:5", (576, 720)),
        (1280, 720, "16:9", (1280, 720)),
        # Portrait sources.
        (1080, 1920, "9:16", (1080, 1920)),
        (1080, 1920, "1:1", (1080, 1080)),
        (1080, 1920, "4:5", (1080, 1350)),
        (1080, 1920, "16:9", (1080, 608)),
    ],
)
def test_compute_crop_size(src_w, src_h, aspect, expected):
    assert compute_crop_size(src_w, src_h, aspect) == expected


@pytest.mark.parametrize("aspect", sorted(ASPECT_RATIOS))
@pytest.mark.parametrize(
    ("src_w", "src_h"),
    [
        (1920, 1080),
        (1280, 720),
        (1080, 1920),
        (720, 1280),
        (1921, 1081),
        (17, 16),
        (640, 480),
    ],
)
def test_crop_size_fits_is_even_and_keeps_aspect(src_w, src_h, aspect):
    crop_w, crop_h = compute_crop_size(src_w, src_h, aspect)
    ratio_w, ratio_h = ASPECT_RATIOS[aspect]
    assert 0 < crop_w <= src_w and 0 < crop_h <= src_h
    assert crop_w % 2 == 0 and crop_h % 2 == 0
    # Within 2 pixels of the exact ratio on the rounded side.
    assert abs(crop_w * ratio_h - crop_h * ratio_w) <= 2 * max(ratio_w, ratio_h)
    # One side uses the full (even) source dimension.
    assert crop_w == src_w - src_w % 2 or crop_h == src_h - src_h % 2


@pytest.mark.parametrize("aspect", ["3:2", "", "9/16", None, 9])
def test_compute_crop_size_rejects_unsupported_aspect(aspect):
    with pytest.raises(ValueError):
        compute_crop_size(1920, 1080, aspect)


@pytest.mark.parametrize(
    ("src_w", "src_h"),
    [(0, 1080), (-1920, 1080), (1920, 15), (1920.0, 1080), (True, 1080), (70000, 1080)],
)
def test_compute_crop_size_rejects_invalid_dimensions(src_w, src_h):
    with pytest.raises(ValueError):
        compute_crop_size(src_w, src_h, "9:16")


def test_center_mode_returns_single_centered_keyframe():
    plan = center_crop_plan(1920, 1080, "9:16", start=12.5)
    assert (plan.crop_width, plan.crop_height) == (608, 1080)
    assert plan.keyframes == (Keyframe(12.5, 656, 0),)
    assert plan.mode == "center"
    assert plan.fallback_reason is None
    assert plan.detector is None


def test_crop_plan_from_centers_records_detector():
    plan = crop_plan_from_centers(
        1920, 1080, "9:16", [(0.0, 960.0, 540.0)], 10, detector="haar"
    )
    assert plan.detector == "haar"


def test_center_crop_plan_portrait_to_landscape():
    plan = center_crop_plan(1080, 1920, "16:9")
    assert plan.keyframes == (Keyframe(0.0, 0, (1920 - 608) // 2),)


def test_center_crop_full_frame_is_at_origin():
    plan = center_crop_plan(1920, 1080, "16:9")
    assert plan.keyframes == (Keyframe(0.0, 0, 0),)


def test_clamp_position_keeps_crop_inside_frame():
    assert clamp_position(-50, -10, 608, 1080, 1920, 1080) == (0, 0)
    assert clamp_position(5000, 5000, 608, 1080, 1920, 1080) == (1312, 0)
    assert clamp_position(100.4, 0, 608, 1080, 1920, 1080) == (100, 0)
    with pytest.raises(ValueError):
        clamp_position(math.nan, 0, 608, 1080, 1920, 1080)


def test_crop_plan_rejects_out_of_frame_keyframes():
    with pytest.raises(ValueError):
        CropPlan(1920, 1080, "9:16", 608, 1080, (Keyframe(0.0, 1313, 0),))
    with pytest.raises(ValueError):
        CropPlan(1920, 1080, "9:16", 608, 1080, (Keyframe(0.0, -1, 0),))
    with pytest.raises(ValueError):
        CropPlan(1920, 1080, "9:16", 608, 1080, ())
    with pytest.raises(ValueError):
        CropPlan(
            1920, 1080, "9:16", 608, 1080, (Keyframe(1.0, 0, 0), Keyframe(0.5, 0, 0))
        )


@pytest.mark.parametrize("aspect", sorted(ASPECT_RATIOS))
@pytest.mark.parametrize(("src_w", "src_h"), [(1920, 1080), (1280, 720), (1080, 1920)])
def test_keyframes_always_inside_frame(src_w, src_h, aspect):
    rng = random.Random(f"{src_w}x{src_h}-{aspect}")
    # Centers well outside the frame as well as inside it.
    centers = [
        (
            index * 0.5,
            rng.uniform(-2 * src_w, 3 * src_w),
            rng.uniform(-2 * src_h, 3 * src_h),
        )
        for index in range(200)
    ]
    for max_step in (1, 37.5, 10_000):
        plan = crop_plan_from_centers(src_w, src_h, aspect, centers, max_step)
        assert plan.mode == "face"
        assert len(plan.keyframes) == len(centers)
        for keyframe in plan.keyframes:
            assert 0 <= keyframe.x <= src_w - plan.crop_width
            assert 0 <= keyframe.y <= src_h - plan.crop_height


def test_smoothing_limits_per_step_movement():
    keyframes = [Keyframe(0.0, 0, 0), Keyframe(0.5, 1000, 0), Keyframe(1.0, 1000, 0)]
    assert smooth_keyframes(keyframes, 100) == [
        Keyframe(0.0, 0, 0),
        Keyframe(0.5, 100, 0),
        Keyframe(1.0, 200, 0),
    ]


def test_smoothing_limits_diagonal_and_random_steps():
    rng = random.Random(38)
    keyframes = [
        Keyframe(index * 0.5, rng.randint(0, 1312), rng.randint(0, 800))
        for index in range(300)
    ]
    for max_step in (1, 7.5, 50, 333):
        smoothed = smooth_keyframes(keyframes, max_step)
        assert [k.time_seconds for k in smoothed] == [k.time_seconds for k in keyframes]
        assert smoothed[0] == keyframes[0]
        for previous, current in zip(smoothed, smoothed[1:], strict=False):
            assert (
                math.hypot(current.x - previous.x, current.y - previous.y) <= max_step
            )
            # Never outside the box spanned by the inputs.
            assert 0 <= current.x <= 1312 and 0 <= current.y <= 800


def test_smoothing_reaches_target_and_keeps_small_moves():
    keyframes = [Keyframe(float(t), 300, 0) for t in range(10)]
    keyframes[0] = Keyframe(0.0, 0, 0)
    smoothed = smooth_keyframes(keyframes, 100)
    assert [k.x for k in smoothed] == [0, 100, 200, 300, 300, 300, 300, 300, 300, 300]
    small = [Keyframe(0.0, 10, 10), Keyframe(1.0, 15, 12)]
    assert smooth_keyframes(small, 100) == small


def test_smoothing_accepts_plain_tuples_and_empty_input():
    assert smooth_keyframes([], 10) == []
    assert smooth_keyframes([(0, 5, 6)], 10) == [Keyframe(0.0, 5, 6)]


@pytest.mark.parametrize("max_step", [0, -1, math.inf, math.nan, True, "10"])
def test_smoothing_rejects_invalid_step(max_step):
    with pytest.raises(ValueError):
        smooth_keyframes([Keyframe(0.0, 0, 0)], max_step)


def test_smoothing_rejects_unordered_keyframes():
    with pytest.raises(ValueError):
        smooth_keyframes([Keyframe(1.0, 0, 0), Keyframe(0.5, 0, 0)], 10)


def test_crop_plan_from_centers_requires_centers():
    with pytest.raises(ValueError):
        crop_plan_from_centers(1920, 1080, "9:16", [], 10)
