import pytest

from app.core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Load settings only from the test's environment and never reuse a cache.

    A developer's backend/.env is ignored so tests behave the same everywhere,
    and the get_settings() cache is cleared so environment changes made by a
    test are picked up.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def audio_file(tmp_path):
    """A small stand-in audio file whose name must never reach a provider."""
    path = tmp_path / "my private recording.wav"
    path.write_bytes(b"RIFF" + b"\x01" * 96)
    return path


@pytest.fixture
def work_dir(tmp_path):
    """An empty directory for temporary transcription chunks."""
    path = tmp_path / "work"
    path.mkdir()
    return path
