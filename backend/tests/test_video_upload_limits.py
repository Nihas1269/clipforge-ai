import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.core.config import get_settings
from app.database import Base, create_db_engine, create_session_factory, get_db
from app.main import create_app

SECRET = "test-only-jwt-signing-secret-0123456789abcdef"
PASSWORD = "s3cure-Passw0rd"
LIMITS_URL = "/api/v1/videos/upload-limits"
MIB = 1024 * 1024


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    engine = create_db_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)

    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path / "storage"))
    for name in ("MAX_UPLOAD_SIZE_MB", "ALLOWED_VIDEO_EXTENSIONS", "ALLOWED_VIDEO_CONTENT_TYPES"):
        monkeypatch.delenv(name, raising=False)

    def factory(**env: str) -> TestClient:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        application = create_app()

        def override_get_db():
            session = session_factory()
            try:
                yield session
            finally:
                session.close()

        application.dependency_overrides[get_db] = override_get_db
        return TestClient(application)

    yield factory
    get_settings.cache_clear()
    engine.dispose()


def _token(client: TestClient) -> str:
    credentials = {"email": "alice@example.com", "password": PASSWORD}
    assert client.post("/api/v1/auth/register", json=credentials).status_code == 201
    response = client.post("/api/v1/auth/login", json=credentials)
    assert response.status_code == 200
    client.cookies.clear()
    return response.json()["access_token"]


def test_upload_limits_require_authentication(make_client):
    client = make_client()

    response = client.get(LIMITS_URL)

    assert response.status_code == 401


def test_upload_limits_return_the_default_settings(make_client):
    client = make_client()
    token = _token(client)

    response = client.get(LIMITS_URL, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "max_upload_size_bytes": 500 * MIB,
        "allowed_extensions": [".mp4", ".mov", ".webm", ".mkv"],
        "allowed_content_types": [
            "video/mp4",
            "video/quicktime",
            "video/webm",
            "video/x-matroska",
        ],
    }


def test_upload_limits_follow_configuration_and_drop_unsupported_entries(make_client):
    client = make_client(
        MAX_UPLOAD_SIZE_MB="2",
        ALLOWED_VIDEO_EXTENSIONS="mp4,.AVI,.webm",
        ALLOWED_VIDEO_CONTENT_TYPES="video/mp4,video/x-msvideo",
    )
    token = _token(client)

    response = client.get(LIMITS_URL, headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "max_upload_size_bytes": 2 * MIB,
        "allowed_extensions": [".mp4", ".webm"],
        "allowed_content_types": ["video/mp4"],
    }
