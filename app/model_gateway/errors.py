"""Classify provider errors for graceful degradation in eval scripts."""

from __future__ import annotations


def is_quota_or_rate_limit_error(exc: BaseException) -> bool:
    """True for HTTP 429 / RESOURCE_EXHAUSTED style failures."""
    text = str(exc).upper()
    if "429" in text or "RESOURCE_EXHAUSTED" in text:
        return True
    code = getattr(exc, "code", None)
    if code == 429:
        return True
    status = getattr(exc, "status_code", None)
    return status == 429


def is_daily_quota_exhausted(exc: BaseException) -> bool:
    """True when the free-tier per-day request cap is exhausted (retry won't help today)."""
    text = str(exc)
    upper = text.upper()
    # Wordings seen so far: quotaId "...PerDayPerProjectPerModel-FreeTier" (2026-09) and
    # "Generate content requests per day limit exceeded on Free Tier" (2026-11).
    if "PERDAY" in upper or "PER_DAY" in upper or "PER DAY" in upper:
        return True
    if "GENERATEREQUESTSPERDAY" in upper.replace("_", ""):
        return True
    if "QUOTAVALUE': '20'" in text or 'QUOTAVALUE": "20"' in text:
        return True
    return "FreeTier" in text and "PerDay" in text


class ProviderUnavailableError(ConnectionError):
    """The provider could not be reached or kept failing with 5xx/timeouts.

    Subclasses ``ConnectionError`` so the graph's agent-failure handling degrades the
    run to REVIEW. SDK transport errors (``openai.APIConnectionError``,
    ``httpx.TransportError``, Gemini ``ServerError``) do not inherit from the builtin
    connection/timeout types, so without this translation they escaped that handling.
    Quota, rate-limit and authentication errors are never translated.
    """


# Transport-failure classes by name, so the gateway needs no direct SDK imports.
_TRANSPORT_ERROR_NAMES = frozenset(
    {
        "APIConnectionError",  # openai (APITimeoutError subclasses it)
        "APITimeoutError",
        "TransportError",  # httpx: ConnectError, ReadTimeout, RemoteProtocolError, ...
        "TimeoutException",
        "NetworkError",
    },
)


def is_auth_error(exc: BaseException) -> bool:
    """True for rejected credentials or permissions (401/403)."""
    for attr in ("status_code", "code"):
        if getattr(exc, attr, None) in {401, 403}:
            return True
    text = str(exc).upper()
    return "UNAUTHENTICATED" in text or "PERMISSION_DENIED" in text or "API_KEY_INVALID" in text


def is_operational_stop_error(exc: BaseException) -> bool:
    """Quota, rate-limit or auth failures: retrying burns quota or cannot succeed."""
    return is_quota_or_rate_limit_error(exc) or is_daily_quota_exhausted(exc) or is_auth_error(exc)


def is_provider_unavailable_error(exc: BaseException) -> bool:
    """True for connection failures, timeouts and 5xx overload; never quota or auth."""
    if is_operational_stop_error(exc):
        return False
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    if any(cls.__name__ in _TRANSPORT_ERROR_NAMES for cls in type(exc).__mro__):
        return True
    return is_transient_api_error(exc)


def is_transient_api_error(exc: BaseException) -> bool:
    """True for retryable provider overload (503, UNAVAILABLE)."""
    text = str(exc).upper()
    if "503" in text or "UNAVAILABLE" in text or "HIGH DEMAND" in text:
        return True
    code = getattr(exc, "code", None)
    if code in {503, 500, 502, 504}:
        return True
    status = getattr(exc, "status_code", None)
    return status in {500, 502, 503, 504}
