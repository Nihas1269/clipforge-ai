import math
import re
from types import SimpleNamespace

import pytest

from app.subtitles.formats import (
    MAX_LINE_LENGTH,
    Cue,
    cues_from_segments,
    format_srt_timestamp,
    format_vtt_timestamp,
    subtitle_filename,
    to_srt,
    to_vtt,
)

SRT_TIMING = re.compile(
    r"^(\d{2,}):(\d{2}):(\d{2}),(\d{3}) --> (\d{2,}):(\d{2}):(\d{2}),(\d{3})$"
)
VTT_TIMING = re.compile(
    r"^(\d{2,}):(\d{2}):(\d{2})\.(\d{3}) --> (\d{2,}):(\d{2}):(\d{2})\.(\d{3})$"
)


def _srt_blocks(text: str) -> list[list[str]]:
    return (
        [block.split("\n") for block in text.strip("\n").split("\n\n")] if text else []
    )


def _vtt_cue_blocks(text: str) -> list[list[str]]:
    blocks = [block.split("\n") for block in text.strip("\n").split("\n\n")]
    assert blocks[0] == ["WEBVTT"]
    return blocks[1:]


# Timestamps


@pytest.mark.parametrize(
    ("seconds", "srt", "vtt"),
    [
        (0, "00:00:00,000", "00:00:00.000"),
        (1.5, "00:00:01,500", "00:00:01.500"),
        (61.007, "00:01:01,007", "00:01:01.007"),
        (3661.25, "01:01:01,250", "01:01:01.250"),
        (59.9996, "00:01:00,000", "00:01:00.000"),
        (7199.999, "01:59:59,999", "01:59:59.999"),
        (100 * 3600, "100:00:00,000", "100:00:00.000"),
        (-0.5, "00:00:00,000", "00:00:00.000"),
    ],
)
def test_timestamps(seconds, srt, vtt):
    assert format_srt_timestamp(seconds) == srt
    assert format_vtt_timestamp(seconds) == vtt


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, "1.0", None, True])
def test_invalid_timestamps_are_rejected(value):
    with pytest.raises(ValueError):
        format_srt_timestamp(value)
    with pytest.raises(ValueError):
        to_vtt([Cue(value, 1.0, "text")])


# SRT


def test_srt_has_sequential_indices_and_comma_milliseconds():
    cues = [
        Cue(0.0, 2.5, "First."),
        Cue(2.5, 5.0, "Second."),
        Cue(5.0, 7.125, "Third."),
    ]

    text = to_srt(cues)

    assert text == (
        "1\n00:00:00,000 --> 00:00:02,500\nFirst.\n\n"
        "2\n00:00:02,500 --> 00:00:05,000\nSecond.\n\n"
        "3\n00:00:05,000 --> 00:00:07,125\nThird.\n"
    )
    blocks = _srt_blocks(text)
    assert [block[0] for block in blocks] == ["1", "2", "3"]
    assert all(SRT_TIMING.match(block[1]) for block in blocks)


def test_srt_skips_empty_cues_and_keeps_numbering_sequential():
    cues = [
        Cue(0, 1, "a"),
        Cue(1, 2, "   "),
        Cue(2, 3, ""),
        Cue(3, 4, "\x00\x07"),
        Cue(4, 5, "b"),
    ]

    blocks = _srt_blocks(to_srt(cues))

    assert [block[0] for block in blocks] == ["1", "2"]
    assert [block[2] for block in blocks] == ["a", "b"]


def test_empty_transcript_gives_empty_srt_and_header_only_vtt():
    assert to_srt([]) == ""
    assert to_vtt([]) == "WEBVTT\n"
    assert to_srt([Cue(0, 1, " ")]) == ""
    assert to_vtt([Cue(0, 1, " ")]) == "WEBVTT\n"


def test_long_captions_are_wrapped_without_losing_text():
    words = ["word%02d" % i for i in range(40)] + ["https://example.com/" + "x" * 60]
    text = " ".join(words)

    for output in (to_srt([Cue(0, 10, text)]), to_vtt([Cue(0, 10, text)])):
        lines = output.strip("\n").split("\n\n")[-1].split("\n")[2:]
        assert len(lines) > 1
        assert " ".join(lines).split() == words
        # Only a single word longer than the limit may exceed it.
        assert all(len(line) <= MAX_LINE_LENGTH or " " not in line for line in lines)


def test_line_breaks_and_blank_lines_in_text_cannot_end_a_cue():
    cues = [
        Cue(0, 1, "line one\n\nline two\r\n\r\n3\n00:00:00,000 --> 00:00:01,000"),
        Cue(1, 2, "next"),
    ]

    srt = to_srt(cues)
    vtt = to_vtt(cues)

    assert len(_srt_blocks(srt)) == 2
    assert len(_vtt_cue_blocks(vtt)) == 2
    assert "-->" not in "\n".join(_srt_blocks(srt)[0][2:])
    assert "-->" not in "\n".join(_vtt_cue_blocks(vtt)[0][2:])


def test_special_characters():
    cue = Cue(0, 1, "<b>Tom & Jerry</b> --> \"quotes\" 'single' café తెలుగు 😀\ttab")

    srt_text = _srt_blocks(to_srt([cue]))[0][2:]
    vtt_text = _vtt_cue_blocks(to_vtt([cue]))[0][2:]

    assert (
        " ".join(srt_text)
        == "<b>Tom & Jerry</b> -> \"quotes\" 'single' café తెలుగు 😀 tab"
    )
    assert " ".join(vtt_text) == (
        "&lt;b&gt;Tom &amp; Jerry&lt;/b&gt; --&gt; \"quotes\" 'single' café తెలుగు 😀 tab"
    )


def test_overlapping_cues_keep_their_source_times_in_start_order():
    cues = [Cue(5.0, 7.0, "second"), Cue(0.0, 6.0, "first"), Cue(5.0, 5.5, "third")]

    srt = _srt_blocks(to_srt(cues))
    vtt = _vtt_cue_blocks(to_vtt(cues))

    assert [block[2] for block in srt] == ["first", "second", "third"]
    assert [block[1] for block in srt] == [
        "00:00:00,000 --> 00:00:06,000",
        "00:00:05,000 --> 00:00:07,000",
        "00:00:05,000 --> 00:00:05,500",
    ]
    assert [block[1] for block in vtt] == [
        "00:00:00.000 --> 00:00:06.000",
        "00:00:05.000 --> 00:00:07.000",
        "00:00:05.000 --> 00:00:05.500",
    ]


def test_cue_shorter_than_a_millisecond_still_ends_after_it_starts():
    [block] = _srt_blocks(to_srt([Cue(1.0001, 1.0004, "blink")]))

    assert block[1] == "00:00:01,000 --> 00:00:01,001"


# VTT


def test_vtt_starts_with_header_and_uses_dot_milliseconds():
    text = to_vtt([Cue(0.0, 2.5, "First."), Cue(3661.25, 3662.0, "Later.")])

    assert text.startswith("WEBVTT\n\n")
    assert text == (
        "WEBVTT\n\n"
        "1\n00:00:00.000 --> 00:00:02.500\nFirst.\n\n"
        "2\n01:01:01.250 --> 01:01:02.000\nLater.\n"
    )
    blocks = _vtt_cue_blocks(text)
    assert all(VTT_TIMING.match(block[1]) for block in blocks)
    assert all("," not in block[1] for block in blocks)


# Helpers


def test_cues_from_segments_use_the_segment_times():
    segments = [
        SimpleNamespace(start_seconds=1.25, end_seconds=2.5, text="a"),
        SimpleNamespace(start_seconds=2.5, end_seconds=4.0, text="b"),
    ]

    assert cues_from_segments(segments) == [Cue(1.25, 2.5, "a"), Cue(2.5, 4.0, "b")]


@pytest.mark.parametrize(
    ("original", "extension", "expected"),
    [
        ("My talk.mp4", "srt", "My_talk.srt"),
        ("lecture.final.mov", "vtt", "lecture.final.vtt"),
        ("no-extension", "srt", "no-extension.srt"),
        (None, "srt", "transcript.srt"),
        ("", "vtt", "transcript.vtt"),
        ("..mp4", "srt", "transcript.srt"),
        ('a"b;c\r\nd.mp4', "srt", "a_b_c_d.srt"),
        ("తెలుగు.mp4", "vtt", "transcript.vtt"),
        ("x" * 300 + ".mp4", "srt", "x" * 100 + ".srt"),
    ],
)
def test_subtitle_filename_is_header_safe(original, extension, expected):
    filename = subtitle_filename(original, extension)

    assert filename == expected
    assert re.fullmatch(r"[A-Za-z0-9._-]+", filename)
