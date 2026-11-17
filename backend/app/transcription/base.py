"""Transcription provider interface and result types.

A provider turns an audio file into a TranscriptionResult: the language and a
list of timed segments, each with optional word timings. Results are
validated when they are created, so a provider can never hand back segments
with negative, inverted or non-finite timestamps.
"""

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

# BCP 47 allows longer tags, but 35 characters covers every tag in practical
# use (RFC 5646 section 4.4.1).
LANGUAGE_MAX_LENGTH = 35
# "auto" (and a missing value) asks the provider to detect the language.
AUTO_LANGUAGE = "auto"
# ISO 639-2 "undetermined": an explicit "the language is unknown".
UNDETERMINED_LANGUAGE = "und"

# A primary ISO 639 code (2 or 3 letters) followed by optional subtags such as
# a script or region ("en", "te", "hi-IN", "zh-Hant-TW").
_LANGUAGE_PATTERN = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*")


class TranscriptionError(Exception):
    """Transcribing an audio file failed."""


class TranscriptionConfigurationError(TranscriptionError):
    """The configured transcription provider is unknown or not usable."""


class InvalidTranscriptionResultError(TranscriptionError, ValueError):
    """A transcription result violates the result schema."""


class TranscriptionProviderError(TranscriptionError):
    """A remote provider rejected the request, failed, or could not be reached.

    `status_code` is the HTTP status, if a response was received. `retryable`
    is True for failures that may succeed later (rate limits, server errors,
    network problems, timeouts); `retry_after` is the provider's suggested
    wait in seconds, if it sent one. Messages never include the response body.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


class TranscriptionTimeoutError(TranscriptionProviderError):
    """The provider did not answer within the configured timeout."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


class InvalidLanguageError(ValueError):
    """A language value is not an ISO 639 code (BCP 47 tag) or "auto"."""


def normalize_language(value: str | None) -> str | None:
    """Return the language tag to use, or None for automatic detection.

    Accepts None, "" and "auto" (any case) as "detect automatically", and
    ISO 639 codes with optional BCP 47 subtags ("en", "te", "hi-IN"). "und"
    is kept as an explicit "unknown". Only surrounding whitespace is removed
    and the primary code lowercased; the tag is never mapped to another
    language. Raises InvalidLanguageError for anything else.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidLanguageError("Language must be a string.")
    value = value.strip()
    if not value or value.lower() == AUTO_LANGUAGE:
        return None
    if len(value) > LANGUAGE_MAX_LENGTH or not _LANGUAGE_PATTERN.fullmatch(value):
        raise InvalidLanguageError(
            "Language must be an ISO 639 code such as 'en' or 'hi-IN', or 'auto'."
        )
    primary, separator, rest = value.partition("-")
    return f"{primary.lower()}{separator}{rest}"


def _check_time(value: object, name: str) -> float:
    # bool is an int subclass, but True is never a timestamp.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidTranscriptionResultError(f"{name} must be a number.")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise InvalidTranscriptionResultError(
            f"{name} must be a finite, non-negative number."
        )
    return value


@dataclass(frozen=True)
class WordTiming:
    """One word and the time span (in seconds) it is spoken."""

    word: str
    start: float
    end: float

    def __post_init__(self) -> None:
        if not isinstance(self.word, str) or not self.word.strip():
            raise InvalidTranscriptionResultError("A word must be a non-empty string.")
        start = _check_time(self.start, "Word start")
        end = _check_time(self.end, "Word end")
        # A word may be reported with zero length, but never end before it starts.
        if end < start:
            raise InvalidTranscriptionResultError(
                "A word must not end before it starts."
            )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def to_dict(self) -> dict[str, object]:
        return {"word": self.word, "start": self.start, "end": self.end}


@dataclass(frozen=True)
class TranscriptionSegment:
    """A span of speech: start < end, in seconds from the start of the audio."""

    start: float
    end: float
    text: str
    # None when the provider does not report word timings.
    words: tuple[WordTiming, ...] | None = None

    def __post_init__(self) -> None:
        start = _check_time(self.start, "Segment start")
        end = _check_time(self.end, "Segment end")
        if end <= start:
            raise InvalidTranscriptionResultError("A segment must end after it starts.")
        if not isinstance(self.text, str):
            raise InvalidTranscriptionResultError("Segment text must be a string.")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        if self.words is not None:
            words = tuple(self.words)
            if not all(isinstance(word, WordTiming) for word in words):
                raise InvalidTranscriptionResultError(
                    "Segment words must be WordTiming values."
                )
            object.__setattr__(self, "words", words)


@dataclass(frozen=True)
class TranscriptionResult:
    """The output of a transcription provider.

    `language` is the language the provider reports (or the one it was asked
    to use), normalized with normalize_language(); None means unknown.
    `provider` and `model` record what produced the transcript. Segments must
    be in chronological order (each starts no earlier than the previous one).
    """

    language: str | None
    segments: tuple[TranscriptionSegment, ...]
    provider: str
    model: str | None = None

    def __post_init__(self) -> None:
        try:
            language = normalize_language(self.language)
        except InvalidLanguageError as exc:
            raise InvalidTranscriptionResultError(str(exc)) from None
        object.__setattr__(self, "language", language)
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise InvalidTranscriptionResultError("The provider name must be set.")
        segments = tuple(self.segments)
        if not all(isinstance(segment, TranscriptionSegment) for segment in segments):
            raise InvalidTranscriptionResultError(
                "Segments must be TranscriptionSegment values."
            )
        for previous, current in zip(segments, segments[1:]):
            if current.start < previous.start:
                raise InvalidTranscriptionResultError(
                    "Segments must be in chronological order."
                )
        object.__setattr__(self, "segments", segments)

    @property
    def full_text(self) -> str:
        """The segment texts joined with single spaces (empty segments skipped)."""
        return " ".join(
            segment.text.strip() for segment in self.segments if segment.text.strip()
        )

    def check_within_duration(self, duration_seconds: float) -> None:
        """Raise InvalidTranscriptionResultError if any timestamp is past the end."""
        duration = _check_time(duration_seconds, "Duration")
        for segment in self.segments:
            if segment.end > duration:
                raise InvalidTranscriptionResultError(
                    "A segment ends after the end of the audio."
                )
            for word in segment.words or ():
                if word.end > duration:
                    raise InvalidTranscriptionResultError(
                        "A word ends after the end of the audio."
                    )


@runtime_checkable
class TranscriptionProvider(Protocol):
    """Turns an audio file into a TranscriptionResult.

    `language` is an ISO 639 code (see normalize_language) or None to let the
    provider detect it. Implementations raise TranscriptionError on failure
    and never return a partial result as success.
    """

    name: str

    def transcribe(
        self, audio_path: str | Path, language: str | None = None
    ) -> TranscriptionResult: ...
