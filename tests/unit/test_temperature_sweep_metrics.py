"""Sweep metrics: provider/quota failures are not schema failures."""

from __future__ import annotations

from scripts.temperature_sweep import SweepArm, choose_temperature


def _arm(temperature: float, outcomes: list[str]) -> SweepArm:
    return SweepArm(
        temperature=temperature,
        attempts=len(outcomes),
        schema_valid=outcomes.count("ok"),
        outcomes=outcomes,
    )


def test_transient_failure_is_not_counted_against_the_arm() -> None:
    arm = _arm(0.0, ["ok", "ok", "transient_api"])
    assert arm.completed == 2
    assert arm.schema_valid_rate == 1.0
    assert arm.summary()["api_or_quota_failures"] == 1


def test_schema_invalid_is_counted() -> None:
    assert _arm(0.2, ["ok", "invalid_schema"]).schema_valid_rate == 0.5


def test_earlier_proxy_sweep_shape_no_longer_penalizes_a_503() -> None:
    """The committed proxy sweep had 0.0 = [ok, ok, transient_api], 0.2 = [ok, ok, ok]."""
    arms = {
        0.0: _arm(0.0, ["ok", "ok", "transient_api"]),
        0.2: _arm(0.2, ["ok", "ok", "ok"]),
        1.0: _arm(1.0, ["ok", "invalid_schema", "ok"]),
    }
    assert choose_temperature(arms) == 0.0


def test_no_choice_without_a_measurement_on_every_arm() -> None:
    arms = {
        0.0: _arm(0.0, ["daily_quota_exhausted", "skipped_quota_exhausted"]),
        0.2: _arm(0.2, ["ok"]),
    }
    assert choose_temperature(arms) is None
