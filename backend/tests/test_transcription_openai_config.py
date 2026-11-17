import pytest

from app.core.config import Settings
from app.transcription import (
    OpenAICompatibleTranscriptionProvider,
    TranscriptionConfigurationError,
    get_transcription_provider,
)
from app.transcription.mapping import language_from_provider
from tests.transcription_support import (
    API_KEY,
    BASE_URL,
    MODEL,
    PRODUCTION_SECRET,
    json_handler,
    make_provider,
    provider_settings,
)

# --- OpenAI-compatible: configuration -------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://transcription.example.test/v1",
        "ftp://transcription.example.test/v1",
        "https://user:pass@transcription.example.test/v1",
        "https://transcription.example.test/v1?key=x",
        "https://transcription.example.test/v1#x",
        "https://transcription.example.test:99999/v1",
        "not a url",
        "https://",
    ],
)
def test_unsafe_base_urls_are_rejected(url):
    with pytest.raises(TranscriptionConfigurationError) as excinfo:
        make_provider(json_handler({}), base_url=url)

    assert "pass" not in str(excinfo.value)


@pytest.mark.parametrize(
    "url", ["http://localhost:8000/v1", "http://127.0.0.1:9000", "http://[::1]:8000/v1"]
)
def test_http_is_allowed_for_loopback_hosts(url):
    make_provider(json_handler({}), base_url=url, api_key=None)


def test_api_key_is_required_for_remote_endpoints():
    with pytest.raises(TranscriptionConfigurationError):
        make_provider(json_handler({}), api_key=None)


def test_api_key_with_whitespace_is_rejected_without_echoing_it():
    with pytest.raises(TranscriptionConfigurationError) as excinfo:
        make_provider(json_handler({}), api_key="secret-value\nX-Injected: 1")

    assert "secret-value" not in str(excinfo.value)


@pytest.mark.parametrize("model", ["", "   ", "a" * 201, "bad\nmodel"])
def test_invalid_model_is_rejected(model):
    with pytest.raises(TranscriptionConfigurationError):
        make_provider(json_handler({}), model=model)


def test_factory_creates_the_openai_compatible_provider():
    provider = get_transcription_provider(provider_settings())

    assert isinstance(provider, OpenAICompatibleTranscriptionProvider)
    assert provider.model == MODEL


def test_factory_allows_the_openai_compatible_provider_in_production():
    settings = provider_settings(APP_ENV="production", SECRET_KEY=PRODUCTION_SECRET)

    assert isinstance(
        get_transcription_provider(settings), OpenAICompatibleTranscriptionProvider
    )


def test_factory_names_every_missing_openai_setting():
    settings = provider_settings(
        TRANSCRIPTION_API_BASE_URL=None,
        TRANSCRIPTION_API_KEY=None,
        TRANSCRIPTION_MODEL=" ",
    )

    with pytest.raises(TranscriptionConfigurationError) as excinfo:
        get_transcription_provider(settings)

    message = str(excinfo.value)
    for name in (
        "TRANSCRIPTION_API_BASE_URL",
        "TRANSCRIPTION_API_KEY",
        "TRANSCRIPTION_MODEL",
    ):
        assert name in message


def test_factory_requires_api_key_for_a_remote_endpoint():
    with pytest.raises(TranscriptionConfigurationError) as excinfo:
        get_transcription_provider(provider_settings(TRANSCRIPTION_API_KEY="  "))

    assert "TRANSCRIPTION_API_KEY" in str(excinfo.value)


def test_factory_does_not_need_api_key_for_a_loopback_endpoint():
    settings = provider_settings(
        TRANSCRIPTION_API_BASE_URL="http://localhost:8000/v1",
        TRANSCRIPTION_API_KEY=None,
    )

    assert isinstance(
        get_transcription_provider(settings), OpenAICompatibleTranscriptionProvider
    )


def test_factory_rejects_http_for_a_remote_endpoint_without_echoing_the_key():
    settings = provider_settings(
        TRANSCRIPTION_API_BASE_URL="http://transcription.example.test"
    )

    with pytest.raises(TranscriptionConfigurationError) as excinfo:
        get_transcription_provider(settings)

    assert API_KEY not in str(excinfo.value)


def test_factory_works_with_the_environment(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_PROVIDER", " OpenAI_Compatible ")
    monkeypatch.setenv("TRANSCRIPTION_API_BASE_URL", BASE_URL)
    monkeypatch.setenv("TRANSCRIPTION_API_KEY", API_KEY)
    monkeypatch.setenv("TRANSCRIPTION_MODEL", MODEL)

    assert isinstance(
        get_transcription_provider(), OpenAICompatibleTranscriptionProvider
    )


def test_transcription_settings_defaults():
    settings = Settings(
        TRANSCRIPTION_API_BASE_URL="", TRANSCRIPTION_API_KEY=" ", TRANSCRIPTION_MODEL=""
    )

    assert settings.TRANSCRIPTION_API_BASE_URL is None
    assert settings.TRANSCRIPTION_API_KEY is None
    assert settings.TRANSCRIPTION_MODEL is None
    assert settings.TRANSCRIPTION_MAX_UPLOAD_MB == 24
    assert settings.TRANSCRIPTION_CHUNK_SECONDS == 600
    assert settings.TRANSCRIPTION_TIMEOUT_SECONDS == 300
    # The key is a secret and never shown in the settings' repr.
    assert API_KEY not in repr(Settings(TRANSCRIPTION_API_KEY=API_KEY))


def test_mapping_language_names():
    assert language_from_provider("english") == "en"
    assert language_from_provider(" Telugu ") == "te"
    assert language_from_provider("hi-IN") == "hi-IN"
    assert language_from_provider("lao") == "lo"
    assert language_from_provider("klingon") is None
    assert language_from_provider(None) is None
    assert language_from_provider(5) is None
