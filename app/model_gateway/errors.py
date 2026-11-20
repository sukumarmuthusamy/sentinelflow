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
    if "PERDAY" in upper or "PER_DAY" in upper:
        return True
    if "GENERATEREQUESTSPERDAY" in upper.replace("_", ""):
        return True
    if "QUOTAVALUE': '20'" in text or 'QUOTAVALUE": "20"' in text:
        return True
    return "FreeTier" in text and "PerDay" in text


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
