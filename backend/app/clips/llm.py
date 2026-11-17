"""An optional clip detector that asks an LLM for engaging moments.

The transcript is split into chunks (see plan_chunks) and each chunk is sent
to an LLMClient with the prompt from app/clips/llm_prompt.py, asking for a
strict JSON array of {start_seconds, end_seconds, title, reason, score}.

Validation of an answer (parse_clip_response):

- The whole answer is rejected (LLMOutputError) when it is not valid JSON
  (NaN and Infinity are not allowed), is not a JSON array, has more than
  MAX_RESPONSE_ITEMS items, or is a non-empty array none of whose items is
  valid. A single surrounding Markdown code fence is tolerated. A rejected
  answer is requested once more; a second rejection fails the detection.
- Otherwise every item is checked on its own against LLMClipItem: exactly the
  five keys, numeric finite times with 0 <= start < end, a score in [0, 1],
  and a non-empty title (at most TITLE_MAX_LENGTH characters) and reason (at
  most REASON_MAX_LENGTH) without control characters. An item that fails, or
  that lies outside its chunk, is discarded as a whole; nothing is clamped,
  truncated or repaired.

The remaining items of all chunks go through validate_candidates(), which
drops clips outside the video or the length limits and overlapping
(duplicate) clips, and keeps the best `max_clips`. Every clip therefore
satisfies 0 <= start < end <= video duration.

Provider errors (HTTP errors, timeouts, network errors) are never retried
here. Whenever the LLM path fails, or no client is configured, the detector
logs a warning and returns the fallback (heuristic) detector's result. Each
detect() call stores a ClipDetectionRun in `last_run` and logs it: detector
used, whether the fallback was used and why, provider, model, prompt version,
request count, token usage and estimated cost when known. Transcript text,
prompts, answers and API keys are never logged.
"""

import json
import logging
import math
import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from app.clips.base import (
    TIME_TOLERANCE_SECONDS,
    ClipCandidate,
    ClipDetectionOptions,
    ClipDetector,
    ClipSegment,
    InvalidClipCandidateError,
    check_video_duration,
    validate_candidates,
)
from app.clips.heuristic import HeuristicClipDetector
from app.clips.llm_clients import (
    LLMClient,
    LLMConfigurationError,
    LLMError,
    LLMOutputError,
    LLMUsage,
    get_llm_client,
)
from app.clips.llm_prompt import (
    PROMPT_VERSION,
    REASON_MAX_LENGTH,
    RETRY_NOTE,
    SYSTEM_PROMPT,
    build_user_prompt,
    render_segment,
)
from app.core.config import Settings, get_settings
from app.models.clip import TITLE_MAX_LENGTH

__all__ = [
    "ClipDetectionRun",
    "LLMClient",
    "LLMClipDetector",
    "LLMClipItem",
    "TranscriptChunk",
    "parse_clip_response",
    "plan_chunks",
]

logger = logging.getLogger(__name__)

# Two attempts per chunk: the first answer and one retry for malformed output.
MAX_ATTEMPTS = 2
# More chunks than this fails the LLM path (cost guard); raise
# LLM_MAX_PROMPT_CHARS for very long transcripts.
MAX_CHUNKS = 50
# Upper bound of items in one answer; the prompt asks for at most max_clips.
MAX_RESPONSE_ITEMS = 100
# Longest answer text that is parsed.
MAX_ANSWER_CHARS = 200_000
# Room left in the prompt for the numbers in the template.
PROMPT_SLACK_CHARS = 64
# A segment line is never shortened below this many characters of text.
MIN_SEGMENT_TEXT_CHARS = 16
TRUNCATION_MARK = "..."

_FENCE = re.compile(r"\A```(?:json|JSON)?[ \t]*\n(.*)\n[ \t]*```\Z", re.DOTALL)


def _clean_text(text: str) -> str:
    # Control and formatting characters removed, whitespace collapsed.
    text = "".join(
        " " if unicodedata.category(char).startswith("C") else char for char in text
    )
    return " ".join(text.split())


def _has_control_characters(text: str) -> bool:
    return any(unicodedata.category(char) == "Cc" for char in text)


def _check_number(value: Any) -> Any:
    # bool is an int subclass, but True is never a time or score; strings are
    # never converted to numbers.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("must be a number")
    return value


class LLMClipItem(BaseModel):
    """One clip suggestion in the model's answer, validated strictly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    title: StrictStr = Field(min_length=1, max_length=TITLE_MAX_LENGTH)
    reason: StrictStr = Field(min_length=1, max_length=REASON_MAX_LENGTH)
    score: float = Field(ge=0, le=1, allow_inf_nan=False)

    @field_validator("start_seconds", "end_seconds", "score", mode="before")
    @classmethod
    def _numbers_only(cls, value: Any) -> Any:
        return _check_number(value)

    @field_validator("title", "reason", mode="before")
    @classmethod
    def _strip(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("title", "reason", mode="after")
    @classmethod
    def _no_control_characters(cls, value: str) -> str:
        if _has_control_characters(value):
            raise ValueError("must not contain control characters")
        return value

    @model_validator(mode="after")
    def _end_after_start(self) -> "LLMClipItem":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def parse_clip_response(text: str) -> tuple[list[LLMClipItem], int]:
    """Return the valid items of an answer and the number of discarded items.

    Raises LLMOutputError when the answer as a whole is unusable (see the
    module docstring); the message never contains the answer.
    """
    if not isinstance(text, str):
        raise LLMOutputError("The LLM answer is not text.")
    if len(text) > MAX_ANSWER_CHARS:
        raise LLMOutputError("The LLM answer is too long.")
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        data = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise LLMOutputError("The LLM answer is not valid JSON.") from None
    if not isinstance(data, list):
        raise LLMOutputError("The LLM answer is not a JSON array.")
    if len(data) > MAX_RESPONSE_ITEMS:
        raise LLMOutputError(
            f"The LLM answer has more than {MAX_RESPONSE_ITEMS} items."
        )
    items: list[LLMClipItem] = []
    discarded = 0
    for raw in data:
        try:
            items.append(LLMClipItem.model_validate(raw))
        except ValidationError:
            discarded += 1
    if data and not items:
        raise LLMOutputError("No item of the LLM answer matches the clip schema.")
    return items, discarded


@dataclass(frozen=True)
class TranscriptChunk:
    """Consecutive whole segments sent in one request, in absolute video time."""

    start: float
    end: float
    lines: tuple[str, ...]


@dataclass(frozen=True)
class _PreparedSegment:
    start: float
    end: float
    text: str


def _prepare(
    segments: Sequence[ClipSegment], duration: float
) -> list[_PreparedSegment]:
    prepared: list[tuple[float, float, int, _PreparedSegment]] = []
    for position, segment in enumerate(segments):
        start, end, text = segment.start, segment.end, segment.text
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not isinstance(text, str)
        ):
            continue
        start, end = float(start), float(end)
        if not (math.isfinite(start) and math.isfinite(end)):
            continue
        if start < 0 or end <= start or end > duration + TIME_TOLERANCE_SECONDS:
            continue
        text = _clean_text(text)
        if not text:
            continue
        item = _PreparedSegment(start=start, end=min(end, duration), text=text)
        prepared.append((item.start, item.end, position, item))
    prepared.sort(key=lambda entry: entry[:3])
    return [entry[3] for entry in prepared]


def _fit_line(segment: _PreparedSegment, available: int) -> tuple[str, bool]:
    """The segment's line, its text shortened if the line alone exceeds `available`."""
    line = render_segment(segment.start, segment.end, segment.text)
    if len(line) + 1 <= available:
        return line, False
    text = segment.text
    while len(line) + 1 > available and len(text) > MIN_SEGMENT_TEXT_CHARS:
        # Escaping can make a line longer than its text, so shrink until it fits.
        excess = len(line) + 1 - available
        keep = max(
            MIN_SEGMENT_TEXT_CHARS, len(text) - excess - len(TRUNCATION_MARK) - 8
        )
        text = text[:keep]
        line = render_segment(segment.start, segment.end, text + TRUNCATION_MARK)
    if len(line) + 1 > available:
        raise LLMConfigurationError(
            "LLM_MAX_PROMPT_CHARS is too small for a single transcript segment.",
            category="prompt_budget",
        )
    return line, True


def _prompt_overhead(duration: float, options: ClipDetectionOptions) -> int:
    empty = build_user_prompt(
        [],
        chunk_start=duration,
        chunk_end=duration,
        video_duration=duration,
        options=options,
    )
    return len(empty) + len(RETRY_NOTE) + PROMPT_SLACK_CHARS


def plan_chunks(
    segments: Sequence[ClipSegment],
    video_duration: float,
    options: ClipDetectionOptions,
    max_prompt_chars: int,
) -> tuple[list[TranscriptChunk], int]:
    """Split the transcript into chunks whose prompt fits `max_prompt_chars`.

    Chunks hold consecutive whole segments in time order with their absolute
    timestamps, and do not overlap. Only a segment too long for a prompt on
    its own is shortened (its text is cut and marked with "..."). Segments
    with invalid times, ending past the video or without text are skipped.
    Returns the chunks and the number of shortened segments; raises
    LLMError when there would be more than MAX_CHUNKS chunks or the budget
    cannot hold one segment.
    """
    duration = check_video_duration(video_duration)
    available = max_prompt_chars - _prompt_overhead(duration, options)
    if available < MIN_SEGMENT_TEXT_CHARS * 4:
        raise LLMConfigurationError(
            "LLM_MAX_PROMPT_CHARS is too small for the clip detection prompt.",
            category="prompt_budget",
        )
    chunks: list[TranscriptChunk] = []
    truncated = 0
    lines: list[str] = []
    used = 0
    chunk_start = chunk_end = 0.0
    for segment in _prepare(segments, duration):
        line, shortened = _fit_line(segment, available)
        truncated += shortened
        size = len(line) + 1
        if lines and used + size > available:
            chunks.append(TranscriptChunk(chunk_start, chunk_end, tuple(lines)))
            lines, used = [], 0
            if len(chunks) >= MAX_CHUNKS:
                # Another chunk is about to start.
                raise LLMError(
                    f"The transcript needs more than {MAX_CHUNKS} LLM requests.",
                    category="too_many_chunks",
                )
        if not lines:
            chunk_start, chunk_end = segment.start, segment.end
        lines.append(line)
        used += size
        chunk_end = max(chunk_end, segment.end)
    if lines:
        chunks.append(TranscriptChunk(chunk_start, chunk_end, tuple(lines)))
    return chunks, truncated


@dataclass(frozen=True)
class ClipDetectionRun:
    """What one detect() call did; safe to log (no content, no secrets).

    `detector` is the detector whose clips were returned ("llm" or the
    fallback's name). Token counts are sums over the requests that reported
    them (None if none did); `estimated_cost_usd` is set only when every
    request reported usage and both prices are configured.
    """

    detector: str
    fallback_used: bool
    failure_category: str | None
    provider: str | None
    model: str | None
    prompt_version: str
    chunks: int
    requests: int
    truncated_segments: int
    discarded_items: int
    input_tokens: int | None
    output_tokens: int | None
    estimated_cost_usd: float | None
    duration_ms: int
    clips: int

    def as_log_fields(self) -> dict[str, Any]:
        return dict(self.__dict__)


class _RunStats:
    def __init__(self) -> None:
        self.chunks = 0
        self.requests = 0
        self.truncated_segments = 0
        self.discarded_items = 0
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.usage_complete = True

    def add_usage(self, usage: LLMUsage | None) -> None:
        if usage is None or usage.input_tokens is None or usage.output_tokens is None:
            self.usage_complete = False
        if usage is None:
            return
        if usage.input_tokens is not None:
            self.input_tokens = (self.input_tokens or 0) + usage.input_tokens
        if usage.output_tokens is not None:
            self.output_tokens = (self.output_tokens or 0) + usage.output_tokens


class LLMClipDetector:
    """A ClipDetector backed by an LLMClient, falling back to another detector.

    `client` None means the LLM is not configured: every detection uses the
    fallback (default: HeuristicClipDetector) and records why.
    """

    name = "llm"

    def __init__(
        self,
        client: LLMClient | None,
        *,
        fallback: ClipDetector | None = None,
        max_prompt_chars: int = 24000,
        unavailable_category: str = "configuration",
        input_cost_per_million_tokens: float | None = None,
        output_cost_per_million_tokens: float | None = None,
    ) -> None:
        self.client = client
        self.fallback = fallback if fallback is not None else HeuristicClipDetector()
        self.max_prompt_chars = max_prompt_chars
        self.unavailable_category = unavailable_category
        self.input_cost_per_million_tokens = input_cost_per_million_tokens
        self.output_cost_per_million_tokens = output_cost_per_million_tokens
        self.provider: str | None = getattr(client, "provider", None)
        self.model: str | None = getattr(client, "model", None)
        self.last_run: ClipDetectionRun | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        fallback: ClipDetector | None = None,
        **client_overrides: Any,
    ) -> "LLMClipDetector":
        """Create the detector from settings; never raises for missing LLM settings.

        A missing or invalid LLM configuration is logged as a warning (setting
        names only) and every detection then uses the fallback.
        """
        settings = settings or get_settings()
        if fallback is None:
            fallback = HeuristicClipDetector(keywords=settings.CLIP_KEYWORDS)
        client: LLMClient | None
        try:
            client = get_llm_client(settings, **client_overrides)
        except LLMConfigurationError as exc:
            logger.warning(
                "llm_clip_detector_unconfigured",
                extra={
                    "provider": settings.LLM_PROVIDER,
                    "category": exc.category,
                    "error": str(exc),
                },
            )
            client = None
        detector = cls(
            client,
            fallback=fallback,
            max_prompt_chars=settings.LLM_MAX_PROMPT_CHARS,
            input_cost_per_million_tokens=settings.LLM_INPUT_COST_PER_MILLION_TOKENS,
            output_cost_per_million_tokens=settings.LLM_OUTPUT_COST_PER_MILLION_TOKENS,
        )
        if client is None:
            detector.provider = settings.LLM_PROVIDER
            detector.model = settings.LLM_MODEL
        return detector

    def detect(
        self,
        segments: Sequence[ClipSegment],
        video_duration: float,
        options: ClipDetectionOptions,
    ) -> list[ClipCandidate]:
        duration = check_video_duration(video_duration)
        if not isinstance(options, ClipDetectionOptions):
            raise TypeError("options must be ClipDetectionOptions.")
        started = time.monotonic()
        stats = _RunStats()
        if self.client is None:
            return self._fall_back(
                segments, duration, options, stats, started, self.unavailable_category
            )
        try:
            clips = self._detect_with_llm(segments, duration, options, stats)
        except LLMError as exc:
            return self._fall_back(
                segments, duration, options, stats, started, exc.category
            )
        except Exception as exc:  # noqa: BLE001 - any LLM failure falls back
            logger.warning(
                "llm_clip_detection_unexpected_error",
                extra={"error_type": type(exc).__name__},
            )
            return self._fall_back(
                segments, duration, options, stats, started, "unexpected"
            )
        self._record(self.name, None, stats, started, len(clips))
        return clips

    def _detect_with_llm(
        self,
        segments: Sequence[ClipSegment],
        duration: float,
        options: ClipDetectionOptions,
        stats: _RunStats,
    ) -> list[ClipCandidate]:
        chunks, stats.truncated_segments = plan_chunks(
            segments, duration, options, self.max_prompt_chars
        )
        stats.chunks = len(chunks)
        candidates: list[ClipCandidate] = []
        for index, chunk in enumerate(chunks):
            user = build_user_prompt(
                chunk.lines,
                chunk_start=chunk.start,
                chunk_end=chunk.end,
                video_duration=duration,
                options=options,
            )
            for item in self._request_items(index, user, stats):
                if (
                    item.start_seconds < chunk.start - TIME_TOLERANCE_SECONDS
                    or item.end_seconds > chunk.end + TIME_TOLERANCE_SECONDS
                ):
                    stats.discarded_items += 1
                    continue
                try:
                    candidates.append(
                        ClipCandidate(
                            start=item.start_seconds,
                            end=item.end_seconds,
                            score=item.score,
                            title=item.title,
                            reason=item.reason,
                        )
                    )
                except InvalidClipCandidateError:
                    stats.discarded_items += 1
        return validate_candidates(candidates, duration, options)

    def _request_items(
        self, index: int, user: str, stats: _RunStats
    ) -> list[LLMClipItem]:
        assert self.client is not None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            prompt = user if attempt == 1 else user + RETRY_NOTE
            stats.requests += 1
            try:
                try:
                    text = self.client.complete_json(SYSTEM_PROMPT, prompt)
                finally:
                    stats.add_usage(getattr(self.client, "last_usage", None))
                items, discarded = parse_clip_response(text)
            except LLMOutputError as exc:
                # Only malformed output is retried; provider errors propagate.
                logger.warning(
                    "llm_clip_output_invalid",
                    extra={
                        "chunk_index": index,
                        "attempt": attempt,
                        "will_retry": attempt < MAX_ATTEMPTS,
                        "error": str(exc),
                    },
                )
                if attempt >= MAX_ATTEMPTS:
                    raise
                continue
            stats.discarded_items += discarded
            return items
        raise AssertionError("unreachable")  # pragma: no cover

    def _fall_back(
        self,
        segments: Sequence[ClipSegment],
        duration: float,
        options: ClipDetectionOptions,
        stats: _RunStats,
        started: float,
        category: str,
    ) -> list[ClipCandidate]:
        logger.warning(
            "clip_detection_fallback",
            extra={
                "detector": self.name,
                "fallback": self.fallback.name,
                "provider": self.provider,
                "model": self.model,
                "category": category,
            },
        )
        clips = self.fallback.detect(segments, duration, options)
        self._record(self.fallback.name, category, stats, started, len(clips))
        return clips

    def _estimated_cost(self, stats: _RunStats) -> float | None:
        if (
            self.input_cost_per_million_tokens is None
            or self.output_cost_per_million_tokens is None
            or not stats.requests
            or not stats.usage_complete
            or stats.input_tokens is None
            or stats.output_tokens is None
        ):
            return None
        cost = (
            stats.input_tokens * self.input_cost_per_million_tokens
            + stats.output_tokens * self.output_cost_per_million_tokens
        ) / 1_000_000
        return round(cost, 6)

    def _record(
        self,
        detector: str,
        failure_category: str | None,
        stats: _RunStats,
        started: float,
        clips: int,
    ) -> None:
        self.last_run = ClipDetectionRun(
            detector=detector,
            fallback_used=failure_category is not None,
            failure_category=failure_category,
            provider=self.provider,
            model=self.model,
            prompt_version=PROMPT_VERSION,
            chunks=stats.chunks,
            requests=stats.requests,
            truncated_segments=stats.truncated_segments,
            discarded_items=stats.discarded_items,
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
            estimated_cost_usd=self._estimated_cost(stats),
            duration_ms=round((time.monotonic() - started) * 1000),
            clips=clips,
        )
        logger.info("llm_clip_detection_run", extra=self.last_run.as_log_fields())
