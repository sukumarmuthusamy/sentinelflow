"""Capability-based LLM gateway for structured agent outputs."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from app.core.settings import Settings, get_settings
from app.model_gateway.errors import (
    ProviderUnavailableError,
    is_provider_unavailable_error,
    is_transient_api_error,
)
from app.model_gateway.pacing import AsyncMinIntervalPacer

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

# Run-metadata key set on every structured-output call made through the gateway.
STRUCTURED_OUTPUT_METADATA_KEY = "sentinelflow_structured_schema"


class StructuredOutputError(Exception):
    """The provider responded, but the output did not validate against the schema.

    Carries the provider-reported usage so a failed call's tokens are still counted.
    """

    def __init__(self, message: str, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


@dataclass(frozen=True)
class LLMResult[ValueT: BaseModel]:
    """A schema-validated value plus the provider-reported token usage."""

    value: ValueT
    input_tokens: int = 0
    output_tokens: int = 0


class StructuredLLM(Protocol):
    """Structured generation port used by agents and the triage planner."""

    async def generate(
        self,
        *,
        model: str,
        temperature: float,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> LLMResult[T]:
        """Return a schema-validated model instance with token usage."""
        ...


def _usage_tokens(raw: object) -> tuple[int, int]:
    usage = getattr(raw, "usage_metadata", None)
    if not isinstance(usage, dict):
        return 0, 0
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


class _PacedStructuredLLM:
    """Shared call path: pacing, transient-only retries, structured parsing, usage."""

    structured_method: str | None = None

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client_max_retries: int | None = None,
        pacer: AsyncMinIntervalPacer | None = None,
        transient_retries: int = 0,
        transient_backoff_seconds: float = 20.0,
    ) -> None:
        """Pass client_max_retries=0 in eval scripts to disable SDK retry bursts.

        ``pacer`` spaces call starts (shared across parallel agents) to stay under a
        provider RPM limit; without it, parallel fan-out bursts straight into 429s.
        """
        self._settings = settings or get_settings()
        self._client_max_retries = client_max_retries
        self._pacer = pacer
        self._transient_retries = transient_retries
        self._transient_backoff = transient_backoff_seconds

    def _chat_model(self, *, model: str, temperature: float) -> BaseChatModel:
        raise NotImplementedError

    async def generate(
        self,
        *,
        model: str,
        temperature: float,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> LLMResult[T]:
        chat = self._chat_model(model=model, temperature=temperature)
        if self.structured_method is None:
            structured = chat.with_structured_output(schema, include_raw=True)
        else:
            structured = chat.with_structured_output(
                schema,
                method=self.structured_method,
                include_raw=True,
            )
        # Metadata only (no behaviour change): it rides on callback events, so a
        # LangGraph "messages" stream can tell this gateway's structured-output runs
        # from any other model call, and name the schema a fragment belongs to.
        structured = structured.with_config(
            metadata={STRUCTURED_OUTPUT_METADATA_KEY: schema.__name__},
        )
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        attempt = 0
        while True:
            if self._pacer is not None:
                await self._pacer.wait_turn()
            try:
                result = await structured.ainvoke(messages)
                break
            except Exception as exc:
                # Only provider overload (503/5xx) is retried here, with growing waits.
                # 429s and daily-quota errors propagate: retrying them burns quota.
                if attempt >= self._transient_retries or not is_transient_api_error(exc):
                    if is_provider_unavailable_error(exc):
                        msg = f"{model} unavailable: {type(exc).__name__}: {str(exc)[:200]}"
                        raise ProviderUnavailableError(msg) from exc
                    raise
                attempt += 1
                wait = self._transient_backoff * attempt
                logger.warning(
                    "%s transient error (attempt %d/%d), retrying in %.0fs: %s",
                    model,
                    attempt,
                    self._transient_retries,
                    wait,
                    str(exc)[:120],
                )
                await asyncio.sleep(wait)
        if not isinstance(result, dict):
            msg = f"Unexpected structured output container: {type(result).__name__}"
            raise StructuredOutputError(msg)
        input_tokens, output_tokens = _usage_tokens(result.get("raw"))
        parsed = result.get("parsed")
        if parsed is None:
            error = result.get("parsing_error")
            msg = f"{schema.__name__} did not validate: {error}"
            raise StructuredOutputError(
                msg,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        try:
            value = parsed if isinstance(parsed, schema) else schema.model_validate(parsed)
        except ValidationError as exc:
            msg = f"{schema.__name__} did not validate: {exc}"
            raise StructuredOutputError(
                msg,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ) from exc
        return LLMResult(value=value, input_tokens=input_tokens, output_tokens=output_tokens)


class GeminiStructuredLLM(_PacedStructuredLLM):
    """Google Gemini structured output via langchain-google-genai."""

    def _chat_model(self, *, model: str, temperature: float) -> BaseChatModel:
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "google_api_key": self._settings.require_gemini_api_key(),
        }
        if self._client_max_retries is not None:
            kwargs["max_retries"] = self._client_max_retries
        return ChatGoogleGenerativeAI(**kwargs)


class OpenRouterStructuredLLM(_PacedStructuredLLM):
    """OpenRouter (OpenAI-compatible) structured output via langchain-openai.

    Model IDs are OpenRouter slugs such as ``openai/gpt-oss-120b:free``. The JSON
    schema response format is used; a model without structured-output support fails
    with ``StructuredOutputError`` rather than silently returning prose.
    """

    structured_method = "json_schema"

    def _chat_model(self, *, model: str, temperature: float) -> BaseChatModel:
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "api_key": self._settings.require_openrouter_api_key(),
            "base_url": self._settings.openrouter_base_url,
            "default_headers": {"X-Title": "SentinelFlow"},
            # Only affects streamed calls (API_STREAM_TOKENS): with a custom base URL
            # langchain-openai omits usage from streams, which would zero token budgets.
            "stream_usage": True,
        }
        if self._client_max_retries is not None:
            kwargs["max_retries"] = self._client_max_retries
        return ChatOpenAI(**kwargs)


def is_openrouter_model(model: str) -> bool:
    """OpenRouter slugs are ``vendor/model``; Gemini model IDs have no slash."""
    return "/" in model


class RoutingStructuredLLM:
    """Route each call to a provider by model ID (capability-based gateway).

    Keeps the Triage Planner on a Gemini cheap tier while agents run elsewhere,
    each provider behind its own pacer.
    """

    def __init__(self, *, gemini: StructuredLLM, openrouter: StructuredLLM | None) -> None:
        self._gemini = gemini
        self._openrouter = openrouter

    async def generate(
        self,
        *,
        model: str,
        temperature: float,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> LLMResult[T]:
        target = self._gemini
        if is_openrouter_model(model):
            if self._openrouter is None:
                msg = f"{model} looks like an OpenRouter model but OPENROUTER_API_KEY is unset"
                raise RuntimeError(msg)
            target = self._openrouter
        return await target.generate(
            model=model,
            temperature=temperature,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
        )


class StaticStructuredLLM:
    """Test double returning preconfigured structured payloads."""

    def __init__(
        self,
        responses: dict[type[BaseModel], BaseModel],
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        self._responses = responses
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.calls: list[str] = []
        # (schema name, system prompt, user prompt) per call, for prompt-content tests.
        self.prompts: list[tuple[str, str, str]] = []

    async def generate(
        self,
        *,
        model: str,
        temperature: float,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> LLMResult[T]:
        _ = (model, temperature)
        payload = self._responses.get(schema)
        if payload is None:
            msg = f"No static response registered for {schema.__name__}"
            raise KeyError(msg)
        self.calls.append(schema.__name__)
        self.prompts.append((schema.__name__, system_prompt, user_prompt))
        return LLMResult(
            value=cast(T, payload.model_copy(deep=True)),
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )
