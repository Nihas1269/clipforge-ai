import itertools
import math
import random

import pytest
from pydantic import ValidationError

from app.clips import (
    TIME_TOLERANCE_SECONDS,
    ClipCandidate,
    ClipDetectionOptions,
    ClipDetector,
    HeuristicClipDetector,
    InvalidClipCandidateError,
    validate_candidates,
)
from app.core.config import Settings
from app.transcription.base import TranscriptionSegment

PLAIN = "and so we continue talking here"
HOT = "Is this the secret? It is amazing! You never knew this important lesson!"


def seg(start, end, text=PLAIN):
    return TranscriptionSegment(start=start, end=end, text=text)


def transcript(duration=120, hot=(60, 80), step=5):
    """`step`-second segments of plain speech, with high-signal text in `hot`."""
    segments = []
    for start in range(0, duration, step):
        text = HOT if hot[0] <= start < hot[1] else PLAIN
        segments.append(seg(start, start + step, text))
    return segments


def candidate(start, end, score=0.5, title="Clip", reason="Because."):
    return ClipCandidate(start=start, end=end, score=score, title=title, reason=reason)


OPTIONS = ClipDetectionOptions(min_seconds=15, max_seconds=30, max_clips=10)


def assert_no_overlaps(clips):
    for a, b in itertools.combinations(clips, 2):
        assert a.end <= b.start or b.end <= a.start, (a, b)


# --- HeuristicClipDetector --------------------------------------------------


def test_heuristic_detector_implements_protocol():
    assert isinstance(HeuristicClipDetector(keywords=[]), ClipDetector)


def test_empty_transcript_returns_empty_list():
    assert HeuristicClipDetector().detect([], 120.0, OPTIONS) == []


def test_transcript_without_words_returns_empty_list():
    segments = [seg(start, start + 5, "  ") for start in range(0, 60, 5)]

    assert HeuristicClipDetector().detect(segments, 60.0, OPTIONS) == []


def test_video_shorter_than_min_length_returns_empty_list():
    segments = [seg(0, 5), seg(5, 10)]

    assert HeuristicClipDetector().detect(segments, 10.0, OPTIONS) == []


def test_candidates_respect_length_and_video_bounds():
    duration = 118.0
    segments = transcript(duration=115) + [seg(115, 118)]

    clips = HeuristicClipDetector().detect(segments, duration, OPTIONS)

    assert clips
    for clip in clips:
        assert 0 <= clip.start < clip.end <= duration
        length = clip.end - clip.start
        assert OPTIONS.min_seconds - TIME_TOLERANCE_SECONDS <= length
        assert length <= OPTIONS.max_seconds + TIME_TOLERANCE_SECONDS
        assert 0.0 <= clip.score <= 1.0


def test_candidates_are_aligned_to_segment_boundaries():
    segments = [seg(0, 4.2), seg(4.2, 11.7), seg(11.7, 19.9), seg(19.9, 26.3)]
    segments += [seg(26.3 + 6 * i, 32.3 + 6 * i, HOT) for i in range(10)]
    starts = {segment.start for segment in segments}
    ends = {segment.end for segment in segments}

    clips = HeuristicClipDetector().detect(segments, 90.0, OPTIONS)

    assert clips
    for clip in clips:
        assert clip.start in starts
        assert clip.end in ends


def test_candidates_do_not_overlap():
    clips = HeuristicClipDetector().detect(transcript(), 120.0, OPTIONS)

    assert len(clips) > 1
    assert_no_overlaps(clips)


@pytest.mark.parametrize("max_clips", [1, 2, 3])
def test_max_clips_is_respected(max_clips):
    options = ClipDetectionOptions(min_seconds=15, max_seconds=30, max_clips=max_clips)

    clips = HeuristicClipDetector().detect(transcript(), 120.0, options)

    assert len(clips) == max_clips


def test_output_is_deterministic_and_independent_of_input_order():
    segments = transcript()
    shuffled = list(segments)
    random.Random(7).shuffle(shuffled)

    first = HeuristicClipDetector().detect(segments, 120.0, OPTIONS)
    second = HeuristicClipDetector().detect(segments, 120.0, OPTIONS)
    third = HeuristicClipDetector().detect(shuffled, 120.0, OPTIONS)

    assert first == second == third


def test_results_are_sorted_by_score():
    clips = HeuristicClipDetector().detect(transcript(), 120.0, OPTIONS)

    scores = [clip.score for clip in clips]
    assert scores == sorted(scores, reverse=True)


def test_higher_signal_window_ranks_first():
    clips = HeuristicClipDetector().detect(transcript(hot=(60, 80)), 120.0, OPTIONS)

    top = clips[0]
    assert 60 <= top.start and top.end <= 80
    # Equal scores within the hot region: the earliest start, then the
    # earliest end, wins.
    assert (top.start, top.end) == (60.0, 75.0)
    assert all(top.score > clip.score for clip in clips[1:])
    assert top.title.startswith("Is this the secret?")
    assert "keyword matches" in top.reason


def test_each_signal_raises_the_score():
    detector = HeuristicClipDetector(keywords=["banana"])
    options = ClipDetectionOptions(min_seconds=15, max_seconds=15, max_clips=1)

    def score(text):
        segments = [seg(0, 5, text), seg(5, 10, PLAIN), seg(10, 15, PLAIN)]
        (clip,) = detector.detect(segments, 15.0, options)
        return clip.score

    base = score("one two three four five six")
    assert score("one two three four five six seven eight nine ten") > base
    assert score("one two three four five six?") > base
    assert score("one two three four five banana") > base


def test_keywords_are_configurable():
    segments = transcript(hot=(0, 0))
    segments[9] = seg(45, 50, "and so we continue banana here")

    with_keyword = HeuristicClipDetector(keywords=["Banana"]).detect(
        segments, 120.0, OPTIONS
    )
    without = HeuristicClipDetector(keywords=[]).detect(segments, 120.0, OPTIONS)

    assert with_keyword[0].start <= 45 and with_keyword[0].end >= 50
    assert (without[0].start, without[0].end) == (0.0, 15.0)


def test_keywords_match_whole_words_and_phrases_case_insensitively():
    detector = HeuristicClipDetector(keywords=["how to", "tip"])

    assert detector._count_keywords("HOW  TO cook: a Tip, tips, multiple") == 2


def test_keywords_default_to_settings(monkeypatch):
    monkeypatch.setenv("CLIP_KEYWORDS", " Banana ,, apple pie ")

    assert HeuristicClipDetector().keywords == ("banana", "apple pie")


def test_segments_past_the_video_end_or_invalid_are_ignored():
    segments = transcript(duration=60)
    segments.append(seg(55, 200, HOT))

    clips = HeuristicClipDetector().detect(segments, 60.0, OPTIONS)

    assert clips
    assert all(clip.end <= 60.0 for clip in clips)
    assert all("secret" not in clip.title.lower() for clip in clips)


def test_segment_end_within_tolerance_is_snapped_to_duration():
    segments = [seg(0, 10), seg(10, 20.0005)]

    (clip,) = HeuristicClipDetector().detect(segments, 20.0, OPTIONS)

    assert clip.end == 20.0


def test_title_falls_back_and_is_bounded():
    long_word = "x" * 500
    segments = [seg(0, 10, long_word), seg(10, 20, "word")]

    (clip,) = HeuristicClipDetector().detect(segments, 20.0, OPTIONS)

    assert len(clip.title) <= 83
    assert clip.title.endswith("...")


@pytest.mark.parametrize("duration", [0, -1, math.nan, math.inf, True, "60"])
def test_invalid_video_duration_is_rejected(duration):
    with pytest.raises(ValueError):
        HeuristicClipDetector().detect(transcript(), duration, OPTIONS)


# --- validate_candidates ----------------------------------------------------


def test_validate_snaps_float_noise_at_the_bounds():
    (clip,) = validate_candidates([candidate(-0.0005, 20.0004)], 20.0, OPTIONS)

    assert (clip.start, clip.end) == (0.0, 20.0)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (-0.5, 20.0),  # starts before the video
        (5.0, 30.5),  # ends after the video
        (10.0, 10.0),  # empty
        (20.0, 5.0),  # inverted
    ],
)
def test_validate_rejects_out_of_bounds_timestamps(start, end):
    assert validate_candidates([candidate(start, end)], 30.0, OPTIONS) == []


def test_validate_drops_too_short_and_too_long_candidates():
    clips = validate_candidates(
        [
            candidate(0, 14.9),
            candidate(0, 30.1),
            candidate(40, 55),
            candidate(60, 90),
            candidate(100, 114.9995),
        ],
        200.0,
        OPTIONS,
    )

    assert [(clip.start, clip.end) for clip in clips] == [
        (40, 55),
        (60, 90),
        (100, 114.9995),
    ]


def test_validate_keeps_higher_score_of_overlapping_candidates():
    clips = validate_candidates(
        [candidate(0, 20, score=0.3), candidate(10, 30, score=0.9)], 60.0, OPTIONS
    )

    assert [(clip.start, clip.score) for clip in clips] == [(10, 0.9)]


def test_validate_allows_touching_candidates():
    clips = validate_candidates(
        [candidate(0, 20, score=0.4), candidate(20, 40, score=0.6)], 60.0, OPTIONS
    )

    assert [(clip.start, clip.end) for clip in clips] == [(20, 40), (0, 20)]


def test_validate_tie_break_is_deterministic():
    items = [
        candidate(10, 30, title="b"),
        candidate(5, 25, title="c"),
        candidate(5, 20, title="a"),
    ]

    for order in itertools.permutations(items):
        clips = validate_candidates(order, 60.0, OPTIONS)
        # Equal scores: earliest start, then earliest end wins.
        assert [(clip.start, clip.end) for clip in clips] == [(5, 20)]


def test_validate_respects_max_clips_and_ranks_by_score():
    options = ClipDetectionOptions(min_seconds=15, max_seconds=30, max_clips=2)
    items = [candidate(i * 20, i * 20 + 15, score=i / 10) for i in range(5)]

    clips = validate_candidates(items, 200.0, options)

    assert [clip.score for clip in clips] == [0.4, 0.3]


def test_validate_rejects_non_candidates():
    with pytest.raises(TypeError):
        validate_candidates([(0, 20)], 60.0, OPTIONS)


def test_validate_rejects_invalid_duration():
    with pytest.raises(ValueError):
        validate_candidates([], math.nan, OPTIONS)


# --- ClipCandidate and ClipDetectionOptions ---------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"score": 1.01},
        {"score": -0.01},
        {"score": math.nan},
        {"score": True},
        {"start": math.inf},
        {"end": "20"},
        {"title": "   "},
        {"title": "x" * 201},
        {"reason": ""},
        {"reason": None},
    ],
)
def test_candidate_schema_is_strict(overrides):
    values = {"start": 0, "end": 20, "score": 0.5, "title": "T", "reason": "R"}
    values.update(overrides)

    with pytest.raises(InvalidClipCandidateError):
        ClipCandidate(**values)


def test_candidate_strips_text_and_converts_numbers():
    clip = ClipCandidate(start=1, end=20, score=1, title="  T ", reason=" R ")

    assert (clip.start, clip.end, clip.score) == (1.0, 20.0, 1.0)
    assert (clip.title, clip.reason) == ("T", "R")


def test_options_defaults():
    options = ClipDetectionOptions()

    assert (options.min_seconds, options.max_seconds, options.max_clips) == (
        15.0,
        90.0,
        10,
    )


def test_options_from_settings(monkeypatch):
    monkeypatch.setenv("CLIP_MIN_SECONDS", "20")
    monkeypatch.setenv("CLIP_MAX_SECONDS", "45")
    monkeypatch.setenv("CLIP_MAX_CLIPS", "4")

    options = ClipDetectionOptions.from_settings()

    assert (options.min_seconds, options.max_seconds, options.max_clips) == (
        20.0,
        45.0,
        4,
    )
    assert ClipDetectionOptions.from_settings(Settings()) == options


@pytest.mark.parametrize(
    "values",
    [
        {"min_seconds": 0},
        {"min_seconds": 30, "max_seconds": 20},
        {"max_clips": 0},
        {"max_clips": 1.5},
        {"max_clips": True},
        {"max_seconds": math.inf},
    ],
)
def test_options_reject_invalid_values(values):
    with pytest.raises(ValueError):
        ClipDetectionOptions(**values)


@pytest.mark.parametrize(
    "env",
    [
        {"CLIP_MIN_SECONDS": "60", "CLIP_MAX_SECONDS": "30"},
        {"CLIP_MIN_SECONDS": "0"},
        {"CLIP_MAX_CLIPS": "0"},
        {"CLIP_MAX_SECONDS": "601"},
    ],
)
def test_settings_reject_invalid_clip_limits(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings()
