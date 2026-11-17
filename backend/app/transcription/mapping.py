"""Turning raw provider output into validated TranscriptionResult values.

Real providers report times relative to the audio they were sent. When a long
file is sent as consecutive chunks, place_segments() checks each chunk's
times against that chunk and shifts them by the chunk's start (its offset)
exactly once, so timestamps are neither lost nor shifted twice.
"""

import math
from bisect import bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.transcription.base import (
    InvalidLanguageError,
    InvalidTranscriptionResultError,
    TranscriptionSegment,
    WordTiming,
    normalize_language,
)

# Providers often report the last segment a little past the end of the audio
# they were sent (rounding, padding). An overshoot up to this many seconds is
# clamped to the end; a larger one makes the response invalid.
TIMESTAMP_TOLERANCE_SECONDS = 1.0

# Whisper-style providers report the detected language by name
# ("english"); results store ISO 639 codes. Javanese is "jw" in Whisper but
# "jv" in ISO 639-1.
WHISPER_LANGUAGE_CODES = {
    "afrikaans": "af",
    "albanian": "sq",
    "amharic": "am",
    "arabic": "ar",
    "armenian": "hy",
    "assamese": "as",
    "azerbaijani": "az",
    "bashkir": "ba",
    "basque": "eu",
    "belarusian": "be",
    "bengali": "bn",
    "bosnian": "bs",
    "breton": "br",
    "bulgarian": "bg",
    "cantonese": "yue",
    "catalan": "ca",
    "chinese": "zh",
    "croatian": "hr",
    "czech": "cs",
    "danish": "da",
    "dutch": "nl",
    "english": "en",
    "estonian": "et",
    "faroese": "fo",
    "finnish": "fi",
    "french": "fr",
    "galician": "gl",
    "georgian": "ka",
    "german": "de",
    "greek": "el",
    "gujarati": "gu",
    "haitian creole": "ht",
    "hausa": "ha",
    "hawaiian": "haw",
    "hebrew": "he",
    "hindi": "hi",
    "hungarian": "hu",
    "icelandic": "is",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "javanese": "jv",
    "kannada": "kn",
    "kazakh": "kk",
    "khmer": "km",
    "korean": "ko",
    "lao": "lo",
    "latin": "la",
    "latvian": "lv",
    "lingala": "ln",
    "lithuanian": "lt",
    "luxembourgish": "lb",
    "macedonian": "mk",
    "malagasy": "mg",
    "malay": "ms",
    "malayalam": "ml",
    "maltese": "mt",
    "maori": "mi",
    "marathi": "mr",
    "mongolian": "mn",
    "myanmar": "my",
    "nepali": "ne",
    "norwegian": "no",
    "nynorsk": "nn",
    "occitan": "oc",
    "pashto": "ps",
    "persian": "fa",
    "polish": "pl",
    "portuguese": "pt",
    "punjabi": "pa",
    "romanian": "ro",
    "russian": "ru",
    "sanskrit": "sa",
    "serbian": "sr",
    "shona": "sn",
    "sindhi": "sd",
    "sinhala": "si",
    "slovak": "sk",
    "slovenian": "sl",
    "somali": "so",
    "spanish": "es",
    "sundanese": "su",
    "swahili": "sw",
    "swedish": "sv",
    "tagalog": "tl",
    "tajik": "tg",
    "tamil": "ta",
    "tatar": "tt",
    "telugu": "te",
    "thai": "th",
    "tibetan": "bo",
    "turkish": "tr",
    "turkmen": "tk",
    "ukrainian": "uk",
    "urdu": "ur",
    "uzbek": "uz",
    "vietnamese": "vi",
    "welsh": "cy",
    "yiddish": "yi",
    "yoruba": "yo",
}


@dataclass(frozen=True)
class RawWord:
    """A word as reported by a provider, in seconds from the start of its audio."""

    word: str
    start: float
    end: float


@dataclass(frozen=True)
class RawSegment:
    """A segment as reported by a provider, before checking and offsetting."""

    start: float
    end: float
    text: str
    words: tuple[RawWord, ...] | None = None


def provider_time(value: object, name: str) -> float:
    """Return a provider timestamp as a float; raise if it is not a valid time."""
    # bool is an int subclass, but True is never a timestamp.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidTranscriptionResultError(
            f"The provider returned a non-numeric {name}."
        )
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise InvalidTranscriptionResultError(
            f"The provider returned an invalid {name}."
        )
    return value


def language_from_provider(value: object) -> str | None:
    """Return the ISO 639 code for a language reported by a provider, or None.

    Accepts codes ("en", "hi-IN") and Whisper language names ("english").
    Anything else is reported as unknown rather than guessed.
    """
    if not isinstance(value, str):
        return None
    name = value.strip().lower()
    if name in WHISPER_LANGUAGE_CODES:
        return WHISPER_LANGUAGE_CODES[name]
    try:
        return normalize_language(value)
    except InvalidLanguageError:
        return None


def assign_words(
    segments: Sequence[RawSegment],
    words: Iterable[RawWord],
    tolerance: float = TIMESTAMP_TOLERANCE_SECONDS,
) -> list[RawSegment]:
    """Attach transcript-level words to the segment containing their midpoint.

    Returns the segments in chronological order, each with its words. A word
    whose midpoint is more than `tolerance` seconds outside every segment is
    dropped.
    """
    ordered = sorted(segments, key=lambda segment: (segment.start, segment.end))
    if not ordered:
        return []
    starts = [segment.start for segment in ordered]
    buckets: list[list[RawWord]] = [[] for _ in ordered]
    for word in words:
        middle = (word.start + word.end) / 2
        index = max(bisect_right(starts, middle) - 1, 0)
        segment = ordered[index]
        if segment.start - tolerance <= middle <= segment.end + tolerance:
            buckets[index].append(word)
    return [
        RawSegment(segment.start, segment.end, segment.text, tuple(bucket))
        for segment, bucket in zip(ordered, buckets)
    ]


def place_segments(
    raw_segments: Iterable[RawSegment],
    *,
    offset: float,
    duration: float,
    end_limit: float | None = None,
    tolerance: float = TIMESTAMP_TOLERANCE_SECONDS,
) -> list[TranscriptionSegment]:
    """Return the segments of one piece of audio on the timeline of the whole file.

    `raw_segments` use times relative to the piece, which starts `offset`
    seconds into the file and lasts `duration` seconds. Times are checked
    against the piece (an overshoot up to `tolerance` is clamped, a larger one
    raises InvalidTranscriptionResultError), then shifted by `offset` exactly
    once and capped at `end_limit` (default offset + duration) so float
    rounding never pushes them past the end of the file. Segments that are
    empty after clamping are dropped; words are clamped into their segment.
    The segments are returned in chronological order.
    """
    if end_limit is None:
        end_limit = offset + duration
    placed = []
    for raw in sorted(raw_segments, key=lambda segment: (segment.start, segment.end)):
        if raw.end < raw.start:
            raise InvalidTranscriptionResultError(
                "The provider returned a segment that ends before it starts."
            )
        if raw.end > duration + tolerance:
            raise InvalidTranscriptionResultError(
                "The provider returned a timestamp past the end of the audio."
            )
        start = min(offset + min(raw.start, duration), end_limit)
        end = min(offset + min(raw.end, duration), end_limit)
        if end <= start:
            continue
        words: tuple[WordTiming, ...] | None = None
        if raw.words is not None:
            timings: list[WordTiming] = []
            for word in raw.words:
                if word.end < word.start:
                    raise InvalidTranscriptionResultError(
                        "The provider returned a word that ends before it starts."
                    )
                if word.end > duration + tolerance:
                    raise InvalidTranscriptionResultError(
                        "The provider returned a timestamp past the end of the audio."
                    )
                text = word.word.strip()
                if not text:
                    continue
                word_start = min(max(offset + word.start, start), end)
                word_end = min(max(offset + word.end, start), end)
                timings.append(WordTiming(text, word_start, word_end))
            words = tuple(sorted(timings, key=lambda word: (word.start, word.end)))
        placed.append(TranscriptionSegment(start, end, raw.text.strip(), words))
    return placed
