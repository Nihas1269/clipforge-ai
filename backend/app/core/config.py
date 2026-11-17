from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]

# backend/.env, resolved from this file so it is found regardless of the
# current working directory.
ENV_FILE = BACKEND_DIR / ".env"

# backend/storage, git-ignored. Used by the local storage backend.
DEFAULT_LOCAL_STORAGE_DIR = BACKEND_DIR / "storage"

DATABASE_URL_ENV_VAR = "DATABASE_URL"
WILDCARD_ORIGIN = "*"

# Upload allowlists. Entries without a known file signature are ignored by the
# upload endpoint (see app/services/video_upload.py).
DEFAULT_ALLOWED_VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".mkv")
DEFAULT_ALLOWED_VIDEO_CONTENT_TYPES = (
    "video/mp4",
    "video/quicktime",
    "video/webm",
    "video/x-matroska",
)

# Words and phrases the heuristic clip detector treats as a sign of an
# interesting moment (see app/clips/heuristic.py).
DEFAULT_CLIP_KEYWORDS = (
    "secret",
    "mistake",
    "important",
    "surprising",
    "amazing",
    "never",
    "always",
    "best",
    "worst",
    "how to",
    "the truth",
    "lesson",
    "tip",
)

DEFAULT_FFMPEG_PATH = "ffmpeg"
DEFAULT_FFPROBE_PATH = "ffprobe"

# Development-only default for SECRET_KEY. It is public, so it is rejected in
# production.
DEFAULT_SECRET_KEY = "dev-insecure-secret-key-change-me"

# Secret values that are public (code defaults and documented placeholders)
# and must never be used in production. Compared case-insensitively after
# stripping whitespace.
KNOWN_DEVELOPMENT_SECRET_KEYS = frozenset(
    {
        DEFAULT_SECRET_KEY,
        # Placeholder in backend/.env.example.
        "<generate-a-long-random-secret>",
        # Earlier backend/.env.example placeholder.
        "change-me-to-a-long-random-string",
        "change-me",
        "changeme",
        "secret",
        "dev",
        "development",
        "test",
    }
)


def sanitize_cors_origins(origins: Iterable[str]) -> list[str]:
    """Return a cleaned list of allowed CORS origins.

    Entries are stripped and blank or non-string entries are dropped. The
    wildcard "*" is always removed because CORS is configured with credentials
    enabled, and a wildcard would let any origin make credentialed requests.
    """
    cleaned = []
    for origin in origins:
        if not isinstance(origin, str):
            continue
        origin = origin.strip()
        if origin and origin != WILDCARD_ORIGIN:
            cleaned.append(origin)
    return cleaned


def parse_cors_origins(raw: str) -> list[str]:
    """Parse a comma-separated list of allowed CORS origins.

    Blank entries and the wildcard "*" are ignored (see sanitize_cors_origins).
    """
    return sanitize_cors_origins(raw.split(","))


def is_known_development_secret(secret: str) -> bool:
    """Return True if the secret is a public development default or placeholder."""
    normalized = secret.strip().lower()
    return any(normalized == known.lower() for known in KNOWN_DEVELOPMENT_SECRET_KEYS)


class Settings(BaseSettings):
    """Application settings loaded from environment variables and backend/.env.

    Environment variables take precedence over values in the .env file.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        # backend/.env may hold variables that Settings does not read (for
        # example the Docker Compose MINIO_ROOT_* values).
        extra="ignore",
        # Never include submitted values (such as SECRET_KEY) in error messages.
        hide_input_in_errors=True,
    )

    APP_ENV: Literal["development", "test", "production"] = "development"
    APP_NAME: str = "ClipForge AI"
    # Optional so the app starts (and /health responds) without a database;
    # get_database_url() raises a clear error when it is needed but unset.
    DATABASE_URL: str | None = None
    REDIS_URL: str | None = None
    SECRET_KEY: SecretStr = SecretStr(DEFAULT_SECRET_KEY)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=30, gt=0)
    # Lifetimes of the single-use email verification and password reset tokens.
    EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES: int = Field(default=1440, gt=0)
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = Field(default=30, gt=0)
    # NoDecode lets CORS_ORIGINS be a comma-separated string in the
    # environment instead of JSON.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(default_factory=list)
    MAX_UPLOAD_SIZE_MB: int = Field(default=500, gt=0)
    # Comma-separated in the environment; normalized to lower case, and
    # extensions always start with ".".
    ALLOWED_VIDEO_EXTENSIONS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_VIDEO_EXTENSIONS)
    )
    ALLOWED_VIDEO_CONTENT_TYPES: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_VIDEO_CONTENT_TYPES)
    )
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    # Root directory of the local storage backend. A relative path is
    # resolved against the current working directory.
    LOCAL_STORAGE_DIR: Path = DEFAULT_LOCAL_STORAGE_DIR
    # S3-compatible storage (AWS S3, Cloudflare R2, MinIO). Optional here so the
    # app starts without them; get_storage() raises a clear error when
    # STORAGE_BACKEND=s3 and a required value is missing. S3_ENDPOINT_URL is
    # unset for AWS S3.
    S3_ENDPOINT_URL: str | None = None
    S3_REGION: str | None = None
    S3_BUCKET: str | None = None
    S3_ACCESS_KEY_ID: str | None = None
    S3_SECRET_ACCESS_KEY: SecretStr | None = None
    S3_ADDRESSING_STYLE: Literal["auto", "path", "virtual"] = "auto"
    # Outgoing email (verification and password reset). "fake" records
    # messages in memory and never sends; it is refused in production. "smtp"
    # needs SMTP_HOST, EMAIL_FROM and FRONTEND_URL; get_email_sender() reports
    # missing values when email is first used, so the app still starts.
    EMAIL_PROVIDER: Literal["fake", "smtp"] = "fake"
    EMAIL_FROM: str | None = None
    # Base URL of the web app; emailed links point at its /verify-email and
    # /reset-password pages.
    FRONTEND_URL: str | None = None
    SMTP_HOST: str | None = None
    SMTP_PORT: int = Field(default=587, gt=0, lt=65536)
    SMTP_SECURITY: Literal["starttls", "ssl", "none"] = "starttls"
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: SecretStr | None = None
    SMTP_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0, le=120)
    # FFmpeg/ffprobe binaries: a command name looked up on PATH, or a full
    # path. Blank values fall back to the defaults.
    FFMPEG_PATH: str = DEFAULT_FFMPEG_PATH
    FFPROBE_PATH: str = DEFAULT_FFPROBE_PATH
    # Wall-clock limits for a single ffmpeg / ffprobe run; the process is
    # killed when they are exceeded.
    FFMPEG_TIMEOUT_SECONDS: float = Field(default=1800.0, gt=0, le=3600)
    FFPROBE_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0, le=600)
    # Resource limits for FFmpeg processes (POSIX only): virtual address space
    # (not resident memory, so leave headroom for thread stacks and malloc
    # arenas on many-core machines) and the largest file a process may write.
    FFMPEG_MAX_MEMORY_MB: int = Field(default=8192, gt=0)
    FFMPEG_MAX_OUTPUT_FILE_MB: int = Field(default=2048, gt=0)
    # Longer videos are marked failed by metadata extraction.
    MAX_VIDEO_DURATION_SECONDS: int = Field(default=7200, gt=0)
    # Clip detection: length limits of a suggested clip, the most clips
    # suggested per video, and the keywords the heuristic detector rewards
    # (comma-separated in the environment; matched case-insensitively as
    # whole words or phrases).
    CLIP_MIN_SECONDS: float = Field(default=15.0, gt=0, le=600)
    CLIP_MAX_SECONDS: float = Field(default=90.0, gt=0, le=600)
    CLIP_MAX_CLIPS: int = Field(default=10, ge=1, le=50)
    CLIP_KEYWORDS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_CLIP_KEYWORDS)
    )

    # Clip detector: "heuristic" (default, offline) or "llm". A plain string so
    # an unknown name does not stop the app from starting; get_clip_detector()
    # logs a warning and uses the heuristic detector instead. "llm" falls back
    # to the heuristic detector when it is unconfigured or fails.
    CLIP_DETECTOR: str = "heuristic"
    # LLM provider for CLIP_DETECTOR=llm: "openai_compatible" (chat
    # completions) or "anthropic" (Messages API). See docs/setup/llm.md.
    # Optional here so the app starts without them.
    LLM_PROVIDER: str | None = None
    LLM_API_BASE_URL: str | None = None
    LLM_API_KEY: SecretStr | None = None
    LLM_MODEL: str | None = None
    # Overall deadline of one LLM request, checked as the response arrives;
    # each connect/read also times out after at most this long, so a stalled
    # read ends within one more timeout period.
    LLM_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0, le=600)
    # Longest user prompt (in characters) sent in one request; longer
    # transcripts are split into several requests.
    LLM_MAX_PROMPT_CHARS: int = Field(default=24000, ge=2000, le=400000)
    # Most tokens the model may generate per request.
    LLM_MAX_OUTPUT_TOKENS: int = Field(default=2048, ge=256, le=16384)
    # Optional prices in USD per million tokens, used only to log an
    # estimated cost per detection run.
    LLM_INPUT_COST_PER_MILLION_TOKENS: float | None = Field(default=None, ge=0)
    LLM_OUTPUT_COST_PER_MILLION_TOKENS: float | None = Field(default=None, ge=0)

    @field_validator("CLIP_DETECTOR", mode="before")
    @classmethod
    def _normalize_clip_detector(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower() or "heuristic"
        return value

    @field_validator(
        "LLM_PROVIDER",
        "LLM_API_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_INPUT_COST_PER_MILLION_TOKENS",
        "LLM_OUTPUT_COST_PER_MILLION_TOKENS",
        mode="before",
    )
    @classmethod
    def _blank_llm_setting_is_unset(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, str):
            value = value.strip()
            if info.field_name == "LLM_PROVIDER":
                value = value.lower()
            return value or None
        return value

    @field_validator("CLIP_KEYWORDS", mode="before")
    @classmethod
    def _split_clip_keywords(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.split(",")
        return value

    @field_validator("CLIP_KEYWORDS", mode="after")
    @classmethod
    def _normalize_clip_keywords(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for keyword in value:
            # Inner whitespace is collapsed so "how  to" matches "how to".
            keyword = " ".join(keyword.split()).casefold()
            if keyword and keyword not in normalized:
                normalized.append(keyword)
        return normalized

    @model_validator(mode="after")
    def _check_clip_lengths(self) -> "Settings":
        if self.CLIP_MIN_SECONDS > self.CLIP_MAX_SECONDS:
            raise ValueError("CLIP_MIN_SECONDS must not exceed CLIP_MAX_SECONDS.")
        return self

    @field_validator("FFMPEG_PATH", "FFPROBE_PATH", mode="before")
    @classmethod
    def _blank_binary_path_is_default(cls, value: Any, info: ValidationInfo) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return (
                DEFAULT_FFMPEG_PATH
                if info.field_name == "FFMPEG_PATH"
                else DEFAULT_FFPROBE_PATH
            )
        if isinstance(value, str):
            return value.strip()
        return value

    # Speech-to-text provider. A plain string, not a Literal, so an unknown
    # name does not stop the app from starting; get_transcription_provider()
    # raises a clear error when transcription is first used. "fake" returns
    # deterministic placeholder text and is refused in production.
    TRANSCRIPTION_PROVIDER: str = "fake"
    # Real providers (see backend/README.md and docs/setup/transcription.md).
    # Optional here so the app starts without them; the provider factory
    # names the missing values. TRANSCRIPTION_MODEL is the remote model name
    # for "openai_compatible" and the model size or path for "local_whisper".
    TRANSCRIPTION_API_BASE_URL: str | None = None
    TRANSCRIPTION_API_KEY: SecretStr | None = None
    TRANSCRIPTION_MODEL: str | None = None
    # Wall-clock limit of one request to a remote provider.
    TRANSCRIPTION_TIMEOUT_SECONDS: float = Field(default=300.0, gt=0, le=1800)
    # Largest file sent to a remote provider in one request; larger audio is
    # split into consecutive chunks of at most TRANSCRIPTION_CHUNK_SECONDS.
    TRANSCRIPTION_MAX_UPLOAD_MB: int = Field(default=24, gt=0, le=1024)
    TRANSCRIPTION_CHUNK_SECONDS: int = Field(default=600, ge=30, le=3600)

    @field_validator("TRANSCRIPTION_PROVIDER", mode="before")
    @classmethod
    def _normalize_transcription_provider(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower() or "fake"
        return value

    @field_validator(
        "TRANSCRIPTION_API_BASE_URL",
        "TRANSCRIPTION_API_KEY",
        "TRANSCRIPTION_MODEL",
        mode="before",
    )
    @classmethod
    def _blank_transcription_setting_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator(
        "EMAIL_FROM",
        "FRONTEND_URL",
        "SMTP_HOST",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        mode="before",
    )
    @classmethod
    def _blank_email_setting_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("EMAIL_PROVIDER", "SMTP_SECURITY", mode="before")
    @classmethod
    def _normalize_email_choice(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("FRONTEND_URL", mode="after")
    @classmethod
    def _validate_frontend_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if (
            parts.scheme.lower() not in ("http", "https")
            or not parts.netloc
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "FRONTEND_URL must be an http:// or https:// URL without a "
                "query or fragment."
            )
        return value.rstrip("/")

    @field_validator(
        "S3_ENDPOINT_URL",
        "S3_REGION",
        "S3_BUCKET",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        mode="before",
    )
    @classmethod
    def _blank_s3_setting_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("S3_ADDRESSING_STYLE", mode="before")
    @classmethod
    def _normalize_addressing_style(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower() or "auto"
        return value

    @field_validator("LOCAL_STORAGE_DIR", mode="before")
    @classmethod
    def _reject_blank_storage_dir(cls, value: Any) -> Any:
        # Path("") is the current directory; never store files there by
        # accident.
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise ValueError("LOCAL_STORAGE_DIR must not be empty.")
        return value

    @field_validator("DATABASE_URL", "REDIS_URL", mode="before")
    @classmethod
    def _blank_url_is_unset(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.split(",")
        return value

    @field_validator(
        "ALLOWED_VIDEO_EXTENSIONS", "ALLOWED_VIDEO_CONTENT_TYPES", mode="before"
    )
    @classmethod
    def _split_upload_allowlist(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.split(",")
        return value

    @field_validator("ALLOWED_VIDEO_EXTENSIONS", mode="after")
    @classmethod
    def _normalize_extensions(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for ext in value:
            ext = ext.strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = f".{ext}"
            if ext not in normalized:
                normalized.append(ext)
        return normalized

    @field_validator("ALLOWED_VIDEO_CONTENT_TYPES", mode="after")
    @classmethod
    def _normalize_content_types(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for content_type in value:
            content_type = content_type.strip().lower()
            if content_type and content_type not in normalized:
                normalized.append(content_type)
        return normalized

    @field_validator("CORS_ORIGINS", mode="after")
    @classmethod
    def _sanitize_cors_origins(cls, value: list[str]) -> list[str]:
        # Enforce the wildcard restriction for every construction path, not only
        # when settings are loaded from environment variables.
        return sanitize_cors_origins(value)

    @model_validator(mode="after")
    def _require_production_secret(self) -> "Settings":
        if self.APP_ENV != "production":
            return self
        secret = self.SECRET_KEY.get_secret_value()
        if not secret.strip():
            raise ValueError(
                "SECRET_KEY must be set when APP_ENV is 'production'. "
                "See backend/README.md for configuration instructions."
            )
        if is_known_development_secret(secret):
            raise ValueError(
                "SECRET_KEY is a known development default or placeholder and "
                "must not be used when APP_ENV is 'production'. Set it to a "
                "long random value. See backend/README.md."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the application settings, loaded once and cached.

    Every backend module reads configuration through this accessor. Call
    get_settings.cache_clear() to reload (for example in tests).
    """
    return Settings()


def get_database_url() -> str:
    """Return the configured database URL or raise a clear error if it is unset."""
    database_url = get_settings().DATABASE_URL
    if not database_url:
        raise RuntimeError(
            f"The {DATABASE_URL_ENV_VAR} environment variable is not set. "
            "See backend/README.md for database configuration instructions."
        )
    return database_url
