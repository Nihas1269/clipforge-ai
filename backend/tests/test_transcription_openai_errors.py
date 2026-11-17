import logging
import math

import httpx
import pytest

from app.media.ffmpeg import MediaError
from app.transcription import (
    TranscriptionError,
    TranscriptionProviderError,
    TranscriptionTimeoutError,
)
from tests.transcription_support import (
    API_KEY,
    MODEL,
    PRIVATE_TEXT,
    fixed_duration,
    json_handler,
    make_provider,
    verbose_json,
)

# --- OpenAI-compatible: provider errors -----------------------------------


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (401, False),
        (403, False),
        (400, False),
        (413, False),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_http_errors_raise_provider_errors(audio_file, status, retryable):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": PRIVATE_TEXT}})

    with pytest.raises(TranscriptionProviderError) as excinfo:
        make_provider(handler).transcribe(audio_file)

    error = excinfo.value
    assert error.status_code == status
    assert error.retryable is retryable
    assert str(status) in str(error)
    # Neither the response body nor the key is ever repeated.
    assert PRIVATE_TEXT not in str(error)
    assert API_KEY not in str(error)


def test_401_names_the_api_key_setting(audio_file):
    with pytest.raises(TranscriptionProviderError) as excinfo:
        make_provider(json_handler({}, status=401)).transcribe(audio_file)

    assert "TRANSCRIPTION_API_KEY" in str(excinfo.value)


def test_429_reports_retry_after(audio_file):
    def handler(request):
        return httpx.Response(429, headers={"Retry-After": "7"})

    with pytest.raises(TranscriptionProviderError) as excinfo:
        make_provider(handler).transcribe(audio_file)

    assert excinfo.value.retry_after == 7.0


def test_timeout_raises_retryable_timeout_error(audio_file):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(TranscriptionTimeoutError) as excinfo:
        make_provider(handler).transcribe(audio_file)

    assert excinfo.value.retryable


def test_connection_error_is_retryable(audio_file):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TranscriptionProviderError) as excinfo:
        make_provider(handler).transcribe(audio_file)

    assert excinfo.value.retryable
    assert not isinstance(excinfo.value, TranscriptionTimeoutError)


def test_errors_are_logged_without_secrets_or_text(audio_file, caplog):
    def handler(request):
        return httpx.Response(500, text=PRIVATE_TEXT)

    with caplog.at_level(logging.INFO, logger="app.transcription"):
        with pytest.raises(TranscriptionProviderError):
            make_provider(handler).transcribe(audio_file)

    (record,) = [
        r for r in caplog.records if r.getMessage() == "transcription_request_failed"
    ]
    assert record.status_code == 500
    assert record.retryable is True
    for logged in caplog.records:
        assert API_KEY not in repr(logged.__dict__)
        assert PRIVATE_TEXT not in repr(logged.__dict__)


def test_success_is_logged_without_secrets_or_text(audio_file, caplog):
    body = verbose_json(segments=[{"start": 0, "end": 1, "text": PRIVATE_TEXT}])

    with caplog.at_level(logging.INFO, logger="app.transcription"):
        make_provider(json_handler(body)).transcribe(audio_file)

    (record,) = [
        r for r in caplog.records if r.getMessage() == "transcription_completed"
    ]
    assert record.provider == "openai_compatible"
    assert record.model == MODEL
    assert record.segment_count == 1
    for logged in caplog.records:
        assert API_KEY not in repr(logged.__dict__)
        assert PRIVATE_TEXT not in repr(logged.__dict__)


# --- OpenAI-compatible: input checks --------------------------------------


def test_missing_file_fails_without_a_request(tmp_path):
    calls = []
    provider = make_provider(json_handler(verbose_json(), calls=calls))

    with pytest.raises(TranscriptionError):
        provider.transcribe(tmp_path / "missing.wav")
    with pytest.raises(TranscriptionError):
        provider.transcribe(tmp_path)
    assert calls == []


@pytest.mark.parametrize("duration", [None, -1.0, math.nan, math.inf, "10"])
def test_invalid_duration_fails(audio_file, duration):
    provider = make_provider(
        json_handler(verbose_json()), duration_probe=fixed_duration(duration)
    )

    with pytest.raises(TranscriptionError):
        provider.transcribe(audio_file)


def test_unreadable_audio_fails_without_details(audio_file):
    def probe(_path):
        raise MediaError("ffprobe failed", "/secret/path: invalid data")

    with pytest.raises(TranscriptionError) as excinfo:
        make_provider(json_handler(verbose_json()), duration_probe=probe).transcribe(
            audio_file
        )

    assert "/secret/path" not in str(excinfo.value)


def test_silent_audio_makes_no_request(audio_file):
    calls = []
    provider = make_provider(
        json_handler(verbose_json(), calls=calls), duration_probe=fixed_duration(0.0)
    )

    assert provider.transcribe(audio_file).segments == ()
    assert calls == []
