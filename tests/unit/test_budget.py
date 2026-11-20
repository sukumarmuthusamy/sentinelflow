"""Token and call budgets are enforced, not just documented."""

from __future__ import annotations

import pytest
from app.graph.budget import AnalysisBudget, BudgetExhaustedError


def _budget(**overrides: int) -> AnalysisBudget:
    values = {
        "max_llm_calls": 10,
        "max_input_tokens": 1_000,
        "max_output_tokens": 500,
        "max_parallel_agents": 2,
    }
    values.update(overrides)
    return AnalysisBudget(**values)


def test_record_never_raises_and_next_check_does() -> None:
    budget = _budget()
    budget.check_llm_call()
    budget.record_llm_call(input_tokens=1_200, output_tokens=10)
    with pytest.raises(BudgetExhaustedError, match="max_input_tokens"):
        budget.check_llm_call()


def test_output_token_budget_enforced() -> None:
    budget = _budget()
    budget.record_llm_call(input_tokens=1, output_tokens=500)
    with pytest.raises(BudgetExhaustedError, match="max_output_tokens"):
        budget.check_llm_call()


def test_call_budget_enforced() -> None:
    budget = _budget(max_llm_calls=1)
    budget.record_llm_call()
    with pytest.raises(BudgetExhaustedError, match="max_llm_calls"):
        budget.check_llm_call()


def test_parallel_slot_limit() -> None:
    budget = _budget(max_parallel_agents=1)
    with budget.parallel_slot(), pytest.raises(BudgetExhaustedError), budget.parallel_slot():
        pass
