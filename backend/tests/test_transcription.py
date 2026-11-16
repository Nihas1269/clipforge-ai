import logging
import math
import wave

import pytest

from app.core.config import Settings
from app.transcription import (
    FakeTranscriptionProvider,
    InvalidLanguageError,
    InvalidTranscriptionResultError,
    TranscriptionConfigurationError,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionResult,
    TranscriptionSegment,
    WordTiming,
    get_transcription_provider,
    normalize_language,
)
from app.transcription.fake import (
    FAKE_MODEL_NAME,
    FALLBACK_BYTES_PER_SECOND,
    probe_duration_seconds,
)

PRODUCTION_SECRET = "test-only-production-secret-0123456789abcdef"


def fixed_duration(seconds: float):
    return lambda _path: seconds


@pytest.fixture
def audio_file(tmp_path):
    path = tmp_path / "audio.bin"
    path.write_bytes(b"\x00" * 16)
    return path


def make_wav(path, seconds: float, rate: int = 16_000):
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


def assert_well_formed(result: TranscriptionResult, duration: float):
    previous_end = 0.0
    for segment in result.segments:
        assert 0 <= segment.start < segment.end <= duration
        # Monotonically increasing and non-overlapping.
        assert segment.start >= previous_end
        previous_end = segment.end
        previous_word_end = segment.start
        for word in segment.words or ():
            assert segment.start <= word.start <= word.end <= segment.end
            assert word.start >= previous_word_end
            previous_word_end = word.end


# --- Language ------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", "auto", "AUTO", " Auto "])
def test_automatic_language_is_none(value):
    assert normalize_language(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("en", "en"),
        ("EN", "en"),
        (" te ", "te"),
        ("hi-IN", "hi-IN"),
        ("ta", "ta"),
        ("kn", "kn"),
        ("ml", "ml"),
        ("zh-Hant-TW", "zh-Hant-TW"),
        ("fil", "fil"),
        # Explicitly unknown is kept, not turned into "auto".
        ("und", "und"),
    ],
)
def test_iso_language_codes_are_accepted_without_relabeling(value, expected):
    assert normalize_language(value) == expected


@pytest.mark.parametrize(
    "value",
    ["e", "english", "en_US", "en-", "-en", "en--US", "hi IN", "తె", "a" * 40, 5],
)
def test_invalid_language_is_rejected(value):
    with pytest.raises(InvalidLanguageError):
        normalize_language(value)


# --- Result types --------------------------------------------------------


def test_segment_must_end_after_it_starts():
    with pytest.raises(InvalidTranscriptionResultError):
        TranscriptionSegment(start=1.0, end=1.0, text="x")
    with pytest.raises(InvalidTranscriptionResultError):
        TranscriptionSegment(start=2.0, end=1.0, text="x")


@pytest.mark.parametrize("bad", [-0.1, math.nan, math.inf, True, "1.0", None])
def test_segment_times_must_be_finite_non_negative_numbers(bad):
    with pytest.raises(InvalidTranscriptionResultError):
        TranscriptionSegment(start=bad, end=5.0, text="x")


def test_word_timing_validation():
    assert WordTiming("hi", 1, 1).end == 1.0  # zero-length words are allowed
    with pytest.raises(InvalidTranscriptionResultError):
        WordTiming("hi", 2.0, 1.0)
    with pytest.raises(InvalidTranscriptionResultError):
        WordTiming("  ", 0.0, 1.0)
    with pytest.raises(InvalidTranscriptionResultError):
        WordTiming("hi", -1.0, 1.0)


def test_segment_words_must_be_word_timings():
    with pytest.raises(InvalidTranscriptionResultError):
        TranscriptionSegment(start=0, end=1, text="hi", words=[{"word": "hi"}])


def test_result_rejects_out_of_order_segments_and_bad_fields():
    first = TranscriptionSegment(start=5.0, end=6.0, text="b")
    second = TranscriptionSegment(start=0.0, end=1.0, text="a")
    with pytest.raises(InvalidTranscriptionResultError):
        TranscriptionResult(language="en", segments=(first, second), provider="fake")
    with pytest.raises(InvalidTranscriptionResultError):
        TranscriptionResult(language="english!", segments=(), provider="fake")
    with pytest.raises(InvalidTranscriptionResultError):
        TranscriptionResult(language="en", segments=(), provider=" ")


def test_result_full_text_and_duration_check():
    result = TranscriptionResult(
        language="hi",
        segments=[
            TranscriptionSegment(0.0, 1.0, " नमस्ते "),
            TranscriptionSegment(1.0, 2.0, ""),
            TranscriptionSegment(2.0, 3.0, "world", words=[WordTiming("world", 2, 3)]),
        ],
        provider="fake",
    )

    assert result.full_text == "नमस्ते world"
    assert isinstance(result.segments, tuple)
    result.check_within_duration(3.0)
    with pytest.raises(InvalidTranscriptionResultError):
        result.check_within_duration(2.5)


def test_empty_result_is_valid():
    result = TranscriptionResult(language=None, segments=(), provider="fake")

    assert result.full_text == ""
    assert result.language is None
    result.check_within_duration(0)


# --- Fake provider -------------------------------------------------------


def test_fake_provider_implements_the_protocol():
    assert isinstance(FakeTranscriptionProvider(), TranscriptionProvider)


def test_fake_provider_is_deterministic(audio_file):
    provider = FakeTranscriptionProvider(duration_probe=fixed_duration(47.3))

    first = provider.transcribe(audio_file)
    second = provider.transcribe(audio_file)
    third = FakeTranscriptionProvider(duration_probe=fixed_duration(47.3)).transcribe(
        str(audio_file)
    )

    assert first == second == third
    assert first.provider == "fake"
    assert first.model == FAKE_MODEL_NAME


@pytest.mark.parametrize("duration", [0.3, 1.0, 4.999, 5.0, 12.5, 47.3, 3600.25])
def test_fake_segments_are_increasing_and_within_the_duration(audio_file, duration):
    result = FakeTranscriptionProvider(
        duration_probe=fixed_duration(duration)
    ).transcribe(audio_file)

    assert result.segments
    assert_well_formed(result, duration)
    assert result.segments[0].start == 0.0
    # The segments cover the audio up to the last whole millisecond.
    assert result.segments[-1].end == math.floor(duration * 1000) / 1000
    assert len(result.segments) == math.ceil(math.floor(duration * 1000) / 5000)


def test_fake_segments_follow_the_segment_length(audio_file):
    result = FakeTranscriptionProvider(duration_probe=fixed_duration(12.5)).transcribe(
        audio_file
    )

    assert [(s.start, s.end) for s in result.segments] == [
        (0.0, 5.0),
        (5.0, 10.0),
        (10.0, 12.5),
    ]
    assert result.full_text == " ".join(s.text for s in result.segments)


def test_very_short_audio_gives_one_segment(audio_file):
    result = FakeTranscriptionProvider(duration_probe=fixed_duration(0.05)).transcribe(
        audio_file
    )

    assert [(s.start, s.end) for s in result.segments] == [(0.0, 0.05)]
    assert_well_formed(result, 0.05)


@pytest.mark.parametrize("duration", [0.0, 0.0004])
def test_silent_or_empty_audio_gives_zero_segments(audio_file, duration):
    result = FakeTranscriptionProvider(
        duration_probe=fixed_duration(duration)
    ).transcribe(audio_file)

    assert result.segments == ()
    assert result.full_text == ""


def test_fake_provider_reads_wav_duration(tmp_path):
    path = make_wav(tmp_path / "audio.wav", seconds=7.0)

    result = FakeTranscriptionProvider().transcribe(path)

    assert probe_duration_seconds(path) == pytest.approx(7.0)
    assert [(s.start, s.end) for s in result.segments] == [(0.0, 5.0), (5.0, 7.0)]


def test_fake_provider_estimates_duration_of_other_files(tmp_path):
    path = tmp_path / "audio.raw"
    path.write_bytes(b"\x01" * (FALLBACK_BYTES_PER_SECOND * 3))

    result = FakeTranscriptionProvider().transcribe(path)

    assert result.segments[-1].end == 3.0


def test_empty_file_gives_zero_segments(tmp_path):
    path = tmp_path / "empty.wav"
    path.write_bytes(b"")

    assert FakeTranscriptionProvider().transcribe(path).segments == ()


def test_fake_provider_language(audio_file):
    provider = FakeTranscriptionProvider(duration_probe=fixed_duration(1.0))

    assert provider.transcribe(audio_file).language == "en"
    assert provider.transcribe(audio_file, language="auto").language == "en"
    # An explicit language is reported as given, never relabeled.
    assert provider.transcribe(audio_file, language="te").language == "te"
    assert provider.transcribe(audio_file, language="hi-IN").language == "hi-IN"
    assert provider.transcribe(audio_file, language="und").language == "und"
    unknown = FakeTranscriptionProvider(
        detected_language=None, duration_probe=fixed_duration(1.0)
    )
    assert unknown.transcribe(audio_file).language is None


def test_fake_provider_rejects_invalid_language_before_reading(tmp_path):
    def probe(_path):
        raise AssertionError("the file must not be read")

    provider = FakeTranscriptionProvider(duration_probe=probe)

    with pytest.raises(InvalidLanguageError):
        provider.transcribe(tmp_path / "missing.wav", language="english")


def test_fake_provider_preserves_unicode_and_code_switched_text(audio_file):
    texts = [
        "నేను office కి వెళ్తున్నాను",
        "मैं kal meeting में आऊँगा",
        "வணக்கம் everyone",
        "ನಮಸ್ಕಾರ friends",
        "നമസ്കാരം world",
    ]
    provider = FakeTranscriptionProvider(
        segment_texts=texts, duration_probe=fixed_duration(25.0)
    )

    result = provider.transcribe(audio_file, language="te")

    assert [s.text for s in result.segments] == texts
    assert [w.word for w in result.segments[0].words] == texts[0].split()
    assert_well_formed(result, 25.0)


def test_fake_provider_fails_for_missing_file(tmp_path):
    with pytest.raises(TranscriptionError):
        FakeTranscriptionProvider().transcribe(tmp_path / "missing.wav")
    with pytest.raises(TranscriptionError):
        FakeTranscriptionProvider().transcribe(tmp_path)


@pytest.mark.parametrize("duration", [-1.0, math.nan, math.inf])
def test_fake_provider_fails_for_invalid_duration(audio_file, duration):
    provider = FakeTranscriptionProvider(duration_probe=fixed_duration(duration))

    with pytest.raises(TranscriptionError):
        provider.transcribe(audio_file)


def test_fake_provider_reports_read_errors_without_details(audio_file):
    def probe(_path):
        raise PermissionError("/secret/path denied")

    with pytest.raises(TranscriptionError) as excinfo:
        FakeTranscriptionProvider(duration_probe=probe).transcribe(audio_file)

    assert "/secret/path" not in str(excinfo.value)


@pytest.mark.parametrize("kwargs", [{"segment_seconds": 0}, {"segment_texts": []}])
def test_fake_provider_rejects_invalid_options(kwargs):
    with pytest.raises(ValueError):
        FakeTranscriptionProvider(**kwargs)


def test_fake_provider_logs_without_transcript_text(audio_file, caplog):
    provider = FakeTranscriptionProvider(
        segment_texts=["PRIVATE-WORDS"], duration_probe=fixed_duration(2.0)
    )

    with caplog.at_level(logging.INFO, logger="app.transcription.fake"):
        provider.transcribe(audio_file)

    (record,) = [
        r for r in caplog.records if r.getMessage() == "transcription_completed"
    ]
    assert record.provider == "fake"
    assert record.segment_count == 1
    assert "PRIVATE-WORDS" not in caplog.text


# --- Factory -------------------------------------------------------------


def test_factory_returns_fake_provider_by_default(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_PROVIDER", raising=False)

    assert isinstance(get_transcription_provider(), FakeTranscriptionProvider)


@pytest.mark.parametrize("app_env", ["development", "test"])
def test_factory_default_is_fake_in_development_and_test(app_env):
    settings = Settings(APP_ENV=app_env)

    assert settings.TRANSCRIPTION_PROVIDER == "fake"
    assert isinstance(get_transcription_provider(settings), FakeTranscriptionProvider)


@pytest.mark.parametrize("value", [" FAKE ", "Fake", ""])
def test_factory_normalizes_provider_name(monkeypatch, value):
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", value)

    assert isinstance(get_transcription_provider(), FakeTranscriptionProvider)


@pytest.mark.parametrize("value", ["whisper", "openai", "unknown"])
def test_factory_raises_for_unknown_provider(monkeypatch, value):
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", value)

    with pytest.raises(TranscriptionConfigurationError) as excinfo:
        get_transcription_provider()
    assert value in str(excinfo.value)


def test_unknown_provider_does_not_stop_settings_from_loading():
    settings = Settings(TRANSCRIPTION_PROVIDER="nope")

    assert settings.TRANSCRIPTION_PROVIDER == "nope"
    with pytest.raises(TranscriptionConfigurationError):
        get_transcription_provider(settings)


def test_factory_refuses_fake_provider_in_production():
    settings = Settings(APP_ENV="production", SECRET_KEY=PRODUCTION_SECRET)

    with pytest.raises(TranscriptionConfigurationError):
        get_transcription_provider(settings)
