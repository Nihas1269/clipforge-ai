import json
import logging

import httpx
import pytest

from app.clips import ClipDetector, LLMClipDetector
from app.clips.llm_prompt import PROMPT_VERSION, RETRY_NOTE
from tests.llm_helpers import (
    OPTIONS,
    FakeClient,
    anthropic_client,
    anthropic_reply,
    assert_within_video,
    heuristic,
    item,
    openai_client,
    openai_reply,
    transcript,
)

# --- Detector -----------------------------------------------------------------


def test_llm_detector_implements_protocol():
    assert isinstance(LLMClipDetector(FakeClient("[]")), ClipDetector)


def test_valid_json_produces_candidates_openai():
    def handler(request):
        return openai_reply(json.dumps([item(10, 30, score=0.9, title=" Hook ")]))

    detector = LLMClipDetector(openai_client(handler), fallback=heuristic())
    clips = detector.detect(transcript(), 120.0, OPTIONS)

    assert [(c.start, c.end, c.score, c.title) for c in clips] == [
        (10.0, 30.0, 0.9, "Hook")
    ]
    run = detector.last_run
    assert run.detector == "llm"
    assert run.fallback_used is False and run.failure_category is None
    assert run.provider == "openai_compatible" and run.model == "test-model"
    assert run.prompt_version == PROMPT_VERSION
    assert (run.chunks, run.requests) == (1, 1)
    assert (run.input_tokens, run.output_tokens) == (120, 30)
    assert run.estimated_cost_usd is None


def test_valid_json_produces_candidates_anthropic():
    def handler(request):
        return anthropic_reply(json.dumps([item(60, 85)]))

    detector = LLMClipDetector(anthropic_client(handler), fallback=heuristic())
    clips = detector.detect(transcript(), 120.0, OPTIONS)
    assert [(c.start, c.end) for c in clips] == [(60.0, 85.0)]
    assert detector.last_run.provider == "anthropic"
    assert detector.last_run.input_tokens == 90


def test_out_of_range_and_overlapping_items_are_dropped():
    answer = json.dumps(
        [
            item(10, 30, score=0.9),
            item(20, 40, score=0.5),  # overlaps the better clip
            item(10, 30, score=0.4),  # duplicate
            item(100, 125),  # past the end of the video
            item(-5, 15),  # negative start
            item(50, 55),  # shorter than min_seconds
            item(40, 100),  # longer than max_seconds
            item(70, 60),  # ends before it starts
            item(80, 100, score=2),  # score out of range
            item(80, 100, score=0.7),
        ]
    )
    detector = LLMClipDetector(FakeClient(answer), fallback=heuristic())
    clips = detector.detect(transcript(), 120.0, OPTIONS)

    assert [(c.start, c.end) for c in clips] == [(10.0, 30.0), (80.0, 100.0)]
    assert_within_video(clips, 120.0)
    assert detector.last_run.fallback_used is False
    assert detector.last_run.discarded_items == 4


def test_clips_respect_a_shorter_video_duration():
    # The transcript runs past the video; segments after it are not sent and
    # clips past it are dropped.
    answer = json.dumps([item(70, 95), item(30, 50)])
    client = FakeClient(answer)
    detector = LLMClipDetector(client, fallback=heuristic())
    clips = detector.detect(transcript(), 90.0, OPTIONS)
    assert [(c.start, c.end) for c in clips] == [(30.0, 50.0)]
    assert_within_video(clips, 90.0)
    assert '"start": 90.0' not in client.prompts[0][1]


def test_invalid_json_is_retried_once_then_succeeds():
    client = FakeClient("oops, not json", json.dumps([item(10, 30)]))
    detector = LLMClipDetector(client, fallback=heuristic())
    clips = detector.detect(transcript(), 120.0, OPTIONS)

    assert [(c.start, c.end) for c in clips] == [(10.0, 30.0)]
    assert len(client.prompts) == 2
    first, second = client.prompts[0][1], client.prompts[1][1]
    assert second == first + RETRY_NOTE
    assert detector.last_run.requests == 2
    assert detector.last_run.fallback_used is False


def test_invalid_json_twice_falls_back_to_heuristic(caplog):
    caplog.set_level(logging.WARNING)
    client = FakeClient("not json")
    detector = LLMClipDetector(client, fallback=heuristic())
    clips = detector.detect(transcript(), 120.0, OPTIONS)

    assert len(client.prompts) == 2
    assert clips == heuristic().detect(transcript(), 120.0, OPTIONS)
    assert clips
    run = detector.last_run
    assert run.detector == "heuristic"
    assert run.fallback_used is True
    assert run.failure_category == "invalid_output"
    assert run.requests == 2
    assert any(r.getMessage() == "clip_detection_fallback" for r in caplog.records)


def test_invalid_json_over_http_retries_then_falls_back():
    calls = []

    def handler(request):
        calls.append(request)
        return openai_reply("I cannot help with that.")

    detector = LLMClipDetector(openai_client(handler), fallback=heuristic())
    clips = detector.detect(transcript(), 120.0, OPTIONS)
    assert len(calls) == 2
    assert clips == heuristic().detect(transcript(), 120.0, OPTIONS)
    assert detector.last_run.failure_category == "invalid_output"


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "rate_limited"),
        (500, "server_error"),
        (400, "request_rejected"),
    ],
)
def test_http_errors_fall_back_without_retry(status, category):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "nope"})

    detector = LLMClipDetector(openai_client(handler), fallback=heuristic())
    clips = detector.detect(transcript(), 120.0, OPTIONS)

    assert len(calls) == 1
    assert clips == heuristic().detect(transcript(), 120.0, OPTIONS)
    assert detector.last_run.fallback_used is True
    assert detector.last_run.failure_category == category


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("down"), "network"),
    ],
)
def test_transport_errors_fall_back(error, category):
    def handler(request):
        raise error

    detector = LLMClipDetector(anthropic_client(handler), fallback=heuristic())
    clips = detector.detect(transcript(), 120.0, OPTIONS)
    assert clips == heuristic().detect(transcript(), 120.0, OPTIONS)
    assert detector.last_run.failure_category == category
    assert detector.last_run.requests == 1


def test_unexpected_client_error_falls_back():
    detector = LLMClipDetector(FakeClient(RuntimeError("boom")), fallback=heuristic())
    clips = detector.detect(transcript(), 120.0, OPTIONS)
    assert clips == heuristic().detect(transcript(), 120.0, OPTIONS)
    assert detector.last_run.failure_category == "unexpected"


def test_empty_transcript_makes_no_request():
    client = FakeClient("[]")
    detector = LLMClipDetector(client, fallback=heuristic())
    assert detector.detect([], 120.0, OPTIONS) == []
    assert client.prompts == []
    assert detector.last_run.fallback_used is False


def test_invalid_arguments_are_not_swallowed():
    detector = LLMClipDetector(FakeClient("[]"), fallback=heuristic())
    with pytest.raises(ValueError):
        detector.detect(transcript(), 0, OPTIONS)
    with pytest.raises(TypeError):
        detector.detect(transcript(), 120.0, None)


def test_estimated_cost_is_recorded_when_prices_are_configured():
    detector = LLMClipDetector(
        FakeClient("bad", "[]"),
        fallback=heuristic(),
        input_cost_per_million_tokens=3.0,
        output_cost_per_million_tokens=15.0,
    )
    detector.detect(transcript(), 120.0, OPTIONS)
    run = detector.last_run
    assert (run.requests, run.input_tokens, run.output_tokens) == (2, 200, 40)
    assert run.estimated_cost_usd == pytest.approx((200 * 3 + 40 * 15) / 1e6)
