import json
import time

import httpx
import pytest

from app.clips.llm_clients import (
    MAX_RESPONSE_BYTES,
    AnthropicLLMClient,
    LLMConfigurationError,
    LLMOutputError,
    LLMProviderError,
    LLMUsage,
    OpenAICompatibleLLMClient,
    get_llm_client,
)
from tests.llm_helpers import (
    BASE_URL,
    TEST_KEY,
    anthropic_client,
    anthropic_reply,
    llm_settings,
    openai_client,
    openai_reply,
)

# --- Adapters: request formatting --------------------------------------------


def test_openai_adapter_request_format():
    seen = []

    def handler(request):
        seen.append(request)
        return openai_reply("[]")

    client = openai_client(handler, max_output_tokens=512)
    assert client.complete_json("system text", "user text") == "[]"

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {TEST_KEY}"
    assert "x-api-key" not in request.headers
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.content)
    assert body == {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "system text"},
            {"role": "user", "content": "user text"},
        ],
        "temperature": 0,
        "max_tokens": 512,
    }
    assert client.last_usage == LLMUsage(input_tokens=120, output_tokens=30)


def test_anthropic_adapter_request_format():
    seen = []

    def handler(request):
        seen.append(request)
        return anthropic_reply("[]")

    client = anthropic_client(handler, max_output_tokens=1024)
    assert client.complete_json("system text", "user text") == "[]"

    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/messages"
    assert request.headers["x-api-key"] == TEST_KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in request.headers
    body = json.loads(request.content)
    assert body == {
        "model": "test-claude",
        "max_tokens": 1024,
        "system": "system text",
        "messages": [{"role": "user", "content": "user text"}],
        "temperature": 0,
    }
    assert client.last_usage == LLMUsage(input_tokens=90, output_tokens=25)


def test_anthropic_adapter_joins_text_blocks_and_skips_others():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "[ "},
                    {"type": "tool_use", "id": "x"},
                    {"type": "text", "text": "]"},
                ],
                "stop_reason": "end_turn",
            },
        )

    client = anthropic_client(handler)
    assert client.complete_json("s", "u") == "[ ]"
    assert client.last_usage == LLMUsage(None, None)


@pytest.mark.parametrize(
    ("make_client", "reply"),
    [
        (openai_client, lambda: openai_reply("[", finish_reason="length")),
        (anthropic_client, lambda: anthropic_reply("[", stop_reason="max_tokens")),
        (openai_client, lambda: httpx.Response(200, json={"choices": []})),
        (anthropic_client, lambda: httpx.Response(200, json={"content": "x"})),
    ],
)
def test_adapters_reject_truncated_or_empty_answers(make_client, reply):
    client = make_client(lambda request: reply())
    with pytest.raises(LLMOutputError):
        client.complete_json("s", "u")


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (401, "authentication", False),
        (403, "authentication", False),
        (400, "request_rejected", False),
        (429, "rate_limited", True),
        (500, "server_error", True),
        (503, "server_error", True),
    ],
)
def test_adapters_map_http_errors(status, category, retryable):
    client = openai_client(lambda request: httpx.Response(status, text="secret body"))
    with pytest.raises(LLMProviderError) as info:
        client.complete_json("s", "u")
    assert info.value.category == category
    assert info.value.status_code == status
    assert info.value.retryable is retryable
    assert "secret body" not in str(info.value)


def test_adapter_rejects_oversized_and_non_json_responses():
    big = openai_client(
        lambda request: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
    )
    with pytest.raises(LLMProviderError) as info:
        big.complete_json("s", "u")
    assert info.value.category == "response_too_large"

    not_json = openai_client(lambda request: httpx.Response(200, content=b"<html>"))
    with pytest.raises(LLMProviderError) as info:
        not_json.complete_json("s", "u")
    assert info.value.category == "malformed_response"


class ChunkedStream(httpx.SyncByteStream):
    """A response body without Content-Length, recording how much was read."""

    def __init__(self, chunks, delay=0.0):
        self.chunks = chunks
        self.delay = delay
        self.sent = 0

    def __iter__(self):
        for chunk in self.chunks:
            if self.delay:
                time.sleep(self.delay)
            self.sent += 1
            yield chunk


def test_adapter_stops_reading_an_oversized_streamed_body():
    stream = ChunkedStream([b"x" * (1024 * 1024)] * 10)
    client = openai_client(lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(LLMProviderError) as info:
        client.complete_json("s", "u")
    assert info.value.category == "response_too_large"
    # Abandoned just past MAX_RESPONSE_BYTES (2 MiB), not read to the end.
    assert stream.sent == 3


def test_adapter_enforces_an_overall_deadline():
    body = json.dumps(
        {"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]}
    ).encode()
    # Every chunk arrives well within the read timeout, but the body as a
    # whole takes longer than the request deadline.
    stream = ChunkedStream([body[i : i + 1] for i in range(len(body))], delay=0.02)
    client = openai_client(
        lambda request: httpx.Response(200, stream=stream), timeout_seconds=0.1
    )
    with pytest.raises(LLMProviderError) as info:
        client.complete_json("s", "u")
    assert info.value.category == "timeout"
    assert info.value.retryable is True
    assert stream.sent < len(body)


def test_adapter_does_not_follow_redirects():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(307, headers={"Location": "https://elsewhere.test/v1"})

    with pytest.raises(LLMProviderError):
        openai_client(handler).complete_json("s", "u")
    assert len(calls) == 1


def test_adapter_uses_configured_timeout():
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return openai_reply("[]")

    openai_client(handler, timeout_seconds=7).complete_json("s", "u")
    assert seen[0]["read"] == 7
    assert seen[0]["connect"] == 7


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "http://llm.example.test/v1"},
        {"base_url": "https://user:pw@llm.example.test/v1"},
        {"base_url": "https://llm.example.test/v1?x=1"},
        {"base_url": "ftp://llm.example.test"},
        {"model": " "},
        {"api_key": None},
        {"api_key": "bad key"},
        {"timeout_seconds": 0},
    ],
)
def test_adapter_rejects_invalid_configuration(kwargs):
    options = {"base_url": BASE_URL, "model": "m", "api_key": TEST_KEY}
    options.update(kwargs)
    with pytest.raises(LLMConfigurationError) as info:
        OpenAICompatibleLLMClient(**options)
    assert TEST_KEY not in str(info.value)


def test_adapter_allows_local_endpoint_without_key():
    seen = []

    def handler(request):
        seen.append(request)
        return openai_reply("[]")

    client = OpenAICompatibleLLMClient(
        base_url="http://localhost:11434/v1",
        model="local",
        transport=httpx.MockTransport(handler),
    )
    client.complete_json("s", "u")
    assert "Authorization" not in seen[0].headers


def test_get_llm_client_selects_adapter_and_names_missing_settings():
    assert isinstance(get_llm_client(llm_settings()), OpenAICompatibleLLMClient)
    assert isinstance(
        get_llm_client(llm_settings(LLM_PROVIDER="Anthropic")), AnthropicLLMClient
    )
    with pytest.raises(LLMConfigurationError, match="LLM_PROVIDER is not set"):
        get_llm_client(llm_settings(LLM_PROVIDER=""))
    with pytest.raises(LLMConfigurationError, match="not supported"):
        get_llm_client(llm_settings(LLM_PROVIDER="other"))
    with pytest.raises(LLMConfigurationError) as info:
        get_llm_client(llm_settings(LLM_API_KEY=" ", LLM_MODEL=""))
    assert "LLM_API_KEY" in str(info.value) and "LLM_MODEL" in str(info.value)
