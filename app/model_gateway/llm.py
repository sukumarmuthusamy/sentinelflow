"""Capability-based LLM gateway for structured agent outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, ValidationError

from app.core.settings import Settings, get_settings

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    """The provider responded, but the output did not validate against the schema."""


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


class GeminiStructuredLLM:
    """Google Gemini structured output via langchain-google-genai."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client_max_retries: int | None = None,
    ) -> None:
        """Pass client_max_retries=0 in eval scripts to disable SDK tenacity bursts."""
        self._settings = settings or get_settings()
        self._client_max_retries = client_max_retries

    async def generate(
        self,
        *,
        model: str,
        temperature: float,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> LLMResult[T]:
        llm_kwargs: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "google_api_key": self._settings.require_gemini_api_key(),
        }
        if self._client_max_retries is not None:
            llm_kwargs["max_retries"] = self._client_max_retries
        llm = ChatGoogleGenerativeAI(**llm_kwargs)
        structured = llm.with_structured_output(schema, include_raw=True)
        result = await structured.ainvoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ],
        )
        if not isinstance(result, dict):
            msg = f"Unexpected structured output container: {type(result).__name__}"
            raise StructuredOutputError(msg)
        input_tokens, output_tokens = _usage_tokens(result.get("raw"))
        parsed = result.get("parsed")
        if parsed is None:
            error = result.get("parsing_error")
            msg = f"{schema.__name__} did not validate: {error}"
            raise StructuredOutputError(msg)
        try:
            value = parsed if isinstance(parsed, schema) else schema.model_validate(parsed)
        except ValidationError as exc:
            msg = f"{schema.__name__} did not validate: {exc}"
            raise StructuredOutputError(msg) from exc
        return LLMResult(value=value, input_tokens=input_tokens, output_tokens=output_tokens)


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

    async def generate(
        self,
        *,
        model: str,
        temperature: float,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> LLMResult[T]:
        _ = (model, temperature, system_prompt, user_prompt)
        payload = self._responses.get(schema)
        if payload is None:
            msg = f"No static response registered for {schema.__name__}"
            raise KeyError(msg)
        self.calls.append(schema.__name__)
        return LLMResult(
            value=cast(T, payload.model_copy(deep=True)),
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )
