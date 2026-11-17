import httpx
import pytest

from app.transcription import (
    InvalidTranscriptionResultError,
    TranscriptionProvider,
    TranscriptionProviderError,
)
from tests.transcription_support import (
    API_KEY,
    ENDPOINT,
    MODEL,
    fixed_duration,
    json_handler,
    make_provider,
    verbose_json,
)

# --- OpenAI-compatible: request format ------------------------------------


def test_openai_provider_implements_the_protocol():
    provider = make_provider(json_handler(verbose_json()))

    assert isinstance(provider, TranscriptionProvider)
    assert provider.name == "openai_compatible"


def test_request_format_and_auth_header(audio_file):
    calls = []
    provider = make_provider(json_handler(verbose_json(), calls=calls))

    provider.transcribe(audio_file, language="hi-IN")

    (request,) = calls
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="model"\r\n\r\n' + MODEL.encode() in body
    assert b'name="response_format"\r\n\r\nverbose_json' in body
    assert b'name="timestamp_granularities[]"\r\n\r\nsegment' in body
    assert b'name="timestamp_granularities[]"\r\n\r\nword' in body
    # Only the primary ISO 639-1 subtag is sent.
    assert b'name="language"\r\n\r\nhi\r\n' in body
    # The audio is sent under a generic name, never the local file name.
    assert b'filename="audio.wav"' in body
    assert b"private" not in body
    assert audio_file.read_bytes() in body


@pytest.mark.parametrize("language", [None, "auto", "und"])
def test_no_language_is_sent_for_automatic_detection(audio_file, language):
    calls = []
    provider = make_provider(json_handler(verbose_json(), calls=calls))

    provider.transcribe(audio_file, language=language)

    assert b'name="language"' not in calls[0].content


def test_loopback_endpoint_may_use_http_without_api_key(audio_file):
    calls = []
    provider = make_provider(
        json_handler(verbose_json(), calls=calls),
        base_url="http://localhost:8000/v1/",
        api_key=None,
    )

    provider.transcribe(audio_file)

    assert str(calls[0].url) == "http://localhost:8000/v1/audio/transcriptions"
    assert "authorization" not in calls[0].headers


def test_redirects_are_not_followed(audio_file):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://elsewhere.test/"})

    with pytest.raises(TranscriptionProviderError) as excinfo:
        make_provider(handler).transcribe(audio_file)

    assert len(calls) == 1
    assert excinfo.value.status_code == 302
    assert not excinfo.value.retryable


# --- OpenAI-compatible: response mapping ----------------------------------


def test_response_is_mapped_into_a_transcription_result(audio_file):
    body = verbose_json(
        segments=[
            {"id": 0, "start": 0.0, "end": 2.5, "text": " Hello there."},
            {"id": 1, "start": 2.5, "end": 5.0, "text": " నమస్కారం friends"},
        ],
        words=[
            {"word": "Hello", "start": 0.1, "end": 0.6},
            {"word": "there.", "start": 0.7, "end": 1.2},
            {"word": "నమస్కారం", "start": 2.6, "end": 3.5},
            {"word": "friends", "start": 3.6, "end": 4.4},
        ],
        language="english",
    )

    result = make_provider(json_handler(body)).transcribe(audio_file)

    assert result.provider == "openai_compatible"
    assert result.model == MODEL
    assert result.language == "en"
    assert [(s.start, s.end, s.text) for s in result.segments] == [
        (0.0, 2.5, "Hello there."),
        (2.5, 5.0, "నమస్కారం friends"),
    ]
    assert [(w.word, w.start, w.end) for w in result.segments[0].words] == [
        ("Hello", 0.1, 0.6),
        ("there.", 0.7, 1.2),
    ]
    assert [w.word for w in result.segments[1].words] == ["నమస్కారం", "friends"]
    assert result.full_text == "Hello there. నమస్కారం friends"


def test_segment_level_words_are_used(audio_file):
    body = verbose_json(
        segments=[
            {
                "start": 1.0,
                "end": 2.0,
                "text": "hi",
                "words": [{"word": " hi", "start": 1.1, "end": 1.4}],
            }
        ]
    )

    result = make_provider(json_handler(body)).transcribe(audio_file)

    assert [(w.word, w.start, w.end) for w in result.segments[0].words] == [
        ("hi", 1.1, 1.4)
    ]


def test_segments_without_words_have_no_word_timings(audio_file):
    body = verbose_json(segments=[{"start": 0.0, "end": 1.0, "text": "hi"}])

    result = make_provider(json_handler(body)).transcribe(audio_file)

    assert result.segments[0].words is None


def test_requested_language_is_reported_as_given(audio_file):
    body = verbose_json(
        segments=[{"start": 0, "end": 1, "text": "x"}], language="hindi"
    )

    result = make_provider(json_handler(body)).transcribe(audio_file, language="hi-IN")

    assert result.language == "hi-IN"


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("telugu", "te"),
        ("Tamil", "ta"),
        ("javanese", "jv"),
        ("te", "te"),
        ("klingon", None),
    ],
)
def test_detected_language_is_mapped_to_an_iso_code(audio_file, reported, expected):
    body = verbose_json(
        segments=[{"start": 0, "end": 1, "text": "x"}], language=reported
    )

    assert make_provider(json_handler(body)).transcribe(audio_file).language == expected


def test_text_without_segments_becomes_one_segment(audio_file):
    body = {"text": "just text", "language": "en"}

    result = make_provider(
        json_handler(body), duration_probe=fixed_duration(12.0)
    ).transcribe(audio_file)

    assert [(s.start, s.end, s.text) for s in result.segments] == [
        (0.0, 12.0, "just text")
    ]


def test_empty_response_gives_zero_segments(audio_file):
    result = make_provider(json_handler({"text": ""})).transcribe(audio_file)

    assert result.segments == ()


def test_small_overshoot_is_clamped_to_the_end_of_the_audio(audio_file):
    body = verbose_json(
        segments=[{"start": 29.0, "end": 30.4, "text": "end"}],
        words=[{"word": "end", "start": 29.5, "end": 30.4}],
    )

    result = make_provider(json_handler(body)).transcribe(audio_file)

    assert result.segments[0].end == 30.0
    assert result.segments[0].words[0].end == 30.0


def test_unordered_segments_are_sorted(audio_file):
    body = verbose_json(
        segments=[
            {"start": 5.0, "end": 6.0, "text": "b"},
            {"start": 1.0, "end": 2.0, "text": "a"},
        ]
    )

    result = make_provider(json_handler(body)).transcribe(audio_file)

    assert [s.text for s in result.segments] == ["a", "b"]


# --- OpenAI-compatible: malformed responses -------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"[]",
        b'{"segments": {}}',
        b'{"segments": ["x"]}',
        b'{"segments": [{"start": "0", "end": 1, "text": "x"}]}',
        b'{"segments": [{"start": 0, "end": 1, "text": 5}]}',
        b'{"segments": [{"start": -1, "end": 1, "text": "x"}]}',
        b'{"segments": [{"start": NaN, "end": 1, "text": "x"}]}',
        b'{"segments": [{"start": 0, "end": Infinity, "text": "x"}]}',
        b'{"segments": [{"start": true, "end": 1, "text": "x"}]}',
        b'{"segments": [{"start": 3, "end": 2, "text": "x"}]}',
        b'{"segments": [{"start": 0, "end": 45, "text": "past the end"}]}',
        b'{"segments": [{"start": 0, "end": 1, "text": "x"}], "words": "x"}',
        b'{"segments": [{"start": 0, "end": 1, "text": "x"}],'
        b' "words": [{"word": "x", "start": 0.8, "end": 0.2}]}',
        b'{"segments": [{"start": 0, "end": 1, "text": "x"}],'
        b' "words": [{"word": 1, "start": 0, "end": 1}]}',
        b'{"text": 5}',
    ],
)
def test_malformed_responses_are_rejected(audio_file, content):
    def handler(request):
        return httpx.Response(200, content=content)

    with pytest.raises(InvalidTranscriptionResultError):
        make_provider(handler).transcribe(audio_file)
