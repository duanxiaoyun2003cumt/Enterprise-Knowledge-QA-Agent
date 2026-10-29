"""Minimal OpenAI-compatible Chat Completions client.

Deliberately small: one request method, sanitized error mapping, no agent loop,
no provider abstraction, and no retry scheduler. The SDK's automatic retries
are disabled so failures surface immediately and predictably.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, NoReturn

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from .config import Settings


class LLMClientError(RuntimeError):
    """Base class for model client failures with sanitized messages."""


class LLMAuthenticationError(LLMClientError):
    """The provider rejected the API key."""


class LLMConnectionError(LLMClientError):
    """The provider endpoint could not be reached."""


class LLMTimeoutError(LLMClientError):
    """The request exceeded the configured timeout."""


class LLMAPIError(LLMClientError):
    """The provider returned a non-success HTTP status."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMServerError(LLMAPIError):
    """The provider returned a 5xx status."""


class LLMRateLimitError(LLMAPIError):
    """The provider rejected the request due to rate limiting."""


class LLMResponseFormatError(LLMClientError):
    """The provider response did not match the Chat Completions shape."""


@dataclass(frozen=True)
class LLMResponse:
    """Text and basic metadata from one successful completion."""

    text: str
    model: str
    latency_ms: float
    finish_reason: str | None = None


class LLMClient:
    """Send minimal Chat Completions requests to an OpenAI-compatible endpoint."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
            max_retries=0,
        )

    @property
    def model(self) -> str:
        return self._settings.model

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """Send one Chat Completions request and return the textual response.

        ``response_format`` and ``temperature`` are only sent when explicitly
        provided, because not every OpenAI-compatible provider accepts the same
        parameters.
        """
        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": messages,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if temperature is not None:
            kwargs["temperature"] = temperature

        started = time.perf_counter()
        try:
            completion = self._client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            self._raise_mapped(exc)
        latency_ms = (time.perf_counter() - started) * 1000

        choices = getattr(completion, "choices", None)
        if not choices:
            raise LLMResponseFormatError("Model response contained no choices.")

        choice = choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseFormatError(
                "Model response did not contain non-empty text content."
            )

        reported_model = getattr(completion, "model", None) or self._settings.model
        finish_reason = getattr(choice, "finish_reason", None)
        return LLMResponse(
            text=content,
            model=str(reported_model),
            latency_ms=latency_ms,
            finish_reason=str(finish_reason) if finish_reason is not None else None,
        )

    def _raise_mapped(self, exc: OpenAIError) -> NoReturn:
        """Translate SDK errors without echoing credentials or request headers."""
        if isinstance(exc, AuthenticationError):
            raise LLMAuthenticationError(
                "Model API authentication failed (HTTP 401). Check that "
                "LLM_API_KEY is valid for the configured endpoint."
            ) from None
        if isinstance(exc, RateLimitError):
            raise LLMRateLimitError(
                "Model API rate limit or quota exceeded (HTTP 429). Wait and "
                "retry, or check the provider account quota.",
                status_code=getattr(exc, "status_code", 429),
            ) from None
        if isinstance(exc, APITimeoutError):
            raise LLMTimeoutError(
                f"Model request timed out after {self._settings.timeout:g} seconds. "
                "Increase LLM_TIMEOUT or check network/provider latency."
            ) from None
        if isinstance(exc, APIConnectionError):
            raise LLMConnectionError(
                "Could not connect to the model endpoint configured by "
                "LLM_BASE_URL. Check the URL, network access, and provider "
                "availability."
            ) from None
        if isinstance(exc, APIStatusError):
            status = getattr(exc, "status_code", None)
            if status is not None and status >= 500:
                raise LLMServerError(
                    f"Model provider returned a server error (HTTP {status}). "
                    "Retry later or inspect provider status.",
                    status_code=status,
                ) from None
            if status == 402:
                raise LLMAPIError(
                    "Model provider reports insufficient account balance "
                    "(HTTP 402). Top up the DeepSeek account and retry.",
                    status_code=status,
                ) from None
            raise LLMAPIError(
                f"Model provider rejected the request (HTTP {status}). Check "
                "LLM_MODEL, the endpoint, and request parameters.",
                status_code=status,
            ) from None
        raise LLMClientError(
            f"Unexpected model client error ({type(exc).__name__})."
        ) from None
