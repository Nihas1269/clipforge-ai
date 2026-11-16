"""Safe wrappers around the FFmpeg and ffprobe command-line tools.

Every process is started with an argument list (never a shell), with stdin
closed, a wall-clock timeout, and stdout/stderr written to temporary files of
which only a bounded amount is read back. On POSIX systems the address space
and the size of files the process may write are limited as well. Input files
are opened with `-protocol_whitelist file`, so a crafted media file cannot
make FFmpeg open network or other non-file resources.

The binaries are FFMPEG_PATH and FFPROBE_PATH (a name looked up on PATH or a
full path). Every failure raises MediaError (or a subclass); its message
includes the tail of the tool's stderr, truncated, so it must not be shown to
end users as is.
"""

import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

BYTES_PER_MB = 1024 * 1024
# ffprobe's JSON for the few entries requested is a few KiB; anything larger
# is treated as malformed.
MAX_PROBE_OUTPUT_BYTES = 1024 * 1024
# Only the end of stderr is kept: that is where FFmpeg reports the error.
STDERR_TAIL_BYTES = 8 * 1024
STDERR_MAX_CHARS = 1000

DEFAULT_SAMPLE_RATE = 16000
MIN_SAMPLE_RATE = 8000
MAX_SAMPLE_RATE = 192000

MAX_DIMENSION = 65535
MAX_FPS = 1000.0

# Only these entries are requested from ffprobe, which keeps its output small.
PROBE_ENTRIES = (
    "format=duration,format_name"
    ":stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration"
    ":stream_disposition=attached_pic"
)
INPUT_PROTOCOLS = "file"

_CODEC_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class MediaError(Exception):
    """FFmpeg or ffprobe failed, or produced output that cannot be used.

    `stderr` holds the (truncated) tail of the tool's error output, if any.
    """

    def __init__(self, message: str, stderr: str | None = None) -> None:
        self.message = message
        self.stderr = truncate_stderr(stderr) if stderr else None
        text = f"{message} stderr: {self.stderr}" if self.stderr else message
        super().__init__(text)


class FFmpegNotFoundError(MediaError):
    """The configured ffmpeg or ffprobe binary does not exist or cannot be run."""


class MediaTimeoutError(MediaError):
    """The process did not finish within its timeout and was killed."""


@dataclass(frozen=True)
class MediaInfo:
    """What probe() found out about a media file.

    Values that ffprobe did not report, or reported as nonsense (zero,
    negative, infinite), are None.
    """

    duration_seconds: float | None
    width: int | None
    height: int | None
    fps: float | None
    has_video: bool
    has_audio: bool
    video_codec: str | None
    audio_codec: str | None
    format_name: str | None = None


@dataclass(frozen=True)
class ProcessResult:
    """A finished process: exit code, captured stdout and the stderr tail."""

    returncode: int
    stdout: bytes
    stderr: str


def truncate_stderr(stderr: str, max_chars: int = STDERR_MAX_CHARS) -> str:
    """Return the end of `stderr` without control characters, at most `max_chars`."""
    text = _CONTROL_CHARACTERS.sub(" ", stderr).strip()
    if len(text) > max_chars:
        text = "..." + text[-(max_chars - 3) :]
    return text


def _resolve_binary(configured: str, setting_name: str) -> str:
    found = shutil.which(configured)
    if found is None:
        raise FFmpegNotFoundError(
            f"{configured!r} was not found or is not executable. Install FFmpeg "
            f"or set {setting_name} to the full path of the binary."
        )
    return found


def ffmpeg_binary(settings: Settings | None = None) -> str:
    """Return the full path of the configured ffmpeg binary or raise FFmpegNotFoundError."""
    settings = settings or get_settings()
    return _resolve_binary(settings.FFMPEG_PATH, "FFMPEG_PATH")


def ffprobe_binary(settings: Settings | None = None) -> str:
    """Return the full path of the configured ffprobe binary or raise FFmpegNotFoundError."""
    settings = settings or get_settings()
    return _resolve_binary(settings.FFPROBE_PATH, "FFPROBE_PATH")


def _resource_limiter(settings: Settings) -> Callable[[], None] | None:
    """Return a preexec_fn applying resource limits, or None where unsupported."""
    if sys.platform == "win32":
        return None
    try:
        import resource
    except ImportError:  # pragma: no cover - every POSIX Python has it
        return None

    memory = settings.FFMPEG_MAX_MEMORY_MB * BYTES_PER_MB
    file_size = settings.FFMPEG_MAX_OUTPUT_FILE_MB * BYTES_PER_MB

    def apply_limits() -> None:
        # Runs in the child just before exec. A limit the platform does not
        # support is skipped rather than failing the process.
        for limit, value in (
            (getattr(resource, "RLIMIT_AS", None), memory),
            (getattr(resource, "RLIMIT_FSIZE", None), file_size),
            (getattr(resource, "RLIMIT_CORE", None), 0),
        ):
            if limit is None:
                continue
            try:
                resource.setrlimit(limit, (value, value))
            except (ValueError, OSError):
                pass

    return apply_limits


def _read_head(fileobj: IO[bytes], max_bytes: int) -> bytes:
    fileobj.seek(0)
    return fileobj.read(max_bytes + 1)


def _read_tail(fileobj: IO[bytes], max_bytes: int = STDERR_TAIL_BYTES) -> str:
    fileobj.seek(0, os.SEEK_END)
    size = fileobj.tell()
    fileobj.seek(max(0, size - max_bytes))
    return fileobj.read(max_bytes).decode("utf-8", errors="replace")


def _run(
    command: list[str],
    *,
    timeout: float,
    settings: Settings,
    max_stdout_bytes: int,
    tool: str,
) -> ProcessResult:
    """Run `command` (an argument list, no shell) with bounded time and output."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=timeout,
                shell=False,
                check=False,
                close_fds=True,
                preexec_fn=_resource_limiter(settings),
            )
        except subprocess.TimeoutExpired:
            # subprocess.run() has already killed the process.
            raise MediaTimeoutError(
                f"{tool} did not finish within {timeout:g} seconds and was stopped.",
                _read_tail(err),
            ) from None
        except FileNotFoundError:
            raise FFmpegNotFoundError(
                f"{tool} could not be started because the binary was not found."
            ) from None
        except OSError as exc:
            raise MediaError(
                f"{tool} could not be started ({type(exc).__name__}: {exc.strerror})."
            ) from None

        stdout = _read_head(out, max_stdout_bytes)
        stderr = _read_tail(err)
    if len(stdout) > max_stdout_bytes:
        raise MediaError(f"{tool} produced more output than expected.", stderr)
    return ProcessResult(returncode=completed.returncode, stdout=stdout, stderr=stderr)


def _as_arguments(args: Sequence[str | os.PathLike[str]]) -> list[str]:
    if isinstance(args, (str, bytes)) or not isinstance(args, Sequence):
        raise TypeError(
            "FFmpeg arguments must be a list of strings, not a command line."
        )
    arguments = []
    for arg in args:
        if isinstance(arg, os.PathLike):
            arg = os.fspath(arg)
        if not isinstance(arg, str):
            raise TypeError("Every FFmpeg argument must be a string or a path.")
        if "\x00" in arg:
            raise ValueError("FFmpeg arguments must not contain NUL characters.")
        arguments.append(arg)
    return arguments


def run_ffmpeg(
    args: Sequence[str | os.PathLike[str]],
    *,
    timeout: float | None = None,
    settings: Settings | None = None,
) -> ProcessResult:
    """Run ffmpeg with `args` and return the result; raise MediaError on failure.

    `args` is an argument list (never a shell command line); `-nostdin
    -hide_banner -loglevel error` are added in front. `timeout` defaults to
    FFMPEG_TIMEOUT_SECONDS. A non-zero exit raises MediaError with the
    truncated stderr; a timeout raises MediaTimeoutError and a missing binary
    FFmpegNotFoundError.
    """
    settings = settings or get_settings()
    arguments = _as_arguments(args)
    if timeout is None:
        timeout = settings.FFMPEG_TIMEOUT_SECONDS
    command = [
        ffmpeg_binary(settings),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *arguments,
    ]
    result = _run(
        command,
        timeout=timeout,
        settings=settings,
        max_stdout_bytes=MAX_PROBE_OUTPUT_BYTES,
        tool="ffmpeg",
    )
    if result.returncode != 0:
        raise MediaError(
            f"ffmpeg failed with exit code {result.returncode}.", result.stderr
        )
    return result


def _input_file(path: str | os.PathLike[str]) -> str:
    """Return the absolute path of an existing regular file for use as an input.

    An absolute path never starts with "-", so it cannot be taken for an option.
    """
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise MediaError("The input file does not exist or is not a regular file.")
    return str(resolved)


# Parsing ffprobe output


def _positive_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _dimension(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= MAX_DIMENSION else None


def _frame_rate(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    numerator, _, denominator = value.partition("/")
    try:
        num = int(numerator)
        den = int(denominator) if denominator else 1
    except ValueError:
        return None
    if num <= 0 or den <= 0:
        return None
    fps = num / den
    return fps if fps <= MAX_FPS else None


def _codec(value: Any) -> str | None:
    if isinstance(value, str) and _CODEC_NAME.fullmatch(value):
        return value
    return None


def _is_attached_picture(stream: dict[str, Any]) -> bool:
    # Cover art in audio files is reported as a video stream.
    disposition = stream.get("disposition")
    return isinstance(disposition, dict) and disposition.get("attached_pic") == 1


def parse_probe_output(raw: bytes) -> MediaInfo:
    """Parse `ffprobe -print_format json` output; raise MediaError if malformed."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MediaError("ffprobe returned malformed output (not JSON).") from None
    if not isinstance(data, dict):
        raise MediaError("ffprobe returned malformed output (not a JSON object).")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    if not isinstance(streams, list) or not isinstance(fmt, dict):
        raise MediaError("ffprobe returned malformed output (unexpected structure).")
    streams = [stream for stream in streams if isinstance(stream, dict)]
    if not streams:
        raise MediaError("The file contains no media streams.")

    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not _is_attached_picture(s)
        ),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _positive_float(fmt.get("duration"))
    for stream in (video, audio):
        if duration is None and stream is not None:
            duration = _positive_float(stream.get("duration"))

    fps = None
    if video is not None:
        fps = _frame_rate(video.get("avg_frame_rate")) or _frame_rate(
            video.get("r_frame_rate")
        )
    format_name = fmt.get("format_name")
    return MediaInfo(
        duration_seconds=duration,
        width=_dimension(video.get("width")) if video else None,
        height=_dimension(video.get("height")) if video else None,
        fps=fps,
        has_video=video is not None,
        has_audio=audio is not None,
        video_codec=_codec(video.get("codec_name")) if video else None,
        audio_codec=_codec(audio.get("codec_name")) if audio else None,
        format_name=format_name[:100] if isinstance(format_name, str) else None,
    )


# Public operations


def probe(
    path: str | os.PathLike[str],
    *,
    timeout: float | None = None,
    settings: Settings | None = None,
) -> MediaInfo:
    """Return the duration, dimensions, frame rate, streams and codecs of a file.

    Uses `ffprobe -print_format json`. Raises MediaError if the file is
    missing or not a media file ffprobe can read, MediaTimeoutError after
    `timeout` (default FFPROBE_TIMEOUT_SECONDS) and FFmpegNotFoundError if
    ffprobe is not installed.
    """
    settings = settings or get_settings()
    source = _input_file(path)
    if timeout is None:
        timeout = settings.FFPROBE_TIMEOUT_SECONDS
    command = [
        ffprobe_binary(settings),
        "-v",
        "error",
        "-protocol_whitelist",
        INPUT_PROTOCOLS,
        "-print_format",
        "json",
        "-show_entries",
        PROBE_ENTRIES,
        "-i",
        source,
    ]
    result = _run(
        command,
        timeout=timeout,
        settings=settings,
        max_stdout_bytes=MAX_PROBE_OUTPUT_BYTES,
        tool="ffprobe",
    )
    if result.returncode != 0:
        raise MediaError(
            f"ffprobe could not read the file (exit code {result.returncode}).",
            result.stderr,
        )
    return parse_probe_output(result.stdout)


def extract_audio(
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    *,
    timeout: float | None = None,
    settings: Settings | None = None,
) -> Path:
    """Write the audio of `src` to `dst` as mono 16-bit PCM WAV and return `dst`.

    `dst` is overwritten if it exists; a partial file is removed on failure.
    Raises MediaError if `src` has no audio or cannot be decoded.
    """
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE
    ):
        raise ValueError(
            f"sample_rate must be an integer between {MIN_SAMPLE_RATE} and {MAX_SAMPLE_RATE}."
        )
    source = _input_file(src)
    target = Path(dst).resolve()
    if target.is_dir():
        raise ValueError("dst must be a file path, not a directory.")
    if str(target) == source:
        raise ValueError("dst must differ from src.")
    try:
        run_ffmpeg(
            [
                "-protocol_whitelist",
                INPUT_PROTOCOLS,
                "-i",
                source,
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                "-f",
                "wav",
                "-y",
                str(target),
            ],
            timeout=timeout,
            settings=settings,
        )
        if not target.is_file() or target.stat().st_size == 0:
            raise MediaError("ffmpeg did not produce an audio file.")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


__all__ = [
    "FFmpegNotFoundError",
    "MediaError",
    "MediaInfo",
    "MediaTimeoutError",
    "ProcessResult",
    "extract_audio",
    "ffmpeg_binary",
    "ffprobe_binary",
    "parse_probe_output",
    "probe",
    "run_ffmpeg",
    "truncate_stderr",
]
