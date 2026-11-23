"""Token and call budgets are enforced, not just documented."""

from __future__ import annotations

import asyncio

import pytest
from app.graph.budget import AnalysisBudget, BudgetExhaustedError, TraceAccumulator


def _budget(**overrides: int) -> AnalysisBudget:
    values = {
        "max_llm_calls": 10,
        "max_input_tokens": 1_000,
        "max_output_tokens": 500,
        "max_parallel_agents": 2,
    }
    values.update(overrides)
    return AnalysisBudget(**values)


def test_settling_never_raises_and_next_reservation_does() -> None:
    budget = _budget()
    budget.reserve_llm_call().complete(input_tokens=1_200, output_tokens=10)
    with pytest.raises(BudgetExhaustedError, match="max_input_tokens"):
        budget.reserve_llm_call()


def test_output_token_budget_enforced() -> None:
    budget = _budget()
    budget.reserve_llm_call().complete(input_tokens=1, output_tokens=500)
    with pytest.raises(BudgetExhaustedError, match="max_output_tokens"):
        budget.check_llm_call()


def test_call_budget_enforced() -> None:
    budget = _budget(max_llm_calls=1)
    budget.reserve_llm_call().complete()
    with pytest.raises(BudgetExhaustedError, match="max_llm_calls"):
        budget.reserve_llm_call()


def test_in_flight_reservations_count_against_the_limit() -> None:
    budget = _budget(max_llm_calls=2)
    first = budget.reserve_llm_call()
    second = budget.reserve_llm_call()
    with pytest.raises(BudgetExhaustedError, match="max_llm_calls"):
        budget.reserve_llm_call()
    with pytest.raises(BudgetExhaustedError):
        budget.check_llm_call()
    assert budget.llm_calls_in_flight == 2
    first.complete()
    second.fail()
    assert (budget.llm_calls, budget.llm_calls_failed, budget.llm_calls_in_flight) == (1, 1, 0)


def test_failed_calls_are_not_refunded() -> None:
    budget = _budget(max_llm_calls=1)
    budget.reserve_llm_call().fail(input_tokens=40, output_tokens=7)
    assert budget.input_tokens == 40
    assert budget.output_tokens == 7
    with pytest.raises(BudgetExhaustedError, match="max_llm_calls"):
        budget.reserve_llm_call()


def test_check_does_not_reserve() -> None:
    budget = _budget(max_llm_calls=1)
    budget.check_llm_call()
    budget.check_llm_call()
    assert budget.committed_llm_calls == 0
    budget.reserve_llm_call()


def test_reservation_settles_exactly_once() -> None:
    reservation = _budget().reserve_llm_call()
    reservation.complete()
    with pytest.raises(RuntimeError, match="settled twice"):
        reservation.fail()


@pytest.mark.asyncio
async def test_concurrent_callers_cannot_overshoot_max_llm_calls() -> None:
    """Regression: five simultaneous callers against max_llm_calls=2."""
    budget = _budget(max_llm_calls=2, max_parallel_agents=5)
    gate = asyncio.Event()
    started: list[int] = []

    async def caller(index: int) -> str:
        try:
            reservation = budget.reserve_llm_call()
        except BudgetExhaustedError:
            return "refused"
        started.append(index)
        await gate.wait()  # every admitted call is in flight at the same time
        reservation.complete(input_tokens=1, output_tokens=1)
        return "ok"

    tasks = [asyncio.create_task(caller(i)) for i in range(5)]
    await asyncio.sleep(0)
    gate.set()
    outcomes = await asyncio.gather(*tasks)
    assert outcomes.count("ok") == 2
    assert outcomes.count("refused") == 3
    assert budget.llm_calls == 2
    assert budget.committed_llm_calls == 2


@pytest.mark.asyncio
async def test_token_overshoot_is_bounded_by_calls_in_flight() -> None:
    """Token usage is only known afterwards: in-flight calls may overshoot, nothing more."""
    budget = _budget(max_llm_calls=100, max_input_tokens=1_000, max_parallel_agents=4)
    gate = asyncio.Event()

    async def caller() -> bool:
        try:
            reservation = budget.reserve_llm_call()
        except BudgetExhaustedError:
            return False
        await gate.wait()
        reservation.complete(input_tokens=900)
        return True

    # Four calls start while settled usage is still 0; each then reports 900 tokens.
    first_wave = [asyncio.create_task(caller()) for _ in range(4)]
    await asyncio.sleep(0)
    gate.set()
    assert all(await asyncio.gather(*first_wave))
    assert budget.input_tokens == 3_600  # overshoot = usage of the 4 in-flight calls
    # Once settled usage is over the limit, no further call starts.
    assert not await caller()


def test_trace_counts_failed_calls_with_their_tokens() -> None:
    trace = TraceAccumulator()
    trace.bump_llm(input_tokens=10, output_tokens=2)
    trace.bump_llm_failed(input_tokens=5, output_tokens=1)
    assert (trace.llm_calls, trace.llm_calls_failed) == (1, 1)
    assert (trace.input_tokens, trace.output_tokens) == (15, 3)


# Parallel-agent limits are a semaphore in the graph context, covered by
# tests/graph/test_degradation_paths.py::test_parallel_limit_throttles_instead_of_crashing.
