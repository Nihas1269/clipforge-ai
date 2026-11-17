import json
import math
import sys
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.transcription import (
    InvalidTranscriptionResultError,
    LocalWhisperTranscriptionProvider,
    TranscriptionConfigurationError,
    TranscriptionError,
    get_transcription_provider,
)
from tests.transcription_support import make_provider, verbose_json

# --- Local Whisper --------------------------------------------------------


class FakeWhisperModel:
    def __init__(self, segments, language="te", duration=10.0, error=None):
        self.segments = segments
        self.info = SimpleNamespace(language=language, duration=duration)
        self.error = error
        self.calls = []

    def transcribe(self, path, language=None, word_timestamps=False):
        self.calls.append(
            {"path": path, "language": language, "words": word_timestamps}
        )
        if self.error is not None:
            raise self.error

        def generate():
            yield from self.segments

        return generate(), self.info


def whisper_segment(start, end, text, words=None):
    return SimpleNamespace(
        start=start,
        end=end,
        text=text,
        words=[
            SimpleNamespace(word=w, start=s, end=e, probability=0.9)
            for w, s, e in words
        ]
        if words is not None
        else None,
    )


def test_local_whisper_fails_clearly_without_faster_whisper(monkeypatch):
    # A None entry makes the import fail whether or not the package is installed.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(TranscriptionConfigurationError) as excinfo:
        LocalWhisperTranscriptionProvider(model="small")
    assert "faster-whisper" in str(excinfo.value)
    assert "requirements-ml.txt" in str(excinfo.value)

    with pytest.raises(TranscriptionConfigurationError):
        get_transcription_provider(
            Settings(
                TRANSCRIPTION_PROVIDER="local_whisper", TRANSCRIPTION_MODEL="small"
            )
        )


def test_local_whisper_factory_requires_a_model(monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(TranscriptionConfigurationError) as excinfo:
        get_transcription_provider(
            Settings(TRANSCRIPTION_PROVIDER="local_whisper", TRANSCRIPTION_MODEL=None)
        )

    assert "TRANSCRIPTION_MODEL" in str(excinfo.value)


def test_local_whisper_factory_loads_the_model_lazily(monkeypatch, audio_file):
    created = []

    class WhisperModel(FakeWhisperModel):
        def __init__(self, name, device, compute_type):
            created.append((name, device, compute_type))
            super().__init__([whisper_segment(0.0, 1.0, "hi")])

    monkeypatch.setitem(
        sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=WhisperModel)
    )

    provider = get_transcription_provider(
        Settings(TRANSCRIPTION_PROVIDER="local_whisper", TRANSCRIPTION_MODEL="small")
    )

    assert isinstance(provider, LocalWhisperTranscriptionProvider)
    assert created == []
    provider.transcribe(audio_file)
    provider.transcribe(audio_file)
    assert created == [("small", "auto", "default")]


def test_local_whisper_maps_segments_and_words(audio_file):
    model = FakeWhisperModel(
        [
            whisper_segment(
                0.0, 2.0, " నేను office", [(" నేను", 0.0, 0.8), (" office", 0.9, 1.6)]
            ),
            whisper_segment(2.0, 10.3, " కి వెళ్తున్నాను", [(" కి", 2.1, 2.4)]),
        ],
        language="te",
        duration=10.0,
    )
    provider = LocalWhisperTranscriptionProvider(
        model="small", model_loader=lambda: model
    )

    result = provider.transcribe(audio_file)

    assert result.provider == "local_whisper"
    assert result.model == "small"
    assert result.language == "te"
    assert [(s.start, s.end, s.text) for s in result.segments] == [
        (0.0, 2.0, "నేను office"),
        (2.0, 10.0, "కి వెళ్తున్నాను"),
    ]
    assert [w.word for w in result.segments[0].words] == ["నేను", "office"]
    assert model.calls == [{"path": str(audio_file), "language": None, "words": True}]


def test_local_whisper_passes_the_primary_language_code(audio_file):
    model = FakeWhisperModel([whisper_segment(0.0, 1.0, "x")], language="hi")
    provider = LocalWhisperTranscriptionProvider(
        model="small", model_loader=lambda: model
    )

    result = provider.transcribe(audio_file, language="hi-IN")

    assert model.calls[0]["language"] == "hi"
    assert result.language == "hi-IN"


def test_local_whisper_errors_hide_details(audio_file):
    model = FakeWhisperModel([], error=RuntimeError("/secret/path exploded"))
    provider = LocalWhisperTranscriptionProvider(
        model="small", model_loader=lambda: model
    )

    with pytest.raises(TranscriptionError) as excinfo:
        provider.transcribe(audio_file)

    assert "/secret/path" not in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value)


def test_local_whisper_model_load_failure_is_a_configuration_error(audio_file):
    def loader():
        raise OSError("model files missing at /secret/path")

    provider = LocalWhisperTranscriptionProvider(model="small", model_loader=loader)

    with pytest.raises(TranscriptionConfigurationError) as excinfo:
        provider.transcribe(audio_file)
    assert "/secret/path" not in str(excinfo.value)


@pytest.mark.parametrize(
    "segments",
    [
        [whisper_segment(0.0, 20.0, "past the end")],
        [whisper_segment(3.0, 2.0, "inverted")],
        [whisper_segment(math.nan, 2.0, "nan")],
        [whisper_segment(0.0, 1.0, None)],
        [whisper_segment(0.0, 1.0, "x", [(None, 0.0, 1.0)])],
    ],
)
def test_local_whisper_rejects_invalid_output(audio_file, segments):
    model = FakeWhisperModel(segments, duration=10.0)
    provider = LocalWhisperTranscriptionProvider(
        model="small", model_loader=lambda: model
    )

    with pytest.raises(InvalidTranscriptionResultError):
        provider.transcribe(audio_file)


def test_local_whisper_missing_file_does_not_load_the_model(tmp_path):
    def loader():
        raise AssertionError("the model must not be loaded")

    provider = LocalWhisperTranscriptionProvider(model="small", model_loader=loader)

    with pytest.raises(TranscriptionError):
        provider.transcribe(tmp_path / "missing.wav")


def test_requirements_keep_faster_whisper_optional():
    from app.core.config import BACKEND_DIR

    base = (BACKEND_DIR / "requirements.txt").read_text(encoding="utf-8").lower()
    ml = (BACKEND_DIR / "requirements-ml.txt").read_text(encoding="utf-8").lower()

    assert "faster-whisper" not in base
    assert "faster-whisper" in ml


def test_no_test_needs_network(audio_file):
    # The MockTransport answers every request; nothing reaches a real host.
    body = json.dumps(verbose_json()).encode()

    def handler(request):
        assert request.url.host == "transcription.example.test"
        return httpx.Response(200, content=body)

    make_provider(handler).transcribe(audio_file)
