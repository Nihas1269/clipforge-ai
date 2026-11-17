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
    TranscriptionProviderError,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionTimeoutError,
    WordTiming,
    normalize_language,
)
from app.transcription.fake import FakeTranscriptionProvider
from app.transcription.local_whisper import LocalWhisperTranscriptionProvider
from app.transcription.openai_compatible import OpenAICompatibleTranscriptionProvider

__all__ = [
    "AUTO_LANGUAGE",
    "UNDETERMINED_LANGUAGE",
    "FakeTranscriptionProvider",
    "InvalidLanguageError",
    "InvalidTranscriptionResultError",
    "LocalWhisperTranscriptionProvider",
    "OpenAICompatibleTranscriptionProvider",
    "TranscriptionConfigurationError",
    "TranscriptionError",
    "TranscriptionProvider",
    "TranscriptionProviderError",
    "TranscriptionResult",
    "TranscriptionSegment",
    "TranscriptionTimeoutError",
    "WordTiming",
    "get_transcription_provider",
    "normalize_language",
]

_PROVIDERS: dict[str, Callable[[Settings], TranscriptionProvider]] = {
    "fake": lambda settings: FakeTranscriptionProvider(),
    "openai_compatible": OpenAICompatibleTranscriptionProvider.from_settings,
    # Imports faster-whisper only here, and fails clearly if it is missing.
    "local_whisper": LocalWhisperTranscriptionProvider.from_settings,
}


def get_transcription_provider(
    settings: Settings | None = None,
) -> TranscriptionProvider:
    """Return the transcription provider selected by TRANSCRIPTION_PROVIDER.

    Raises TranscriptionConfigurationError for an unknown provider name, for
    the fake provider when APP_ENV is production (it would store placeholder
    text as a real transcript), when a real provider's required settings are
    missing or invalid, and for local_whisper when faster-whisper is not
    installed.
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
