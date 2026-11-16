"""Deterministic transcription provider for development and tests.

It never calls an external service. The audio is only inspected for its
duration, and the same file always gives the same transcript: segments of
SEGMENT_SECONDS (the last one shorter) covering the whole duration, with
placeholder text and evenly spaced word timings.
"""

import logging
import math
import wave
from collections.abc import Callable, Sequence
from pathlib import Path

from app.transcription.base import (
    TranscriptionError,
    TranscriptionResult,
    TranscriptionSegment,
    WordTiming,
    normalize_language,
)

logger = logging.getLogger(__name__)

FAKE_PROVIDER_NAME = "fake"
FAKE_MODEL_NAME = "fake-v1"
SEGMENT_SECONDS = 5.0
# Reported when no language is requested.
DEFAULT_DETECTED_LANGUAGE = "en"
# Used for non-WAV files: the size of 16 kHz, 16-bit mono PCM audio.
FALLBACK_BYTES_PER_SECOND = 16_000 * 2

DEFAULT_SEGMENT_TEXTS = (
    "This is a fake transcript segment.",
    "The fake provider returns deterministic text.",
    "Every segment has evenly spaced word timings.",
)


def probe_duration_seconds(audio_path: Path) -> float:
    """Return the duration of a WAV file, or an estimate for other files.

    Files the standard library cannot read as WAV are treated as 16 kHz,
    16-bit mono PCM, so the estimate depends only on the file size.
    """
    try:
        with wave.open(str(audio_path), "rb") as audio:
            frame_rate = audio.getframerate()
            if frame_rate > 0:
                return audio.getnframes() / frame_rate
    except (wave.Error, EOFError):
        pass
    return audio_path.stat().st_size / FALLBACK_BYTES_PER_SECOND


class FakeTranscriptionProvider:
    """A TranscriptionProvider that returns deterministic placeholder segments."""

    name = FAKE_PROVIDER_NAME

    def __init__(
        self,
        *,
        segment_seconds: float = SEGMENT_SECONDS,
        segment_texts: Sequence[str] = DEFAULT_SEGMENT_TEXTS,
        detected_language: str | None = DEFAULT_DETECTED_LANGUAGE,
        duration_probe: Callable[[Path], float] = probe_duration_seconds,
    ) -> None:
        if not math.isfinite(segment_seconds) or segment_seconds < 0.001:
            raise ValueError("segment_seconds must be at least 0.001.")
        if not segment_texts:
            raise ValueError("segment_texts must not be empty.")
        self._segment_ms = round(segment_seconds * 1000)
        self._segment_texts = tuple(segment_texts)
        self._detected_language = normalize_language(detected_language)
        self._duration_probe = duration_probe

    def transcribe(
        self, audio_path: str | Path, language: str | None = None
    ) -> TranscriptionResult:
        # Validate the request before touching the file.
        requested = normalize_language(language)
        path = Path(audio_path)
        if not path.is_file():
            raise TranscriptionError("The audio file does not exist.")
        try:
            duration = float(self._duration_probe(path))
        except OSError as exc:
            raise TranscriptionError(
                f"The audio file could not be read ({type(exc).__name__})."
            ) from None
        if not math.isfinite(duration) or duration < 0:
            raise TranscriptionError("The audio duration is invalid.")

        result = TranscriptionResult(
            # An explicit request is honored as given; otherwise the fake
            # "detects" its configured language.
            language=requested if requested is not None else self._detected_language,
            segments=tuple(self._segments(duration)),
            provider=self.name,
            model=FAKE_MODEL_NAME,
        )
        result.check_within_duration(duration)
        logger.info(
            "transcription_completed",
            extra={
                "provider": self.name,
                "language": result.language,
                "segment_count": len(result.segments),
            },
        )
        return result

    def _segments(self, duration: float) -> list[TranscriptionSegment]:
        # Work in whole milliseconds, rounded down so nothing passes the end.
        total_ms = math.floor(duration * 1000)
        if total_ms / 1000 > duration:
            # duration * 1000 was rounded up to a whole number.
            total_ms -= 1
        segments = []
        for index, start_ms in enumerate(range(0, total_ms, self._segment_ms)):
            end_ms = min(start_ms + self._segment_ms, total_ms)
            text = self._segment_texts[index % len(self._segment_texts)]
            segments.append(
                TranscriptionSegment(
                    start=start_ms / 1000,
                    end=end_ms / 1000,
                    text=text,
                    words=_spread_words(text, start_ms, end_ms),
                )
            )
        return segments


def _spread_words(text: str, start_ms: int, end_ms: int) -> tuple[WordTiming, ...]:
    words = text.split()
    if not words:
        return ()
    step = (end_ms - start_ms) / len(words)
    return tuple(
        WordTiming(
            word=word,
            start=round(start_ms + i * step) / 1000,
            end=(round(start_ms + (i + 1) * step) if i < len(words) - 1 else end_ms)
            / 1000,
        )
        for i, word in enumerate(words)
    )
