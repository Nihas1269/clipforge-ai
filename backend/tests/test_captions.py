"""Caption generation, ASS rendering, colors and the caption font settings."""

from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import ValidationError

from app.captions.ass import (
    ass_event_text,
    escape_ass_line,
    format_ass_timestamp,
    hex_to_ass_color,
    render_ass,
)
from app.captions.generate import (
    CAPTION_TEXT_MAX_LENGTH,
    CaptionLine,
    clean_caption_text,
    generate_captions,
)
from app.captions.styles import (
    CaptionPosition,
    CaptionPreset,
    StyleValues,
    color_alpha,
    default_style,
    match_allowed_font,
    normalize_hex_color,
)
from app.core.config import DEFAULT_CAPTION_FONTS, Settings


@dataclass
class Segment:
    start_seconds: float
    end_seconds: float
    text: str
    words: list[dict[str, Any]] | None = None


@dataclass
class Caption:
    start_seconds: float
    end_seconds: float
    text: str


def words(*items: tuple[str, float, float]) -> list[dict[str, Any]]:
    return [{"word": w, "start": s, "end": e} for w, s, e in items]


def as_tuples(lines: list[CaptionLine]) -> list[tuple[float, float, str]]:
    return [(line.start, line.end, line.text) for line in lines]


# Generation from word timings


def test_generation_respects_clip_range_and_line_length():
    segments = [
        Segment(
            8.0,
            12.0,
            "before edge hello world",
            words(
                ("before", 8.0, 8.5),
                # Midpoint 10.1 is inside the clip; the start is clamped.
                ("edge", 9.8, 10.4),
                ("hello", 10.5, 11.0),
                ("world", 11.0, 11.5),
            ),
        ),
        Segment(
            12.0,
            16.0,
            "this is a longer sentence indeed",
            words(
                ("this", 12.0, 12.3),
                ("is", 12.3, 12.5),
                ("a", 12.5, 12.6),
                ("longer", 12.6, 13.0),
                ("sentence", 13.0, 13.6),
                ("indeed", 13.6, 14.0),
            ),
        ),
        Segment(
            19.5,
            22.0,
            "last gone",
            # "gone" has its midpoint after the clip end.
            words(("last", 19.6, 19.9), ("gone", 20.1, 20.5)),
        ),
        Segment(25.0, 30.0, "outside", words(("outside", 25.0, 26.0))),
    ]

    lines = generate_captions(segments, 10.0, 20.0, 12)

    assert as_tuples(lines) == [
        (0.0, 1.0, "edge hello"),
        (1.0, 1.5, "world"),
        (2.0, 2.6, "this is a"),
        (2.6, 3.0, "longer"),
        (3.0, 3.6, "sentence"),
        (3.6, 4.0, "indeed"),
        (9.6, 9.9, "last"),
    ]
    for line in lines:
        assert 0 <= line.start < line.end <= 10.0
        assert all(len(part) <= 12 for part in line.text.split("\n"))


def test_generation_breaks_lines_at_segment_boundaries():
    segments = [
        Segment(0.0, 1.0, "one", words(("one", 0.0, 0.5))),
        Segment(1.0, 2.0, "two", words(("two", 1.0, 1.5))),
    ]

    lines = generate_captions(segments, 0.0, 30.0, 40)

    assert [line.text for line in lines] == ["one", "two"]


def test_word_longer_than_line_is_wrapped_in_its_own_caption():
    segments = [
        Segment(
            0.0,
            3.0,
            "an extraordinarily long",
            words(
                ("an", 0.0, 0.5),
                ("extraordinarily", 0.5, 1.5),
                ("long", 1.5, 2.0),
            ),
        )
    ]

    lines = generate_captions(segments, 0.0, 20.0, 10)

    assert [line.text.replace("\n", "") for line in lines] == [
        "an",
        "extraordinarily",
        "long",
    ]
    assert all(len(part) <= 10 for line in lines for part in line.text.split("\n"))
    assert lines[1].start == 0.5 and lines[1].end == 1.5


def test_zero_length_words_get_a_minimum_duration():
    segments = [Segment(0.0, 2.0, "hi", words(("hi", 1.0, 1.0)))]

    assert as_tuples(generate_captions(segments, 0.0, 20.0, 32)) == [(1.0, 1.01, "hi")]


def test_word_text_is_cleaned():
    segments = [
        Segment(0.0, 2.0, "x", words((" Hello\x00", 0.0, 0.5), ("  ", 0.5, 0.6)))
    ]

    assert [line.text for line in generate_captions(segments, 0.0, 20.0, 32)] == [
        "Hello"
    ]


# Segment fallback


def test_fallback_to_segment_text_when_words_are_missing():
    segments = [
        Segment(5.0, 12.0, "The quick brown fox jumps over the lazy dog", None),
        Segment(12.0, 15.0, "Second part.", None),
        Segment(30.0, 35.0, "After the clip.", None),
    ]

    lines = generate_captions(segments, 10.0, 20.0, 16)

    # No word timing is invented: each segment is one caption over the part
    # of the segment inside the clip, its text wrapped to the line length.
    assert as_tuples(lines) == [
        (0.0, 2.0, "The quick brown\nfox jumps over\nthe lazy dog"),
        (2.0, 5.0, "Second part."),
    ]


@pytest.mark.parametrize(
    "bad_words",
    [
        [],
        [{"word": "a", "start": 1.0, "end": 0.5}],
        [{"word": "a", "start": "1", "end": 2.0}],
        [{"word": "a", "start": float("nan"), "end": 2.0}],
        [{"start": 1.0, "end": 2.0}],
        ["a"],
    ],
)
def test_fallback_when_word_timings_are_empty_or_invalid(bad_words):
    segments = [Segment(1.0, 3.0, "segment text", bad_words)]

    assert as_tuples(generate_captions(segments, 0.0, 20.0, 32)) == [
        (1.0, 3.0, "segment text")
    ]


def test_mixed_word_and_segment_timing():
    segments = [
        Segment(0.0, 2.0, "with words", words(("with", 0.0, 0.5), ("words", 0.5, 1.0))),
        Segment(2.0, 4.0, "no words", None),
    ]

    assert as_tuples(generate_captions(segments, 0.0, 20.0, 32)) == [
        (0.0, 1.0, "with words"),
        (2.0, 4.0, "no words"),
    ]


def test_overlapping_captions_are_trimmed_or_merged():
    segments = [
        Segment(0.0, 5.0, "one", None),
        Segment(3.0, 6.0, "two", None),
        Segment(8.0, 9.0, "a", None),
        Segment(8.0, 9.5, "b", None),
    ]

    assert as_tuples(generate_captions(segments, 0.0, 20.0, 32)) == [
        (0.0, 3.0, "one"),
        (3.0, 6.0, "two"),
        (8.0, 9.5, "a\nb"),
    ]


def test_overlap_trim_never_leaves_a_caption_shorter_than_the_minimum():
    segments = [
        # Extended to the 10 ms minimum (1.0 to 1.01)...
        Segment(1.0, 1.004, "a", None),
        # ...then overlapped 5 ms in: trimming would leave 5 ms, so it merges.
        Segment(1.005, 2.0, "b", None),
    ]

    lines = generate_captions(segments, 0.0, 20.0, 32)

    assert as_tuples(lines) == [(1.0, 2.0, "a\nb")]
    assert all(round(line.end - line.start, 3) >= 0.01 for line in lines)


def test_invalid_segments_and_blank_text_are_ignored():
    segments = [
        Segment(-1.0, 2.0, "negative", None),
        Segment(3.0, 3.0, "empty range", None),
        Segment(4.0, 5.0, " \x07 ", None),
        Segment(6.0, 7.0, "kept", None),
    ]

    assert as_tuples(generate_captions(segments, 0.0, 20.0, 32)) == [(6.0, 7.0, "kept")]


@pytest.mark.parametrize(
    "start, end, max_chars",
    [(5.0, 5.0, 32), (6.0, 5.0, 32), (-1.0, 5.0, 32), (0.0, 5.0, 0), (0.0, 5.0, True)],
)
def test_generation_rejects_invalid_arguments(start, end, max_chars):
    with pytest.raises(ValueError):
        generate_captions([], start, end, max_chars)


def test_clean_caption_text():
    assert clean_caption_text("  Hello \r\n\r\n world​ \t") == "Hello\nworld"
    with pytest.raises(ValueError):
        clean_caption_text(" \n\x00 ")
    with pytest.raises(ValueError):
        clean_caption_text("x" * (CAPTION_TEXT_MAX_LENGTH + 1))


# Colors and styles


@pytest.mark.parametrize(
    "value, expected",
    [
        ("#FF0000", "&H000000FF"),
        ("#00ff00", "&H0000FF00"),
        ("#0000FF", "&H00FF0000"),
        ("#112233", "&H00332211"),
        ("#11223380", "&H7F332211"),
        ("#FFFFFF00", "&HFFFFFFFF"),
        (" #abcdefff ", "&H00EFCDAB"),
    ],
)
def test_hex_to_ass_color(value, expected):
    assert hex_to_ass_color(value) == expected


@pytest.mark.parametrize(
    "value",
    ["red", "#FFF", "FF0000", "#GG0000", "#FF00000", "rgb(0,0,0)", "", "#FF00 00"],
)
def test_invalid_colors_are_rejected(value):
    with pytest.raises(ValueError):
        hex_to_ass_color(value)


def test_normalize_hex_color_and_alpha():
    assert normalize_hex_color("#abcdef") == "#ABCDEFFF"
    assert normalize_hex_color("#abcdef80") == "#ABCDEF80"
    assert color_alpha("#000000") == 255
    assert color_alpha("#00000040") == 0x40


def test_match_allowed_font_is_case_insensitive():
    fonts = ["Arial", "Open Sans"]

    assert match_allowed_font("open  sans", fonts) == "Open Sans"
    assert match_allowed_font("Comic Sans", fonts) is None


# ASS output


def test_ass_contains_sections_timestamps_and_style():
    style = default_style("Arial")
    captions = [
        Caption(1.0, 2.5, "Hello"),
        Caption(0.0, 0.75, "First"),
        Caption(3.0, 3.0, "Instant"),
        Caption(4.0, 5.0, " \x00 "),
    ]

    content = render_ass(captions, style, 1080, 1920)
    lines = content.splitlines()

    assert lines[0] == "[Script Info]"
    assert "[V4+ Styles]" in lines
    assert "[Events]" in lines
    assert lines.index("[Script Info]") < lines.index("[V4+ Styles]")
    assert lines.index("[V4+ Styles]") < lines.index("[Events]")
    assert "PlayResX: 1080" in lines
    assert "PlayResY: 1920" in lines
    assert (
        "Style: Default,Arial,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        "0,0,0,0,100,100,0,0,1,3,1,2,54,54,154,1"
    ) in lines
    dialogues = [line for line in lines if line.startswith("Dialogue:")]
    assert dialogues == [
        "Dialogue: 0,0:00:00.00,0:00:00.75,Default,,0,0,0,,First",
        "Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Hello",
        # A zero-length caption lasts at least one centisecond.
        "Dialogue: 0,0:00:03.00,0:00:03.01,Default,,0,0,0,,Instant",
    ]
    assert content.endswith("\n")


def test_ass_escapes_override_characters_and_newlines():
    style = default_style("Arial")
    captions = [
        Caption(
            0.0,
            1.0,
            "{\\b1}Bold\\N{\\pos(0,0)}\n"
            "Dialogue: 0,0:00:00.00,0:00:09.00,Default,,0,0,0,,evil\r\nend",
        )
    ]

    content = render_ass(captions, style, 1080, 1920)

    dialogues = [line for line in content.splitlines() if line.startswith("Dialogue:")]
    assert len(dialogues) == 1
    text = dialogues[0].split(",,0,0,0,,", 1)[1]
    assert "{" not in text and "}" not in text
    # The only backslashes are the line breaks written by the renderer.
    assert text.replace("\\N", "").count("\\") == 0
    assert "｛＼b1｝Bold＼N｛＼pos(0,0)｝" in text
    assert text.count("\\N") >= 2


def test_escape_ass_line():
    assert escape_ass_line("a{b}c\\d") == "a｛b｝c＼d"


def test_ass_event_text_wraps_and_uppercases():
    assert ass_event_text("hello big world", 10, True) == "HELLO BIG\\NWORLD"
    assert ass_event_text("one\ntwo", 32, False) == "one\\Ntwo"
    assert ass_event_text("\x00", 32, False) == ""


def test_ass_scales_style_and_applies_preset_and_position():
    style = StyleValues(
        preset=CaptionPreset.BOLD,
        font_family="Open Sans",
        font_size=80,
        primary_color="#FFE14DFF",
        outline_color="#00000080",
        position=CaptionPosition.TOP,
        uppercase=True,
        max_chars_per_line=24,
    )

    content = render_ass([Caption(0.0, 1.0, "big news")], style, 540, 960)

    assert (
        "Style: Default,Open Sans,40,&H004DE1FF,&H00FFFFFF,&H7F000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,2.5,0,8,27,27,77,1"
    ) in content.splitlines()
    assert "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,BIG NEWS" in content


def test_ass_middle_position_has_no_vertical_margin():
    style = StyleValues(**{**default_style("Arial").__dict__, "position": "middle"})

    content = render_ass([], style, 1920, 1080)

    assert content.splitlines()[-1].startswith("Format: Layer")
    assert ",5,96,96,0,1" in content


def test_ass_time_offset_converts_to_source_time():
    content = render_ass(
        [Caption(1.0, 2.0, "x")], default_style("Arial"), 1080, 1920, time_offset=60.0
    )

    assert "Dialogue: 0,0:01:01.00,0:01:02.00,Default,,0,0,0,,x" in content


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "0:00:00.00"),
        (1.5, "0:00:01.50"),
        (59.994, "0:00:59.99"),
        (59.996, "0:01:00.00"),
        (3661.239, "1:01:01.24"),
        (-3.0, "0:00:00.00"),
    ],
)
def test_format_ass_timestamp(seconds, expected):
    assert format_ass_timestamp(seconds) == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "1", True])
def test_format_ass_timestamp_rejects_invalid_times(bad):
    with pytest.raises(ValueError):
        format_ass_timestamp(bad)


@pytest.mark.parametrize("width, height", [(0, 1920), (1080, 9000), (1080.0, 1920)])
def test_render_ass_rejects_invalid_resolution(width, height):
    with pytest.raises(ValueError):
        render_ass([], default_style("Arial"), width, height)


@pytest.mark.parametrize("font", ["Arial,Bold", "Ari{al}", "Arial\\N", ""])
def test_render_ass_rejects_unsafe_font_names(font):
    with pytest.raises(ValueError):
        render_ass([], default_style(font), 1080, 1920)


def test_render_ass_rejects_invalid_colors():
    style = StyleValues(**{**default_style("Arial").__dict__, "primary_color": "red"})

    with pytest.raises(ValueError):
        render_ass([], style, 1080, 1920)


# Settings


def test_caption_fonts_default_and_parsing(monkeypatch):
    assert Settings().CAPTION_FONTS == list(DEFAULT_CAPTION_FONTS)

    monkeypatch.setenv("CAPTION_FONTS", " Roboto , open   sans,,roboto ")
    assert Settings().CAPTION_FONTS == ["Roboto", "open sans"]


@pytest.mark.parametrize("value", ["", " , ", "Arial,Bad{Font}", "Arial,Comma\\N"])
def test_invalid_caption_fonts_are_rejected(monkeypatch, value):
    monkeypatch.setenv("CAPTION_FONTS", value)

    with pytest.raises(ValidationError):
        Settings()
