"""Shared fixtures-as-functions for the LLM clip detector tests."""

import httpx

from app.clips import ClipDetectionOptions, HeuristicClipDetector
from app.clips.llm_clients import (
    AnthropicLLMClient,
    LLMUsage,
    OpenAICompatibleLLMClient,
)
from app.core.config import Settings
from app.transcription.base import TranscriptionSegment

BASE_URL = "https://llm.example.test/v1"
TEST_KEY = "test-api-key-not-real"
PRIVATE_TEXT = "my private words about the launch plan"
OPTIONS = ClipDetectionOptions(min_seconds=15, max_seconds=30, max_clips=5)


def seg(start, end, text=PRIVATE_TEXT):
    return TranscriptionSegment(start=start, end=end, text=text)


def transcript(duration=120, step=5):
    return [seg(start, start + step) for start in range(0, duration, step)]


def item(start, end, score=0.8, title="A great moment", reason="It has a hook."):
    return {
        "start_seconds": start,
        "end_seconds": end,
        "title": title,
        "reason": reason,
        "score": score,
    }


def heuristic():
    return HeuristicClipDetector(keywords=[])


class FakeClient:
    """An LLMClient returning canned answers (or raising canned errors)."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []
        self.last_usage = None

    def complete_json(self, system, user):
        self.prompts.append((system, user))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = answer(system, user)
        if isinstance(answer, Exception):
            self.last_usage = None
            raise answer
        self.last_usage = LLMUsage(input_tokens=100, output_tokens=20)
        return answer


def openai_reply(content, finish_reason="stop", usage=None):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            "usage": usage or {"prompt_tokens": 120, "completion_tokens": 30},
        },
    )


def anthropic_reply(text, stop_reason="end_turn"):
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 90, "output_tokens": 25},
        },
    )


def openai_client(handler, **kwargs):
    return OpenAICompatibleLLMClient(
        base_url=BASE_URL,
        model="test-model",
        api_key=TEST_KEY,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def anthropic_client(handler, **kwargs):
    return AnthropicLLMClient(
        base_url=BASE_URL,
        model="test-claude",
        api_key=TEST_KEY,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def llm_settings(**overrides):
    values = {
        "CLIP_DETECTOR": "llm",
        "LLM_PROVIDER": "openai_compatible",
        "LLM_API_BASE_URL": BASE_URL,
        "LLM_API_KEY": TEST_KEY,
        "LLM_MODEL": "test-model",
        "CLIP_KEYWORDS": "",
    }
    values.update(overrides)
    return Settings(**values)


def assert_within_video(clips, duration):
    for clip in clips:
        assert 0 <= clip.start < clip.end <= duration
