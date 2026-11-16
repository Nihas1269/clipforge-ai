"""Validation helpers for uploaded video files.

This is format validation only: it checks that the file name, the declared
content type and the first bytes of the file agree on a supported container.
It is not malware scanning and does not prove the rest of the file is a
playable video.
"""

import unicodedata

from app.models.video import FILENAME_MAX_LENGTH

# Container families recognized by their file signature.
ISO_BMFF = "iso-bmff"  # MP4 and QuickTime MOV
EBML = "ebml"  # WebM and Matroska

# Extensions with a known signature, and the content type stored for each.
# The stored content type is derived from the validated extension, never
# copied from the client.
EXTENSION_CONTAINERS = {
    ".mp4": ISO_BMFF,
    ".mov": ISO_BMFF,
    ".webm": EBML,
    ".mkv": EBML,
}
CANONICAL_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
}
CONTENT_TYPE_CONTAINERS = {
    "video/mp4": ISO_BMFF,
    "video/quicktime": ISO_BMFF,
    "video/webm": EBML,
    "video/x-matroska": EBML,
    "video/matroska": EBML,
}

# Number of leading bytes needed by detect_container().
SNIFF_BYTES = 12

_EBML_MAGIC = b"\x1a\x45\xdf\xa3"
_FTYP = b"ftyp"
# Longest extension kept when a long file name is shortened.
_MAX_KEPT_EXTENSION_LENGTH = 16


def detect_container(header: bytes) -> str | None:
    """Return the container family identified by the file's first bytes.

    MP4/MOV files start with an ISO BMFF `ftyp` box (4-byte size, then
    "ftyp"); WebM/MKV files start with the EBML header magic 1A 45 DF A3.
    """
    if len(header) >= 8 and header[4:8] == _FTYP:
        if int.from_bytes(header[:4], "big") >= 8:
            return ISO_BMFF
    if header.startswith(_EBML_MAGIC):
        return EBML
    return None


def sanitize_filename(raw: str | None, max_length: int = FILENAME_MAX_LENGTH) -> str:
    """Return a display-safe version of a client-supplied file name.

    Directory components (with "/" or "\\" separators), control and
    formatting characters (including bidirectional overrides) and surrounding
    whitespace are removed, and the result is at most max_length characters,
    keeping the extension. The result is for display only and must never be
    used as a storage path or a command argument. It is empty when nothing
    usable remains.
    """
    if not raw:
        return ""
    name = unicodedata.normalize("NFC", raw)
    name = "".join(ch for ch in name if not unicodedata.category(ch).startswith("C"))
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if name in {".", ".."}:
        return ""
    if len(name) > max_length:
        stem, dot, suffix = name.rpartition(".")
        ext = f"{dot}{suffix}" if dot and len(suffix) < _MAX_KEPT_EXTENSION_LENGTH else ""
        if not stem:
            ext = ""
        name = name[: len(name) - len(ext)][: max_length - len(ext)].rstrip() + ext
    return name


def file_extension(filename: str) -> str:
    """Return the lower-cased extension of filename (".mp4"), or ""."""
    _, dot, suffix = filename.rpartition(".")
    if not dot or not suffix:
        return ""
    return f".{suffix.lower()}"


def normalize_content_type(raw: str | None) -> str:
    """Return the media type without parameters, lower-cased ("" if missing)."""
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip().lower()
