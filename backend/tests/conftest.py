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
