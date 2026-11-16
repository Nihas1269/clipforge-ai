"""Shared helpers for tests that run the real FFmpeg tools.

Test videos are generated at runtime with `ffmpeg -f lavfi`; no binary media
files are committed. Tests that need the tools use `requires_ffmpeg`, which
skips them with a clear reason when ffmpeg or ffprobe is not installed.
"""

import shutil

import pytest

from app.core.config import Settings
from app.media.ffmpeg import run_ffmpeg

_TOOL_SETTINGS = Settings(_env_file=None)
FFMPEG_AVAILABLE = (
    shutil.which(_TOOL_SETTINGS.FFMPEG_PATH) is not None
    and shutil.which(_TOOL_SETTINGS.FFPROBE_PATH) is not None
)
SKIP_REASON = (
    "ffmpeg/ffprobe binaries not found (install FFmpeg or set FFMPEG_PATH and "
    "FFPROBE_PATH); skipping tests that run real FFmpeg."
)
requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason=SKIP_REASON)

TEST_DURATION = 2.0
TEST_WIDTH = 320
TEST_HEIGHT = 240
TEST_FPS = 25


def make_test_video(path, *, duration=TEST_DURATION, with_video=True, with_audio=True):
    """Generate a small MP4 (test pattern and/or sine tone) with ffmpeg lavfi."""
    args = []
    if with_video:
        args += [
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={duration}:size={TEST_WIDTH}x{TEST_HEIGHT}:rate={TEST_FPS}",
        ]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    if with_video:
        args += ["-c:v", "mpeg4", "-pix_fmt", "yuv420p"]
    if with_audio:
        args += ["-c:a", "aac"]
    args += ["-shortest", "-y", str(path)]
    run_ffmpeg(args, timeout=60, settings=_TOOL_SETTINGS)
    return path
