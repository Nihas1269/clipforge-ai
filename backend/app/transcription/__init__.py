"""Speech-to-text transcription.

Use get_transcription_provider() to obtain the provider selected by
Settings.TRANSCRIPTION_PROVIDER.
"""

from collections.abc import Callable

from app.core.config import Settings, get_settings
from app.transcription.base import (
    AUTO_LANGUAGE,
    UNDETERMINED_LANGUAGE,
    InvalidLanguageError,
    InvalidTranscriptionResultError,
    TranscriptionConfigurationError,
    TranscriptionError,
    TranscriptionProvider,
    TranscriptionResult,
    TranscriptionSegment,
    WordTiming,
    normalize_language,
)
from app.transcription.fake import FakeTranscriptionProvider

__all__ = [
    "AUTO_LANGUAGE",
    "UNDETERMINED_LANGUAGE",
    "FakeTranscriptionProvider",
    "InvalidLanguageError",
    "InvalidTranscriptionResultError",
    "TranscriptionConfigurationError",
    "TranscriptionError",
    "TranscriptionProvider",
    "TranscriptionResult",
    "TranscriptionSegment",
    "WordTiming",
    "get_transcription_provider",
    "normalize_language",
]

_PROVIDERS: dict[str, Callable[[Settings], TranscriptionProvider]] = {
    "fake": lambda settings: FakeTranscriptionProvider(),
}


def get_transcription_provider(settings: Settings | None = None) -> TranscriptionProvider:
    """Return the transcription provider selected by TRANSCRIPTION_PROVIDER.

    Raises TranscriptionConfigurationError for an unknown provider name, and
    for the fake provider when APP_ENV is production (it would store
    placeholder text as a real transcript).
    """
    if settings is None:
        settings = get_settings()
    name = settings.TRANSCRIPTION_PROVIDER
    factory = _PROVIDERS.get(name)
    if factory is None:
        supported = ", ".join(repr(provider) for provider in sorted(_PROVIDERS))
        raise TranscriptionConfigurationError(
            f"TRANSCRIPTION_PROVIDER={name!r} is not supported; use one of: {supported}."
        )
    if name == "fake" and settings.APP_ENV == "production":
        raise TranscriptionConfigurationError(
            "TRANSCRIPTION_PROVIDER='fake' returns placeholder text and is not "
            "allowed when APP_ENV is 'production'."
        )
    return factory(settings)
