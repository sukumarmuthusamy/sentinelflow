"""Capability-based LLM gateway for structured agent outputs."""

from __future__ import annotations

from typing import Protocol, TypeVar, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel

from app.core.settings import Settings, get_settings

T = TypeVar("T", bound=BaseModel)


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
    ) -> T:
        """Return a schema-validated model instance."""
        ...


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
    ) -> T:
        api_key = self._settings.require_gemini_api_key()
        llm_kwargs: dict[str, object] = {
            "model": model,
            "temperature": temperature,
            "google_api_key": api_key,
        }
        if self._client_max_retries is not None:
            llm_kwargs["max_retries"] = self._client_max_retries
        if self._client_max_retries is not None:
            llm = ChatGoogleGenerativeAI(
                model=model,
                temperature=temperature,
                google_api_key=api_key,
                max_retries=self._client_max_retries,
            )
        else:
            llm = ChatGoogleGenerativeAI(
                model=model,
                temperature=temperature,
                google_api_key=api_key,
            )
        structured = llm.with_structured_output(schema)
        result = await structured.ainvoke(
            [
                SystemMessage(content=system_prompt),
                HumanMessage(content=user_prompt),
            ],
        )
        if isinstance(result, schema):
            return result
        parsed = schema.model_validate(result)
        return parsed


class StaticStructuredLLM:
    """Test double returning preconfigured structured payloads."""

    def __init__(self, responses: dict[type[BaseModel], BaseModel]) -> None:
        self._responses = responses

    async def generate(
        self,
        *,
        model: str,
        temperature: float,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
    ) -> T:
        _ = (model, temperature, system_prompt, user_prompt)
        payload = self._responses.get(schema)
        if payload is None:
            msg = f"No static response registered for {schema.__name__}"
            raise KeyError(msg)
        return cast(T, payload.model_copy(deep=True))
