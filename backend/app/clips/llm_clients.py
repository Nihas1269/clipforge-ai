"""Provider-agnostic LLM access for the LLM clip detector, over plain httpx.

LLMClient is the only interface the detector needs:
`complete_json(system, user) -> str` returns the model's raw text answer.
Two adapters are provided (no vendor SDKs):

- OpenAICompatibleLLMClient: POST <LLM_API_BASE_URL>/chat/completions with
  `Authorization: Bearer <LLM_API_KEY>`.
- AnthropicLLMClient: POST <LLM_API_BASE_URL>/messages with `x-api-key` and
  `anthropic-version`.

LLM_API_BASE_URL includes the version path (for example
https://api.openai.com/v1 or https://api.anthropic.com/v1). It must be
https:// (http:// only for a loopback host), and redirects are not followed,
so the key is only sent to the configured host. Each request has an overall
deadline (checked while the body streams in, with per-read timeouts for a
stalled connection), the body is streamed and abandoned once it exceeds
MAX_RESPONSE_BYTES, and nothing is retried here.
Failures raise LLMError subclasses carrying a `category`; API keys, prompts
and response bodies are never logged or put in error messages.
"""

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from app.core.config import Settings
from app.transcription.openai_compatible import is_loopback_host, is_loopback_url

logger = logging.getLogger(__name__)

OPENAI_COMPATIBLE_PROVIDER = "openai_compatible"
ANTHROPIC_PROVIDER = "anthropic"
CHAT_COMPLETIONS_PATH = "/chat/completions"
MESSAGES_PATH = "/messages"
ANTHROPIC_VERSION = "2023-06-01"
CONNECT_TIMEOUT_SECONDS = 10.0
# A list of clip suggestions is far smaller than this.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_MODEL_NAME_LENGTH = 200
_API_KEY = re.compile(r"[\x21-\x7e]+")


class LLMError(Exception):
    """An LLM request or its answer failed.

    `category` is a short, loggable failure category, never containing secrets
    or content.
    """

    category = "error"

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category


class LLMConfigurationError(LLMError):
    """The LLM settings are missing or invalid."""

    category = "configuration"


class LLMProviderError(LLMError):
    """The provider could not be reached or answered with an HTTP error."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, category=category)
        self.status_code = status_code
        self.retryable = retryable


class LLMOutputError(LLMError):
    """The model's answer is not usable structured output."""

    category = "invalid_output"


@dataclass(frozen=True)
class LLMUsage:
    """Token counts of one request, when the provider reports them."""

    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class LLMClient(Protocol):
    """Sends a system and a user message and returns the model's text answer.

    Adapters also expose `provider` and `model` names and the `last_usage` of
    the latest request (None when unknown); the detector reads them if
    present.
    """

    def complete_json(self, system: str, user: str) -> str: ...


def _validate_base_url(url: str) -> str:
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        # Accessing the port raises ValueError for an invalid one.
        _ = parts.port
    except ValueError:
        raise LLMConfigurationError("LLM_API_BASE_URL is not a valid URL.") from None
    scheme = parts.scheme.lower()
    if (
        scheme not in ("http", "https")
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise LLMConfigurationError(
            "LLM_API_BASE_URL must be an https:// URL without credentials, a "
            "query or a fragment."
        )
    if scheme == "http" and not is_loopback_host(host):
        raise LLMConfigurationError(
            "LLM_API_BASE_URL must use https://; http:// is only allowed for a "
            "local endpoint (localhost, 127.0.0.1 or ::1)."
        )
    return url.strip().rstrip("/")


def _token_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class _HTTPLLMClient:
    """Shared request handling of the HTTP adapters."""

    provider = "llm"
    path = ""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: SecretStr | str | None = None,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 2048,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = _validate_base_url(base_url) + self.path
        if (
            not isinstance(model, str)
            or not model.strip()
            or len(model) > MAX_MODEL_NAME_LENGTH
            or not model.isprintable()
        ):
            raise LLMConfigurationError("LLM_MODEL must be a non-empty model name.")
        if isinstance(api_key, str):
            api_key = SecretStr(api_key)
        if api_key is not None and not _API_KEY.fullmatch(api_key.get_secret_value()):
            # The value itself is never included in the message.
            raise LLMConfigurationError(
                "LLM_API_KEY contains whitespace or non-printable characters."
            )
        if api_key is None and not is_loopback_url(base_url):
            raise LLMConfigurationError(
                "LLM_API_KEY is required for a remote endpoint."
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise LLMConfigurationError("The LLM request timeout must be positive.")
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens <= 0
        ):
            raise LLMConfigurationError("The LLM output token limit must be positive.")
        self.model = model.strip()
        self._api_key = api_key
        self._timeout_seconds = float(timeout_seconds)
        self._timeout = httpx.Timeout(
            timeout_seconds, connect=min(CONNECT_TIMEOUT_SECONDS, timeout_seconds)
        )
        self._max_output_tokens = max_output_tokens
        self._transport = transport
        self.last_usage: LLMUsage | None = None

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> "_HTTPLLMClient":
        """Create the client from settings; raise a clear error if any are missing."""
        base_url = settings.LLM_API_BASE_URL
        missing = []
        if not base_url:
            missing.append("LLM_API_BASE_URL")
        if settings.LLM_API_KEY is None and not (
            base_url and is_loopback_url(base_url)
        ):
            missing.append("LLM_API_KEY")
        if not settings.LLM_MODEL:
            missing.append("LLM_MODEL")
        if missing:
            raise LLMConfigurationError(
                f"LLM_PROVIDER={cls.provider!r} needs these settings: "
                f"{', '.join(missing)}. See docs/setup/llm.md."
            )
        options: dict[str, Any] = {
            "base_url": base_url,
            "model": settings.LLM_MODEL,
            "api_key": settings.LLM_API_KEY,
            "timeout_seconds": settings.LLM_TIMEOUT_SECONDS,
            "max_output_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
        }
        options.update(overrides)
        return cls(**options)

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, system: str, user: str) -> dict[str, Any]:
        raise NotImplementedError

    def _parse(self, data: Any) -> tuple[str, LLMUsage]:
        raise NotImplementedError

    def complete_json(self, system: str, user: str) -> str:
        self.last_usage = None
        started = time.monotonic()
        deadline = started + self._timeout_seconds
        headers = {"Accept": "application/json", **self._headers()}
        try:
            with httpx.Client(
                transport=self._transport, timeout=self._timeout, follow_redirects=False
            ) as client:
                with client.stream(
                    "POST",
                    self._endpoint,
                    json=self._body(system, user),
                    headers=headers,
                ) as response:
                    self._check_status(response)
                    content = self._read_body(response, deadline)
        except httpx.TimeoutException:
            raise self._failure("timeout", None, retryable=True) from None
        except httpx.TransportError as exc:
            raise self._failure(
                "network", None, retryable=True, detail=type(exc).__name__
            ) from None
        except httpx.HTTPError as exc:
            raise self._failure(
                "request_error", None, detail=type(exc).__name__
            ) from None

        try:
            data = json.loads(content)
        except ValueError:
            raise self._failure("malformed_response", response.status_code) from None
        text, usage = self._parse(data)
        self.last_usage = usage
        logger.info(
            "llm_request_completed",
            extra={
                "provider": self.provider,
                "model": self.model,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "response_chars": len(text),
            },
        )
        return text

    def _read_body(self, response: httpx.Response, deadline: float) -> bytes:
        """Read the body, aborting past MAX_RESPONSE_BYTES or the overall deadline.

        The body is never held in memory beyond the limit. The deadline is
        checked as data arrives; a stalled read is ended by the read timeout.
        """
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise self._failure("response_too_large", response.status_code)
        received = bytearray()
        for chunk in response.iter_bytes():
            received.extend(chunk)
            if len(received) > MAX_RESPONSE_BYTES:
                raise self._failure("response_too_large", response.status_code)
            if time.monotonic() > deadline:
                raise self._failure("timeout", None, retryable=True)
        return bytes(received)

    def _check_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise self._failure("authentication", status)
        if status == 429:
            raise self._failure("rate_limited", status, retryable=True)
        if status == 408 or status >= 500:
            raise self._failure("server_error", status, retryable=True)
        raise self._failure("request_rejected", status)

    def _failure(
        self,
        category: str,
        status: int | None,
        *,
        retryable: bool = False,
        detail: str | None = None,
    ) -> LLMProviderError:
        logger.warning(
            "llm_request_failed",
            extra={
                "provider": self.provider,
                "model": self.model,
                "status_code": status,
                "retryable": retryable,
                "category": category,
            },
        )
        message = f"The LLM provider request failed ({category}"
        if status is not None:
            message += f", HTTP {status}"
        if detail:
            message += f", {detail}"
        return LLMProviderError(
            message + ").", category=category, status_code=status, retryable=retryable
        )


class OpenAICompatibleLLMClient(_HTTPLLMClient):
    """An LLMClient for OpenAI-compatible chat completions APIs."""

    provider = OPENAI_COMPATIBLE_PROVIDER
    path = CHAT_COMPLETIONS_PATH

    def _headers(self) -> dict[str, str]:
        if self._api_key is None:
            return {}
        return {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}

    def _body(self, system: str, user: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": self._max_output_tokens,
        }

    def _parse(self, data: Any) -> tuple[str, LLMUsage]:
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMOutputError("The LLM response has no message content.") from None
        if not isinstance(text, str):
            raise LLMOutputError("The LLM response has no message content.")
        if choice.get("finish_reason") == "length":
            raise LLMOutputError("The LLM answer was cut off at the token limit.")
        usage = data.get("usage") if isinstance(data, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        return text, LLMUsage(
            input_tokens=_token_count(usage.get("prompt_tokens")),
            output_tokens=_token_count(usage.get("completion_tokens")),
        )


class AnthropicLLMClient(_HTTPLLMClient):
    """An LLMClient for the Anthropic Messages API."""

    provider = ANTHROPIC_PROVIDER
    path = MESSAGES_PATH

    def _headers(self) -> dict[str, str]:
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if self._api_key is not None:
            headers["x-api-key"] = self._api_key.get_secret_value()
        return headers

    def _body(self, system: str, user: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self._max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0,
        }

    def _parse(self, data: Any) -> tuple[str, LLMUsage]:
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise LLMOutputError("The LLM response has no message content.")
        parts = [
            block["text"]
            for block in data["content"]
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        if not parts:
            raise LLMOutputError("The LLM response has no message content.")
        if data.get("stop_reason") == "max_tokens":
            raise LLMOutputError("The LLM answer was cut off at the token limit.")
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        return "".join(parts), LLMUsage(
            input_tokens=_token_count(usage.get("input_tokens")),
            output_tokens=_token_count(usage.get("output_tokens")),
        )


LLM_CLIENTS: dict[str, type[_HTTPLLMClient]] = {
    OPENAI_COMPATIBLE_PROVIDER: OpenAICompatibleLLMClient,
    ANTHROPIC_PROVIDER: AnthropicLLMClient,
}


def get_llm_client(settings: Settings, **overrides: Any) -> _HTTPLLMClient:
    """Return the client named by LLM_PROVIDER, or raise LLMConfigurationError."""
    name = settings.LLM_PROVIDER
    if not name:
        raise LLMConfigurationError("LLM_PROVIDER is not set. See docs/setup/llm.md.")
    client_class = LLM_CLIENTS.get(name)
    if client_class is None:
        supported = ", ".join(repr(provider) for provider in sorted(LLM_CLIENTS))
        raise LLMConfigurationError(
            f"LLM_PROVIDER={name!r} is not supported; use one of: {supported}."
        )
    return client_class.from_settings(settings, **overrides)
