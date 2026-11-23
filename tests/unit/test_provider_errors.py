"""Provider error classification drives stop-vs-retry decisions in live runs."""

from __future__ import annotations

from typing import Any

import pytest
from app.core.settings import Settings
from app.graph.state import AnalysisPlan
from app.model_gateway.errors import (
    ProviderUnavailableError,
    is_daily_quota_exhausted,
    is_provider_unavailable_error,
    is_quota_or_rate_limit_error,
    is_transient_api_error,
)
from app.model_gateway.llm import GeminiStructuredLLM

DAILY_2026_11 = Exception(
    "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, "
    "model: gemini-3.8-flash, description: Generate content requests per day limit "
    "exceeded on Free Tier.",
)
DAILY_2026_09 = Exception(
    "429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier",
)
PER_MINUTE = Exception(
    "429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
)
OVERLOADED = Exception("503 UNAVAILABLE. This model is currently experiencing high demand.")


def test_daily_quota_detected_in_both_wordings() -> None:
    assert is_daily_quota_exhausted(DAILY_2026_11)
    assert is_daily_quota_exhausted(DAILY_2026_09)


def test_per_minute_limit_is_not_daily() -> None:
    assert is_quota_or_rate_limit_error(PER_MINUTE)
    assert not is_daily_quota_exhausted(PER_MINUTE)


def test_overload_is_transient_not_quota() -> None:
    assert is_transient_api_error(OVERLOADED)
    assert not is_quota_or_rate_limit_error(OVERLOADED)
    assert not is_transient_api_error(DAILY_2026_11)


# --- Transport failures become ProviderUnavailableError (a ConnectionError) ---------


class APIConnectionError(Exception):
    """Same class name as openai.APIConnectionError (matched by name, not import)."""


class APITimeoutError(APIConnectionError):
    pass


class TransportError(Exception):
    """Same class name as httpx.TransportError."""


class ConnectError(TransportError):
    pass


class AuthenticationError(Exception):
    status_code = 401


def test_provider_unavailable_covers_sdk_transport_errors() -> None:
    for exc in (
        APIConnectionError("Connection error."),
        APITimeoutError("Request timed out."),
        ConnectError("All connection attempts failed"),
        TimeoutError(),
        ConnectionResetError(),
        OVERLOADED,
    ):
        assert is_provider_unavailable_error(exc), exc


def test_provider_unavailable_never_covers_quota_or_auth() -> None:
    for exc in (DAILY_2026_11, DAILY_2026_09, PER_MINUTE, AuthenticationError("401 bad key")):
        assert not is_provider_unavailable_error(exc), exc


class _RaisingStructured:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.attempts = 0

    async def ainvoke(self, messages: object) -> object:
        _ = messages
        self.attempts += 1
        raise self._error


class _FakeChat:
    def __init__(self, structured: _RaisingStructured) -> None:
        self._structured = structured

    def with_structured_output(self, schema: object, **kwargs: object) -> _RaisingStructured:
        _ = (schema, kwargs)
        return self._structured


def _gateway(error: Exception, *, transient_retries: int = 0) -> tuple[Any, _RaisingStructured]:
    structured = _RaisingStructured(error)

    class _Gateway(GeminiStructuredLLM):
        def _chat_model(self, *, model: str, temperature: float) -> Any:
            _ = (model, temperature)
            return _FakeChat(structured)

    gateway = _Gateway(
        Settings(),
        transient_retries=transient_retries,
        transient_backoff_seconds=0.0,
    )
    return gateway, structured


async def _generate(gateway: Any) -> Any:
    return await gateway.generate(
        model="m",
        temperature=0.0,
        system_prompt="s",
        user_prompt="u",
        schema=AnalysisPlan,
    )


@pytest.mark.asyncio
async def test_gateway_translates_overload_after_retries() -> None:
    gateway, structured = _gateway(OVERLOADED, transient_retries=2)
    with pytest.raises(ProviderUnavailableError) as info:
        await _generate(gateway)
    assert structured.attempts == 3  # 1 attempt + 2 transient retries, then translated
    assert info.value.__cause__ is OVERLOADED
    assert isinstance(info.value, ConnectionError)


@pytest.mark.asyncio
async def test_gateway_translates_sdk_timeout_without_retrying() -> None:
    gateway, structured = _gateway(APITimeoutError("Request timed out."), transient_retries=3)
    with pytest.raises(ProviderUnavailableError):
        await _generate(gateway)
    assert structured.attempts == 1  # only 5xx overload is retried


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [DAILY_2026_11, PER_MINUTE, AuthenticationError("401 invalid key")],
    ids=["daily_quota", "per_minute", "auth"],
)
async def test_gateway_leaves_quota_and_auth_errors_untouched(error: Exception) -> None:
    gateway, structured = _gateway(error, transient_retries=3)
    with pytest.raises(type(error)) as info:
        await _generate(gateway)
    assert info.value is error
    assert structured.attempts == 1


def test_graph_retry_policy_skips_operational_stops() -> None:
    from app.graph.build import retry_node_on

    assert not retry_node_on(DAILY_2026_11)
    assert not retry_node_on(PER_MINUTE)
    assert not retry_node_on(AuthenticationError("401"))
    assert retry_node_on(ConnectionResetError())  # LangGraph default kept otherwise
    assert not retry_node_on(KeyError("bug"))
