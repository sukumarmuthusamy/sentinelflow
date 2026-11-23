"""Provider routing in the model gateway: model ID decides the provider."""

from __future__ import annotations

import pytest
from app.core.settings import Settings
from app.model_gateway.llm import (
    RoutingStructuredLLM,
    StaticStructuredLLM,
    is_openrouter_model,
)
from pydantic import BaseModel, SecretStr


class _Out(BaseModel):
    provider: str


def _kwargs(model: str) -> dict[str, object]:
    return {
        "model": model,
        "temperature": 0.0,
        "system_prompt": "s",
        "user_prompt": "u",
        "schema": _Out,
    }


def test_slug_detection() -> None:
    assert is_openrouter_model("openai/gpt-oss-120b:free")
    assert not is_openrouter_model("gemini-3.8-flash")


async def test_routes_by_model_id() -> None:
    gemini = StaticStructuredLLM({_Out: _Out(provider="gemini")})
    openrouter = StaticStructuredLLM({_Out: _Out(provider="openrouter")})
    router = RoutingStructuredLLM(gemini=gemini, openrouter=openrouter)

    a = await router.generate(**_kwargs("gemini-3.5-flash-lite"))  # type: ignore[arg-type]
    b = await router.generate(**_kwargs("meta-llama/llama-4:free"))  # type: ignore[arg-type]

    assert a.value.provider == "gemini"
    assert b.value.provider == "openrouter"
    assert gemini.calls == ["_Out"]
    assert openrouter.calls == ["_Out"]


async def test_openrouter_slug_without_key_fails_loudly() -> None:
    router = RoutingStructuredLLM(
        gemini=StaticStructuredLLM({_Out: _Out(provider="gemini")}),
        openrouter=None,
    )
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        await router.generate(**_kwargs("openai/gpt-oss-120b:free"))  # type: ignore[arg-type]


def test_require_openrouter_key() -> None:
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        Settings(_env_file=None, openrouter_api_key=None).require_openrouter_api_key()  # type: ignore[call-arg]
    key = Settings(_env_file=None, openrouter_api_key=SecretStr("x"))  # type: ignore[call-arg]
    assert key.require_openrouter_api_key() == "x"
