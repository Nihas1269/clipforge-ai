import json
import logging

import httpx
import pytest

from app.clips import HeuristicClipDetector, LLMClipDetector, get_clip_detector
from app.clips.llm_clients import AnthropicLLMClient
from app.clips.llm_prompt import PROMPT_VERSION
from app.core.config import Settings
from tests.llm_helpers import (
    OPTIONS,
    heuristic,
    item,
    llm_settings,
    openai_reply,
    transcript,
)

# --- Selection ----------------------------------------------------------------


def test_default_detector_is_heuristic():
    settings = Settings()
    assert settings.CLIP_DETECTOR == "heuristic"
    assert isinstance(get_clip_detector(settings), HeuristicClipDetector)


def test_unknown_detector_falls_back_to_heuristic(caplog):
    caplog.set_level(logging.WARNING)
    detector = get_clip_detector(Settings(CLIP_DETECTOR="magic"))
    assert isinstance(detector, HeuristicClipDetector)
    assert any(r.getMessage() == "clip_detector_unknown" for r in caplog.records)


def test_unconfigured_llm_falls_back_to_heuristic(caplog, monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_API_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLIP_DETECTOR", " LLM ")
    monkeypatch.setenv("CLIP_KEYWORDS", "")
    caplog.set_level(logging.WARNING)

    detector = get_clip_detector()
    assert isinstance(detector, LLMClipDetector)
    assert detector.client is None
    clips = detector.detect(transcript(), 120.0, OPTIONS)

    assert clips == heuristic().detect(transcript(), 120.0, OPTIONS)
    run = detector.last_run
    assert run.fallback_used is True
    assert run.failure_category == "configuration"
    assert run.requests == 0
    assert run.prompt_version == PROMPT_VERSION
    messages = {r.getMessage() for r in caplog.records}
    assert {"llm_clip_detector_unconfigured", "clip_detection_fallback"} <= messages


def test_configured_llm_detector_from_settings():
    detector = get_clip_detector(llm_settings(LLM_PROVIDER="anthropic"))
    assert isinstance(detector, LLMClipDetector)
    assert isinstance(detector.client, AnthropicLLMClient)
    assert detector.provider == "anthropic" and detector.model == "test-model"


def test_from_settings_end_to_end_with_mock_transport():
    seen = []

    def handler(request):
        seen.append(request)
        return openai_reply(json.dumps([item(10, 30)]))

    settings = llm_settings(
        LLM_MAX_PROMPT_CHARS=5000,
        LLM_INPUT_COST_PER_MILLION_TOKENS="1",
        LLM_OUTPUT_COST_PER_MILLION_TOKENS="2",
        LLM_MAX_OUTPUT_TOKENS=300,
    )
    detector = LLMClipDetector.from_settings(
        settings, transport=httpx.MockTransport(handler)
    )
    clips = detector.detect(transcript(), 120.0, OPTIONS)
    assert [(c.start, c.end) for c in clips] == [(10.0, 30.0)]
    assert json.loads(seen[0].content)["max_tokens"] == 300
    assert len(json.loads(seen[0].content)["messages"][1]["content"]) <= 5000
    assert detector.last_run.estimated_cost_usd == pytest.approx((120 + 60) / 1e6)


def test_llm_settings_blank_values_are_unset():
    settings = Settings(
        LLM_PROVIDER=" ",
        LLM_API_BASE_URL="",
        LLM_API_KEY=" ",
        LLM_MODEL="",
        LLM_INPUT_COST_PER_MILLION_TOKENS="",
    )
    assert settings.LLM_PROVIDER is None
    assert settings.LLM_API_BASE_URL is None
    assert settings.LLM_API_KEY is None
    assert settings.LLM_MODEL is None
    assert settings.LLM_INPUT_COST_PER_MILLION_TOKENS is None
    assert Settings(CLIP_DETECTOR="").CLIP_DETECTOR == "heuristic"
