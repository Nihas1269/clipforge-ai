"""Tests for app.media.ffmpeg.

Tests marked requires_ffmpeg generate a tiny video at runtime with
`ffmpeg -f lavfi` (nothing binary is committed) and run the real tools; they
are skipped when ffmpeg/ffprobe are not installed. The others mock
subprocess.run, so they always run.
"""

import json
import subprocess
import sys
import wave

import pytest

from app.core.config import Settings
from app.media import ffmpeg
from app.media.ffmpeg import (
    STDERR_MAX_CHARS,
    FFmpegNotFoundError,
    MediaError,
    MediaInfo,
    MediaTimeoutError,
    extract_audio,
    parse_probe_output,
    probe,
    run_ffmpeg,
    truncate_stderr,
)
from tests.media_helpers import (
    FFMPEG_AVAILABLE,
    SKIP_REASON,
    TEST_DURATION,
    TEST_FPS,
    TEST_HEIGHT,
    TEST_WIDTH,
    make_test_video,
    requires_ffmpeg,
)


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="module")
def test_video(media_dir):
    if not FFMPEG_AVAILABLE:
        pytest.skip(SKIP_REASON)
    return make_test_video(media_dir / "sample.mp4")


# Real FFmpeg


@requires_ffmpeg
def test_probe_returns_duration_dimensions_and_streams(test_video):
    info = probe(test_video)

    assert info.duration_seconds == pytest.approx(TEST_DURATION, abs=0.25)
    assert (info.width, info.height) == (TEST_WIDTH, TEST_HEIGHT)
    assert info.fps == pytest.approx(TEST_FPS, abs=0.01)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.video_codec == "mpeg4"
    assert info.audio_codec == "aac"


@requires_ffmpeg
def test_probe_audio_only_file_has_no_video(media_dir):
    path = make_test_video(media_dir / "audio-only.mp4", with_video=False)

    info = probe(path)

    assert info.has_video is False
    assert info.has_audio is True
    assert info.width is None and info.height is None and info.fps is None


@requires_ffmpeg
def test_extract_audio_writes_mono_wav(test_video, tmp_path):
    dst = tmp_path / "audio.wav"

    result = extract_audio(test_video, dst)

    assert result == dst.resolve()
    with wave.open(str(dst), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2
        assert wav.getnframes() / wav.getframerate() == pytest.approx(TEST_DURATION, abs=0.25)


@requires_ffmpeg
def test_extract_audio_custom_sample_rate(test_video, tmp_path):
    dst = extract_audio(test_video, tmp_path / "audio.wav", sample_rate=8000)

    with wave.open(str(dst), "rb") as wav:
        assert wav.getframerate() == 8000


@requires_ffmpeg
def test_extract_audio_without_audio_track_fails_and_leaves_no_file(media_dir, tmp_path):
    silent = make_test_video(media_dir / "video-only.mp4", with_audio=False)
    dst = tmp_path / "audio.wav"

    with pytest.raises(MediaError):
        extract_audio(silent, dst)

    assert not dst.exists()


@requires_ffmpeg
@pytest.mark.parametrize(
    "content",
    [
        b"this is not a video at all",
        b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 1000,
    ],
)
def test_invalid_file_raises_media_error(tmp_path, content):
    path = tmp_path / "broken.mp4"
    path.write_bytes(content)

    with pytest.raises(MediaError) as excinfo:
        probe(path)

    assert not isinstance(excinfo.value, (FFmpegNotFoundError, MediaTimeoutError))


@requires_ffmpeg
def test_run_ffmpeg_failure_includes_truncated_stderr(tmp_path):
    with pytest.raises(MediaError) as excinfo:
        run_ffmpeg(["-i", str(tmp_path / "missing.mp4"), "-f", "null", "-"])

    assert excinfo.value.stderr
    assert len(excinfo.value.stderr) <= STDERR_MAX_CHARS
    assert excinfo.value.stderr in str(excinfo.value)


@requires_ffmpeg
def test_input_cannot_reach_network_protocols(tmp_path):
    # An HLS playlist pointing at a remote URL: the protocol whitelist stops
    # ffprobe from fetching it.
    playlist = tmp_path / "evil.mp4"
    playlist.write_text(
        "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:9/x.ts\n#EXT-X-ENDLIST\n"
    )

    with pytest.raises(MediaError):
        probe(playlist, timeout=20)


# Missing binaries and input checks (no FFmpeg needed)


def test_missing_ffprobe_binary_raises_actionable_error(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    settings = Settings(_env_file=None, FFPROBE_PATH=str(tmp_path / "no-such-ffprobe"))

    with pytest.raises(FFmpegNotFoundError) as excinfo:
        probe(video, settings=settings)

    assert "FFPROBE_PATH" in str(excinfo.value)
    assert isinstance(excinfo.value, MediaError)


def test_missing_ffmpeg_binary_raises_actionable_error(tmp_path):
    settings = Settings(_env_file=None, FFMPEG_PATH=str(tmp_path / "no-such-ffmpeg"))

    with pytest.raises(FFmpegNotFoundError) as excinfo:
        run_ffmpeg(["-version"], settings=settings)

    assert "FFMPEG_PATH" in str(excinfo.value)


def test_binary_paths_are_configurable(monkeypatch):
    monkeypatch.setenv("FFMPEG_PATH", "/opt/ffmpeg/bin/ffmpeg")
    monkeypatch.setenv("FFPROBE_PATH", "  ")

    settings = Settings()

    assert settings.FFMPEG_PATH == "/opt/ffmpeg/bin/ffmpeg"
    assert settings.FFPROBE_PATH == "ffprobe"


def test_probe_of_missing_file_raises_media_error(tmp_path):
    with pytest.raises(MediaError):
        probe(tmp_path / "missing.mp4")


@pytest.mark.parametrize("args", ["-i input.mp4 out.wav", b"-version", [1, 2], ["a\x00b"]])
def test_run_ffmpeg_requires_an_argument_list(args):
    with pytest.raises((TypeError, ValueError)):
        run_ffmpeg(args)


@pytest.mark.parametrize("rate", [0, 7999, 192001, 16000.0, True, "16000"])
def test_extract_audio_rejects_invalid_sample_rate(tmp_path, rate):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"x")

    with pytest.raises(ValueError):
        extract_audio(src, tmp_path / "out.wav", sample_rate=rate)


# Process handling (subprocess.run mocked)


class FakeRun:
    """Stands in for subprocess.run; records the call and writes fake output."""

    def __init__(self, *, stdout=b"", stderr=b"", returncode=0, raises=None):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.raises = raises
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if self.raises is not None:
            kwargs["stderr"].write(self.stderr)
            raise self.raises
        kwargs["stdout"].write(self.stdout)
        kwargs["stderr"].write(self.stderr)
        return subprocess.CompletedProcess(command, self.returncode)


@pytest.fixture
def fake_tools(monkeypatch):
    monkeypatch.setattr(ffmpeg, "_resolve_binary", lambda configured, setting: f"/bin/{configured}")


@pytest.fixture
def input_file(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"x")
    return path


def _probe_json(**overrides):
    data = {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "30000/1001",
                "r_frame_rate": "30000/1001",
                "disposition": {"attached_pic": 0},
            },
            {"index": 1, "codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "12.5", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
    }
    data.update(overrides)
    return json.dumps(data).encode()


def test_probe_uses_argument_list_without_shell_and_with_timeout(
    monkeypatch, fake_tools, input_file
):
    fake = FakeRun(stdout=_probe_json())
    monkeypatch.setattr(subprocess, "run", fake)

    info = probe(input_file, timeout=12)

    command, kwargs = fake.calls[0]
    assert isinstance(command, list)
    assert command[0] == "/bin/ffprobe"
    assert command[-1] == str(input_file.resolve())
    assert command[command.index("-print_format") + 1] == "json"
    assert command[command.index("-protocol_whitelist") + 1] == "file"
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 12
    assert kwargs["stdin"] is subprocess.DEVNULL
    if sys.platform != "win32":
        assert callable(kwargs["preexec_fn"])
    assert info == MediaInfo(
        duration_seconds=12.5,
        width=1920,
        height=1080,
        fps=pytest.approx(29.97, abs=0.01),
        has_video=True,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
    )


def test_probe_default_timeout_comes_from_settings(monkeypatch, fake_tools, input_file):
    fake = FakeRun(stdout=_probe_json())
    monkeypatch.setattr(subprocess, "run", fake)

    probe(input_file, settings=Settings(_env_file=None, FFPROBE_TIMEOUT_SECONDS=7))

    assert fake.calls[0][1]["timeout"] == 7


def test_run_ffmpeg_prepends_safe_options_and_uses_configured_timeout(monkeypatch, fake_tools):
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)

    run_ffmpeg(["-i", "in.mp4", "out.wav"], settings=Settings(_env_file=None, FFMPEG_TIMEOUT_SECONDS=5))

    command, kwargs = fake.calls[0]
    assert command == [
        "/bin/ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "in.mp4",
        "out.wav",
    ]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 5


def test_nonzero_exit_raises_media_error_with_truncated_stderr(monkeypatch, fake_tools, input_file):
    stderr = ("noise\n" * 2000 + "moov atom not found\x1b[0m").encode()
    monkeypatch.setattr(subprocess, "run", FakeRun(stderr=stderr, returncode=1))

    with pytest.raises(MediaError) as excinfo:
        probe(input_file)

    error = excinfo.value
    assert len(error.stderr) <= STDERR_MAX_CHARS
    assert error.stderr.endswith("moov atom not found [0m")
    assert "\x1b" not in str(error)
    assert "moov atom not found" in str(error)


def test_timeout_raises_media_timeout_error(monkeypatch, fake_tools, input_file):
    fake = FakeRun(stderr=b"still working", raises=subprocess.TimeoutExpired(["ffprobe"], 3))
    monkeypatch.setattr(subprocess, "run", fake)

    with pytest.raises(MediaTimeoutError) as excinfo:
        probe(input_file, timeout=3)

    assert "3 seconds" in str(excinfo.value)
    assert excinfo.value.stderr == "still working"


def test_binary_vanishing_before_start_raises_not_found(monkeypatch, fake_tools, input_file):
    monkeypatch.setattr(subprocess, "run", FakeRun(raises=FileNotFoundError(2, "gone")))

    with pytest.raises(FFmpegNotFoundError):
        probe(input_file)


def test_oversized_probe_output_is_rejected(monkeypatch, fake_tools, input_file):
    monkeypatch.setattr(ffmpeg, "MAX_PROBE_OUTPUT_BYTES", 100)
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout=_probe_json()))

    with pytest.raises(MediaError, match="more output"):
        probe(input_file)


def test_malformed_probe_output_raises_media_error(monkeypatch, fake_tools, input_file):
    monkeypatch.setattr(subprocess, "run", FakeRun(stdout=b"{not json"))

    with pytest.raises(MediaError, match="malformed"):
        probe(input_file)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff\xfe",
        b"[]",
        b'{"streams": {}}',
        b'{"streams": [], "format": {}}',
        b'{"streams": ["x"], "format": []}',
    ],
)
def test_parse_probe_output_rejects_malformed_json(raw):
    with pytest.raises(MediaError):
        parse_probe_output(raw)


def test_parse_probe_output_ignores_nonsense_values():
    info = parse_probe_output(
        json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264; rm -rf /",
                        "width": 0,
                        "height": "1080",
                        "avg_frame_rate": "0/0",
                        "r_frame_rate": "1000000/1",
                    }
                ],
                "format": {"duration": "nan"},
            }
        ).encode()
    )

    assert info.has_video is True
    assert info.duration_seconds is None
    assert info.width is None
    assert info.height is None
    assert info.fps is None
    assert info.video_codec is None


def test_parse_probe_output_falls_back_to_stream_duration_and_r_frame_rate():
    info = parse_probe_output(
        json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "width": 640,
                        "height": 360,
                        "avg_frame_rate": "0/0",
                        "r_frame_rate": "24/1",
                        "duration": "3.0",
                    }
                ],
                "format": {},
            }
        ).encode()
    )

    assert info.duration_seconds == 3.0
    assert info.fps == 24.0
    assert info.has_audio is False


def test_cover_art_is_not_a_video_stream():
    info = parse_probe_output(
        json.dumps(
            {
                "streams": [
                    {"codec_type": "audio", "codec_name": "mp3"},
                    {
                        "codec_type": "video",
                        "codec_name": "mjpeg",
                        "width": 500,
                        "height": 500,
                        "disposition": {"attached_pic": 1},
                    },
                ],
                "format": {"duration": "180.0"},
            }
        ).encode()
    )

    assert info.has_video is False
    assert info.has_audio is True


def test_truncate_stderr_keeps_the_end():
    text = truncate_stderr("a" * 5000 + "the real error", max_chars=50)

    assert len(text) == 50
    assert text.startswith("...")
    assert text.endswith("the real error")
