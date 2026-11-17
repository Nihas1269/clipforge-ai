"""Local speech-to-text with faster-whisper (optional).

faster-whisper and its models are large, so the package is not part of
requirements.txt; install backend/requirements-ml.txt to use this provider.
The package is imported only when the provider is created, and a clear
TranscriptionConfigurationError is raised if it is missing. The model
(TRANSCRIPTION_MODEL: a size such as "small" or a local model directory) is
loaded on first use and reused. Audio never leaves the machine, but the
first load may download the model files from the Hugging Face Hub.
"""

import importlib
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from app.core.config import Settings
from app.transcription.base import (
    UNDETERMINED_LANGUAGE,
    InvalidTranscriptionResultError,
    TranscriptionConfigurationError,
    TranscriptionError,
    TranscriptionResult,
    normalize_language,
)
from app.transcription.mapping import (
    RawSegment,
    RawWord,
    language_from_provider,
    place_segments,
    provider_time,
)

logger = logging.getLogger(__name__)

LOCAL_WHISPER_PROVIDER_NAME = "local_whisper"
MAX_MODEL_NAME_LENGTH = 500


def import_faster_whisper() -> ModuleType:
    """Import faster_whisper or raise a TranscriptionConfigurationError saying how to install it."""
    try:
        return importlib.import_module("faster_whisper")
    except ImportError:
        raise TranscriptionConfigurationError(
            f"TRANSCRIPTION_PROVIDER={LOCAL_WHISPER_PROVIDER_NAME!r} needs the optional "
            "faster-whisper package, which is not installed. Install it from the "
            "backend/ directory with: pip install -r requirements-ml.txt"
        ) from None


def _raw_segment(segment: Any) -> RawSegment:
    text = getattr(segment, "text", None)
    if not isinstance(text, str):
        raise InvalidTranscriptionResultError("Whisper returned a malformed segment.")
    words = getattr(segment, "words", None)
    raw_words = None
    if words is not None:
        raw_words = []
        for word in words:
            value = getattr(word, "word", None)
            if not isinstance(value, str):
                raise InvalidTranscriptionResultError(
                    "Whisper returned a malformed word."
                )
            raw_words.append(
                RawWord(
                    value,
                    provider_time(getattr(word, "start", None), "word start"),
                    provider_time(getattr(word, "end", None), "word end"),
                )
            )
    return RawSegment(
        start=provider_time(getattr(segment, "start", None), "segment start"),
        end=provider_time(getattr(segment, "end", None), "segment end"),
        text=text,
        words=tuple(raw_words) if raw_words is not None else None,
    )


class LocalWhisperTranscriptionProvider:
    """A TranscriptionProvider running faster-whisper in the worker process."""

    name = LOCAL_WHISPER_PROVIDER_NAME

    def __init__(
        self,
        *,
        model: str,
        device: str = "auto",
        compute_type: str = "default",
        model_loader: Callable[[], Any] | None = None,
    ) -> None:
        if (
            not isinstance(model, str)
            or not model.strip()
            or len(model) > MAX_MODEL_NAME_LENGTH
            or not model.isprintable()
        ):
            raise TranscriptionConfigurationError(
                "TRANSCRIPTION_MODEL must be a Whisper model size or a model directory."
            )
        self.model = model.strip()
        if model_loader is None:
            module = import_faster_whisper()
            model_name = self.model

            def load_model() -> Any:
                return module.WhisperModel(
                    model_name, device=device, compute_type=compute_type
                )

            model_loader = load_model
        self._model_loader = model_loader
        self._whisper: Any = None
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "LocalWhisperTranscriptionProvider":
        """Create the provider from settings; raise a clear error if any are missing."""
        if not settings.TRANSCRIPTION_MODEL:
            raise TranscriptionConfigurationError(
                f"TRANSCRIPTION_PROVIDER={LOCAL_WHISPER_PROVIDER_NAME!r} needs these "
                "settings: TRANSCRIPTION_MODEL (for example 'small'). "
                "See docs/setup/transcription.md."
            )
        return cls(model=settings.TRANSCRIPTION_MODEL)

    def _loaded_model(self) -> Any:
        with self._lock:
            if self._whisper is None:
                try:
                    self._whisper = self._model_loader()
                except Exception as exc:
                    raise TranscriptionConfigurationError(
                        f"The Whisper model could not be loaded ({type(exc).__name__})."
                    ) from None
            return self._whisper

    def transcribe(
        self, audio_path: str | Path, language: str | None = None
    ) -> TranscriptionResult:
        # Validate the request before touching the file.
        requested = normalize_language(language)
        path = Path(audio_path)
        if not path.is_file():
            raise TranscriptionError("The audio file does not exist.")
        whisper = self._loaded_model()
        whisper_language = None
        if requested is not None and requested != UNDETERMINED_LANGUAGE:
            # Whisper takes ISO 639-1 codes, so only the primary subtag is passed.
            whisper_language = requested.split("-")[0]

        started = time.monotonic()
        try:
            segments, info = whisper.transcribe(
                str(path), language=whisper_language, word_timestamps=True
            )
            # The segments are a generator; decoding happens while iterating.
            raw_segments = [_raw_segment(segment) for segment in segments]
            duration = getattr(info, "duration", None)
            detected = language_from_provider(getattr(info, "language", None))
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"Local Whisper transcription failed ({type(exc).__name__})."
            ) from None
        duration = provider_time(duration, "duration")

        result_language: str | None
        if requested is not None and requested != UNDETERMINED_LANGUAGE:
            result_language = requested
        else:
            result_language = detected or requested
        result = TranscriptionResult(
            language=result_language,
            segments=tuple(place_segments(raw_segments, offset=0.0, duration=duration)),
            provider=self.name,
            model=self.model,
        )
        result.check_within_duration(duration)
        logger.info(
            "transcription_completed",
            extra={
                "provider": self.name,
                "model": self.model,
                "language": result.language,
                "segment_count": len(result.segments),
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return result
