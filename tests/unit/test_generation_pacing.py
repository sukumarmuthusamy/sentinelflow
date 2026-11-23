"""Tests for Gemini generation pacing helpers."""

from __future__ import annotations

from app.model_gateway.pacing import estimate_paced_wall_seconds, min_interval_for_rpm


def test_min_interval_for_five_rpm() -> None:
    assert min_interval_for_rpm(5) == 12.5


def test_wall_clock_estimate_nine_calls() -> None:
    estimate = estimate_paced_wall_seconds(call_count=9, min_interval_seconds=12.5)
    assert estimate == 8 * 12.5 + 9 * 5.0
