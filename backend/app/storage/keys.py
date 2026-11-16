"""Helpers that build storage keys."""

import re

from app.storage.base import InvalidStorageKeyError, validate_key

# User and video IDs are integers or identifiers such as UUIDs.
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# An empty extension, or "." followed by 1-10 letters or digits.
_EXT_RE = re.compile(r"(\.[A-Za-z0-9]{1,10})?")


def _validate_id(name: str, value: int | str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise InvalidStorageKeyError(f"{name} must be an int or a string.")
    text = str(value)
    if not _ID_RE.fullmatch(text):
        raise InvalidStorageKeyError(f"{name} contains invalid characters.")
    return text


def video_source_key(user_id: int | str, video_id: int | str, ext: str = "") -> str:
    """Return the key of a video's uploaded source file.

    For example video_source_key(1, 42, ".MP4") == "users/1/videos/42/source.mp4".
    The extension is lower-cased and must be empty or "." plus letters/digits.
    """
    user = _validate_id("user_id", user_id)
    video = _validate_id("video_id", video_id)
    if not isinstance(ext, str) or not _EXT_RE.fullmatch(ext):
        raise InvalidStorageKeyError(
            "ext must be empty or '.' followed by letters or digits."
        )
    return validate_key(f"users/{user}/videos/{video}/source{ext.lower()}")
