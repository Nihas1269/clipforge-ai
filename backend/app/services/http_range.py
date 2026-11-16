"""Parsing of the HTTP `Range` request header (RFC 9110, section 14).

Only single byte ranges are served as 206 Partial Content. A request for
several ranges is valid but is answered with the whole file (200), which the
RFC allows; every other malformed or unsatisfiable range is rejected so the
API answers 416 with `Content-Range: bytes */<size>`.
"""

import re
from dataclasses import dataclass

# Longer headers are rejected rather than parsed.
MAX_RANGE_HEADER_LENGTH = 1024
# Byte positions of at most 19 digits always fit in a signed 64-bit integer.
_RANGE_SPEC_RE = re.compile(r"(\d{0,19})-(\d{0,19})")


class RangeNotSatisfiableError(ValueError):
    """The Range header is malformed or selects no byte of the representation."""


@dataclass(frozen=True)
class ByteRange:
    """An inclusive byte range `start`-`end` within a representation."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def content_range(self, size: int) -> str:
        return f"bytes {self.start}-{self.end}/{size}"


def _parse_spec(spec: str, size: int) -> ByteRange:
    match = _RANGE_SPEC_RE.fullmatch(spec)
    if match is None:
        raise RangeNotSatisfiableError("Malformed byte range.")
    first, last = match.groups()
    if not first and not last:
        raise RangeNotSatisfiableError("Malformed byte range.")

    if not first:
        # Suffix range: the last N bytes.
        suffix = int(last)
        if suffix == 0 or size == 0:
            raise RangeNotSatisfiableError("The range selects no bytes.")
        return ByteRange(max(0, size - suffix), size - 1)

    start = int(first)
    end = int(last) if last else size - 1
    if last and end < start:
        raise RangeNotSatisfiableError("The range ends before it starts.")
    if start >= size:
        raise RangeNotSatisfiableError("The range starts after the end of the file.")
    return ByteRange(start, min(end, size - 1))


def parse_range_header(header: str | None, size: int) -> ByteRange | None:
    """Return the requested byte range of a `size`-byte file.

    Returns None when the whole file should be sent: there is no Range header,
    or it asks for several ranges. Raises RangeNotSatisfiableError when the
    header is malformed, uses a unit other than `bytes`, or selects no byte of
    the file (for example a start at or past the end, or `bytes=-0`). An end
    past the last byte is clipped to the last byte.
    """
    if header is None:
        return None
    if size < 0:
        raise ValueError("size must not be negative.")
    if len(header) > MAX_RANGE_HEADER_LENGTH:
        raise RangeNotSatisfiableError("The Range header is too long.")

    unit, separator, ranges = header.strip().partition("=")
    if not separator or unit.strip().lower() != "bytes":
        raise RangeNotSatisfiableError("Only byte ranges are supported.")

    specs = [spec.strip() for spec in ranges.split(",")]
    if len(specs) == 1:
        return _parse_spec(specs[0], size)

    # Several ranges: each must still be well-formed, but the whole file is
    # sent instead of a multipart/byteranges response.
    satisfiable = False
    for spec in specs:
        try:
            _parse_spec(spec, size)
        except RangeNotSatisfiableError:
            if _RANGE_SPEC_RE.fullmatch(spec) is None or spec == "-":
                raise
            continue
        satisfiable = True
    if not satisfiable:
        raise RangeNotSatisfiableError("No range selects any bytes.")
    return None
