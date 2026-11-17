"""Shared helpers for the transcription provider tests."""

import httpx

from app.core.config import Settings
from app.transcription import OpenAICompatibleTranscriptionProvider

BASE_URL = "https://transcription.example.test/v1"
ENDPOINT = BASE_URL + "/audio/transcriptions"
# A test-only value; never a real credential.
API_KEY = "test-only-api-key-0123456789"
MODEL = "test-whisper-model"
PRODUCTION_SECRET = "test-only-production-secret-0123456789abcdef"
PRIVATE_TEXT = "PRIVATE-TRANSCRIPT-WORDS"


def fixed_duration(seconds):
    return lambda _path: seconds


def no_chunks(*_args):
    raise AssertionError("the audio must not be split")


def verbose_json(segments=(), words=None, language="english", text=None):
    body = {
        "task": "transcribe",
        "language": language,
        "duration": 30.0,
        "text": text if text is not None else " ".join(s["text"] for s in segments),
        "segments": list(segments),
    }
    if words is not None:
        body["words"] = words
    return body


def json_handler(body, status=200, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, json=body)

    return handler


def make_provider(handler, **kwargs):
    options = {
        "base_url": BASE_URL,
        "model": MODEL,
        "api_key": API_KEY,
        "transport": httpx.MockTransport(handler),
        "duration_probe": fixed_duration(30.0),
        "chunk_writer": no_chunks,
    }
    options.update(kwargs)
    return OpenAICompatibleTranscriptionProvider(**options)


def provider_settings(**overrides):
    values = {
        "TRANSCRIPTION_PROVIDER": "openai_compatible",
        "TRANSCRIPTION_API_BASE_URL": BASE_URL,
        "TRANSCRIPTION_API_KEY": API_KEY,
        "TRANSCRIPTION_MODEL": MODEL,
    }
    values.update(overrides)
    return Settings(**values)
