"""Speech-to-text through an OpenAI-compatible `/audio/transcriptions` endpoint.

The audio is posted as multipart form data with `response_format=verbose_json`
and segment and word timestamp granularities. Configuration:
TRANSCRIPTION_API_BASE_URL (https://, or http:// for a loopback host only),
TRANSCRIPTION_API_KEY (sent as a bearer token; optional for loopback hosts)
and TRANSCRIPTION_MODEL.

Files larger than the upload limit are split with FFmpeg into consecutive,
non-overlapping chunks of mono 16 kHz WAV, at most `chunk_seconds` long (a
last piece shorter than MIN_CHUNK_SECONDS is merged into the one before it).
Each chunk is transcribed on its own and its timestamps are shifted by the
chunk's start exactly once (see mapping.place_segments). Chunks are written
to a temporary directory that is removed when transcription ends, on success
or failure.

Requests are not retried here; failures raise TranscriptionProviderError
with `retryable` set, so the job layer decides. API keys, response bodies,
transcript text and audio are never logged or put in error messages.
"""

import ipaddress
import logging
import math
import mimetypes
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.media import ffmpeg
from app.media.ffmpeg import MediaError
from app.transcription.base import (
    UNDETERMINED_LANGUAGE,
    InvalidTranscriptionResultError,
    TranscriptionConfigurationError,
    TranscriptionError,
    TranscriptionProviderError,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionTimeoutError,
    normalize_language,
)
from app.transcription.mapping import (
    RawSegment,
    RawWord,
    assign_words,
    language_from_provider,
    place_segments,
    provider_time,
)

logger = logging.getLogger(__name__)

OPENAI_COMPATIBLE_PROVIDER_NAME = "openai_compatible"
TRANSCRIPTIONS_PATH = "/audio/transcriptions"
BYTES_PER_MB = 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0
# A verbose JSON transcript of one chunk is far smaller than this.
MAX_RESPONSE_BYTES = 20 * BYTES_PER_MB
MAX_MODEL_NAME_LENGTH = 200

# Chunks are mono 16-bit PCM WAV at 16 kHz: 32,000 bytes per second plus a
# small header (WAV_HEADER_MARGIN_BYTES leaves room for it).
CHUNK_SAMPLE_RATE = 16000
CHUNK_BYTES_PER_SECOND = CHUNK_SAMPLE_RATE * 2
WAV_HEADER_MARGIN_BYTES = 4096
# Providers reject very short audio, so a shorter last piece is merged into
# the previous chunk (which may then be up to this much longer).
MIN_CHUNK_SECONDS = 1.0

LOOPBACK_HOSTNAMES = frozenset({"localhost"})
_SAFE_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,10}")
_API_KEY = re.compile(r"[\x21-\x7e]+")

DurationProbe = Callable[[Path], float | None]
ChunkWriter = Callable[[Path, Path, float, float], None]


def is_loopback_host(host: str) -> bool:
    host = host.lower()
    if host in LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_loopback_url(url: str) -> bool:
    """Return True if `url` is an http(s) URL whose host is a loopback address."""
    try:
        parts = urlsplit(url.strip())
        return parts.scheme.lower() in ("http", "https") and is_loopback_host(
            parts.hostname or ""
        )
    except ValueError:
        return False


def validate_base_url(url: str) -> str:
    """Return the base URL without a trailing slash, or raise a configuration error.

    It must be https:// (http:// only for a loopback host such as localhost)
    and must not contain credentials, a query or a fragment. The URL is not
    repeated in the error message.
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        # Accessing the port raises ValueError for an invalid one.
        _ = parts.port
    except ValueError:
        raise TranscriptionConfigurationError(
            "TRANSCRIPTION_API_BASE_URL is not a valid URL."
        ) from None
    scheme = parts.scheme.lower()
    if (
        scheme not in ("http", "https")
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise TranscriptionConfigurationError(
            "TRANSCRIPTION_API_BASE_URL must be an https:// URL without "
            "credentials, a query or a fragment."
        )
    if scheme == "http" and not is_loopback_host(host):
        raise TranscriptionConfigurationError(
            "TRANSCRIPTION_API_BASE_URL must use https://; http:// is only "
            "allowed for a local endpoint (localhost, 127.0.0.1 or ::1)."
        )
    return url.strip().rstrip("/")


def plan_chunks(
    total_seconds: float, chunk_seconds: float
) -> list[tuple[float, float]]:
    """Split [0, total_seconds] into consecutive (offset, duration) pieces.

    Pieces do not overlap and are at most `chunk_seconds` long, except that a
    last piece shorter than MIN_CHUNK_SECONDS is merged into the previous one.
    """
    pieces: list[tuple[float, float]] = []
    index = 0
    while (offset := index * chunk_seconds) < total_seconds:
        pieces.append((offset, min(chunk_seconds, total_seconds - offset)))
        index += 1
    if len(pieces) > 1 and pieces[-1][1] < MIN_CHUNK_SECONDS:
        pieces.pop()
        previous_offset, _ = pieces.pop()
        pieces.append((previous_offset, total_seconds - previous_offset))
    return pieces


def ffmpeg_duration(path: Path, settings: Settings | None = None) -> float | None:
    """Return the duration of an audio file in seconds using ffprobe."""
    return ffmpeg.probe(path, settings=settings).duration_seconds


def ffmpeg_write_chunk(
    source: Path,
    target: Path,
    offset: float,
    duration: float,
    settings: Settings | None = None,
) -> None:
    """Write `duration` seconds of `source`, starting at `offset`, as 16 kHz mono WAV."""
    ffmpeg.run_ffmpeg(
        [
            "-protocol_whitelist",
            ffmpeg.INPUT_PROTOCOLS,
            "-ss",
            f"{offset:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source.resolve()),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(CHUNK_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            "-y",
            str(target),
        ],
        settings=settings,
    )


def chunk_seconds_for(max_upload_bytes: int, chunk_seconds: float) -> float:
    """Return the longest chunk that fits the upload limit, at most `chunk_seconds`."""
    by_size = math.floor(
        (max_upload_bytes - WAV_HEADER_MARGIN_BYTES) / CHUNK_BYTES_PER_SECOND
        - MIN_CHUNK_SECONDS
    )
    return float(min(chunk_seconds, by_size))


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _upload_name(path: Path) -> tuple[str, str]:
    # Only a generic name is sent, never the local file name.
    suffix = path.suffix.lower() if _SAFE_SUFFIX.fullmatch(path.suffix) else ".bin"
    name = f"audio{suffix}"
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return name, content_type


def _parse_word(item: Any) -> RawWord:
    if not isinstance(item, dict) or not isinstance(item.get("word"), str):
        raise InvalidTranscriptionResultError("The provider returned a malformed word.")
    start = provider_time(item.get("start"), "word start")
    end = provider_time(item.get("end"), "word end")
    if end < start:
        raise InvalidTranscriptionResultError(
            "The provider returned a word that ends before it starts."
        )
    return RawWord(item["word"], start, end)


def _parse_words(value: Any) -> list[RawWord] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise InvalidTranscriptionResultError("The provider returned malformed words.")
    return [_parse_word(item) for item in value]


def parse_verbose_json(
    data: Any, duration: float
) -> tuple[str | None, list[RawSegment]]:
    """Return the reported language and the raw segments of a verbose JSON response.

    Words may be reported per segment or for the whole transcript (OpenAI);
    transcript-level words are assigned to segments by time. A response with
    text but no segments becomes one segment covering the audio. Raises
    InvalidTranscriptionResultError for a malformed response.
    """
    if not isinstance(data, dict):
        raise InvalidTranscriptionResultError(
            "The provider returned a malformed response."
        )
    raw_segments = data.get("segments")
    if raw_segments is None:
        raw_segments = []
    if not isinstance(raw_segments, list):
        raise InvalidTranscriptionResultError(
            "The provider returned malformed segments."
        )
    top_level_words = _parse_words(data.get("words"))

    segments = []
    for item in raw_segments:
        if not isinstance(item, dict) or not isinstance(item.get("text", ""), str):
            raise InvalidTranscriptionResultError(
                "The provider returned a malformed segment."
            )
        words = _parse_words(item.get("words"))
        segments.append(
            RawSegment(
                start=provider_time(item.get("start"), "segment start"),
                end=provider_time(item.get("end"), "segment end"),
                text=item.get("text", ""),
                words=tuple(words) if words is not None else None,
            )
        )

    text = data.get("text")
    if text is not None and not isinstance(text, str):
        raise InvalidTranscriptionResultError("The provider returned malformed text.")
    if not segments and text and text.strip() and duration > 0:
        segments = [RawSegment(0.0, duration, text)]
    if top_level_words is not None and all(s.words is None for s in segments):
        segments = assign_words(segments, top_level_words)
    return language_from_provider(data.get("language")), segments


class OpenAICompatibleTranscriptionProvider:
    """A TranscriptionProvider for OpenAI-compatible transcription APIs."""

    name = OPENAI_COMPATIBLE_PROVIDER_NAME

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr | str | None = None,
        timeout_seconds: float = 300.0,
        max_upload_bytes: int = 24 * BYTES_PER_MB,
        chunk_seconds: float = 600.0,
        transport: httpx.BaseTransport | None = None,
        duration_probe: DurationProbe = ffmpeg_duration,
        chunk_writer: ChunkWriter = ffmpeg_write_chunk,
        temp_dir: str | Path | None = None,
    ) -> None:
        self._endpoint = validate_base_url(base_url) + TRANSCRIPTIONS_PATH
        if (
            not isinstance(model, str)
            or not model.strip()
            or len(model) > MAX_MODEL_NAME_LENGTH
            or not model.isprintable()
        ):
            raise TranscriptionConfigurationError(
                "TRANSCRIPTION_MODEL must be a non-empty model name."
            )
        if isinstance(api_key, str):
            api_key = SecretStr(api_key)
        if api_key is not None and not _API_KEY.fullmatch(api_key.get_secret_value()):
            # The value itself is never included in the message.
            raise TranscriptionConfigurationError(
                "TRANSCRIPTION_API_KEY contains whitespace or non-printable characters."
            )
        if api_key is None and not is_loopback_url(base_url):
            raise TranscriptionConfigurationError(
                "TRANSCRIPTION_API_KEY is required for a remote transcription endpoint."
            )
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise TranscriptionConfigurationError(
                "The request timeout must be positive."
            )
        if max_upload_bytes <= 0:
            raise TranscriptionConfigurationError("The upload limit must be positive.")
        if not math.isfinite(chunk_seconds) or chunk_seconds < MIN_CHUNK_SECONDS:
            raise TranscriptionConfigurationError(
                f"The chunk length must be at least {MIN_CHUNK_SECONDS:g} second."
            )
        self.model = model.strip()
        self._api_key = api_key
        self._timeout = httpx.Timeout(
            timeout_seconds, connect=min(CONNECT_TIMEOUT_SECONDS, timeout_seconds)
        )
        self._max_upload_bytes = max_upload_bytes
        self._chunk_seconds = chunk_seconds
        self._transport = transport
        self._duration_probe = duration_probe
        self._chunk_writer = chunk_writer
        self._temp_dir = temp_dir

    @classmethod
    def from_settings(
        cls, settings: Settings, **overrides: Any
    ) -> "OpenAICompatibleTranscriptionProvider":
        """Create the provider from settings; raise a clear error if any are missing."""
        base_url = settings.TRANSCRIPTION_API_BASE_URL
        missing = []
        if not base_url:
            missing.append("TRANSCRIPTION_API_BASE_URL")
        if settings.TRANSCRIPTION_API_KEY is None and not (
            base_url and is_loopback_url(base_url)
        ):
            missing.append("TRANSCRIPTION_API_KEY")
        if not settings.TRANSCRIPTION_MODEL:
            missing.append("TRANSCRIPTION_MODEL")
        if missing:
            raise TranscriptionConfigurationError(
                f"TRANSCRIPTION_PROVIDER={OPENAI_COMPATIBLE_PROVIDER_NAME!r} needs "
                f"these settings: {', '.join(missing)}. See docs/setup/transcription.md."
            )
        max_upload_bytes = settings.TRANSCRIPTION_MAX_UPLOAD_MB * BYTES_PER_MB
        options: dict[str, Any] = {
            "base_url": base_url,
            "model": settings.TRANSCRIPTION_MODEL,
            "api_key": settings.TRANSCRIPTION_API_KEY,
            "timeout_seconds": settings.TRANSCRIPTION_TIMEOUT_SECONDS,
            "max_upload_bytes": max_upload_bytes,
            "chunk_seconds": chunk_seconds_for(
                max_upload_bytes, settings.TRANSCRIPTION_CHUNK_SECONDS
            ),
            "duration_probe": lambda path: ffmpeg_duration(path, settings),
            "chunk_writer": lambda source, target, offset, duration: ffmpeg_write_chunk(
                source, target, offset, duration, settings
            ),
        }
        options.update(overrides)
        return cls(**options)

    def transcribe(
        self, audio_path: str | Path, language: str | None = None
    ) -> TranscriptionResult:
        # Validate the request before touching the file.
        requested = normalize_language(language)
        path = Path(audio_path)
        if not path.is_file():
            raise TranscriptionError("The audio file does not exist.")
        try:
            size = path.stat().st_size
            total = self._duration_probe(path)
        except (OSError, MediaError) as exc:
            raise TranscriptionError(
                f"The audio file could not be read ({type(exc).__name__})."
            ) from None
        if (
            isinstance(total, bool)
            or not isinstance(total, (int, float))
            or not math.isfinite(total)
            or total < 0
        ):
            raise TranscriptionError("The audio duration could not be determined.")
        total = float(total)

        started = time.monotonic()
        segments: list[TranscriptionSegment] = []
        detected: str | None = None
        with httpx.Client(
            transport=self._transport, timeout=self._timeout, follow_redirects=False
        ) as client:
            if size <= self._max_upload_bytes:
                pieces = [(0.0, total)] if total > 0 else []
                for offset, duration in pieces:
                    language_found, placed = self._transcribe_piece(
                        client, path, requested, 0, offset, duration, total
                    )
                    detected = detected or language_found
                    segments.extend(placed)
            else:
                pieces = plan_chunks(total, self._chunk_seconds)
                with tempfile.TemporaryDirectory(
                    prefix="clipforge-transcription-", dir=self._temp_dir
                ) as work_dir:
                    for index, (offset, duration) in enumerate(pieces):
                        chunk = Path(work_dir) / f"chunk-{index:05d}.wav"
                        try:
                            self._write_chunk(path, chunk, offset, duration)
                            language_found, placed = self._transcribe_piece(
                                client, chunk, requested, index, offset, duration, total
                            )
                        finally:
                            chunk.unlink(missing_ok=True)
                        detected = detected or language_found
                        segments.extend(placed)

        result_language: str | None
        if requested is not None and requested != UNDETERMINED_LANGUAGE:
            result_language = requested
        else:
            result_language = detected or requested
        result = TranscriptionResult(
            language=result_language,
            segments=tuple(segments),
            provider=self.name,
            model=self.model,
        )
        result.check_within_duration(total)
        logger.info(
            "transcription_completed",
            extra={
                "provider": self.name,
                "model": self.model,
                "language": result.language,
                "segment_count": len(result.segments),
                "chunk_count": len(pieces),
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return result

    def _write_chunk(
        self, source: Path, chunk: Path, offset: float, duration: float
    ) -> None:
        try:
            self._chunk_writer(source, chunk, offset, duration)
            size = chunk.stat().st_size if chunk.is_file() else 0
        except (OSError, MediaError) as exc:
            raise TranscriptionError(
                f"The audio could not be split into chunks ({type(exc).__name__})."
            ) from None
        if size == 0:
            raise TranscriptionError("The audio could not be split into chunks.")
        if size > self._max_upload_bytes:
            raise TranscriptionError(
                "An audio chunk is larger than the provider upload limit; lower "
                "TRANSCRIPTION_CHUNK_SECONDS or raise TRANSCRIPTION_MAX_UPLOAD_MB."
            )

    def _transcribe_piece(
        self,
        client: httpx.Client,
        audio: Path,
        requested: str | None,
        index: int,
        offset: float,
        duration: float,
        total: float,
    ) -> tuple[str | None, list[TranscriptionSegment]]:
        data = self._post(client, audio, requested, index)
        language, raw_segments = parse_verbose_json(data, duration)
        placed = place_segments(
            raw_segments,
            offset=offset,
            duration=duration,
            end_limit=min(offset + duration, total),
        )
        return language, placed

    def _post(
        self, client: httpx.Client, audio: Path, requested: str | None, index: int
    ) -> Any:
        form: dict[str, Any] = {
            "model": self.model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": ["segment", "word"],
        }
        if requested is not None and requested != UNDETERMINED_LANGUAGE:
            # The API takes ISO 639-1 codes, so only the primary subtag is sent.
            form["language"] = requested.split("-")[0]
        headers = {"Accept": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        upload_name, content_type = _upload_name(audio)
        try:
            with audio.open("rb") as audio_file:
                response = client.post(
                    self._endpoint,
                    data=form,
                    files={"file": (upload_name, audio_file, content_type)},
                    headers=headers,
                )
        except httpx.TimeoutException:
            self._log_failure(index, None, retryable=True, reason="timeout")
            raise TranscriptionTimeoutError(
                "The transcription provider did not answer in time."
            ) from None
        except httpx.TransportError as exc:
            self._log_failure(index, None, retryable=True, reason=type(exc).__name__)
            raise TranscriptionProviderError(
                f"The transcription provider could not be reached ({type(exc).__name__}).",
                retryable=True,
            ) from None
        except httpx.HTTPError as exc:
            self._log_failure(index, None, retryable=False, reason=type(exc).__name__)
            raise TranscriptionProviderError(
                f"The transcription request failed ({type(exc).__name__})."
            ) from None
        except OSError as exc:
            raise TranscriptionError(
                f"The audio file could not be read ({type(exc).__name__})."
            ) from None

        self._check_status(response, index)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise InvalidTranscriptionResultError(
                "The provider returned an unexpectedly large response."
            )
        try:
            return response.json()
        except ValueError:
            raise InvalidTranscriptionResultError(
                "The provider returned a malformed response (not JSON)."
            ) from None

    def _check_status(self, response: httpx.Response, index: int) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        retry_after = None
        if status in (401, 403):
            message = (
                f"The transcription provider rejected the credentials (HTTP {status}). "
                "Check TRANSCRIPTION_API_KEY."
            )
            retryable = False
        elif status == 429:
            message = "The transcription provider is rate limiting requests (HTTP 429)."
            retryable = True
            retry_after = _retry_after(response.headers.get("Retry-After"))
        elif status == 413:
            message = (
                "The transcription provider rejected the audio as too large (HTTP 413); "
                "lower TRANSCRIPTION_MAX_UPLOAD_MB."
            )
            retryable = False
        elif status == 408 or status >= 500:
            message = f"The transcription provider failed (HTTP {status})."
            retryable = True
        else:
            message = (
                f"The transcription provider rejected the request (HTTP {status})."
            )
            retryable = False
        self._log_failure(index, status, retryable=retryable, reason="http_status")
        raise TranscriptionProviderError(
            message, status_code=status, retryable=retryable, retry_after=retry_after
        )

    def _log_failure(
        self, index: int, status: int | None, *, retryable: bool, reason: str
    ) -> None:
        logger.warning(
            "transcription_request_failed",
            extra={
                "provider": self.name,
                "model": self.model,
                "chunk_index": index,
                "status_code": status,
                "retryable": retryable,
                "reason": reason,
            },
        )
