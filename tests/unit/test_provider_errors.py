"""Provider error classification drives stop-vs-retry decisions in live runs."""

from __future__ import annotations

from app.model_gateway.errors import (
    is_daily_quota_exhausted,
    is_quota_or_rate_limit_error,
    is_transient_api_error,
)

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
