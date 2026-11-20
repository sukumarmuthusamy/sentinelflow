"""Per-analysis token and step budgets for the LangGraph workflow."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.settings import Settings, get_settings


@dataclass
class BudgetExhaustedError(Exception):
    """Raised when an analysis exceeds configured limits."""

    reason: str


@dataclass
class AnalysisBudget:
    """Tracks LLM usage and enforces limits during a graph run."""

    max_llm_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_parallel_agents: int
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    active_parallel: int = 0

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> AnalysisBudget:
        cfg = settings or get_settings()
        return cls(
            max_llm_calls=cfg.analysis_max_llm_calls,
            max_input_tokens=cfg.analysis_max_input_tokens,
            max_output_tokens=cfg.analysis_max_output_tokens,
            max_parallel_agents=cfg.analysis_max_parallel_agents,
        )

    def check_llm_call(self) -> None:
        if self.llm_calls >= self.max_llm_calls:
            raise BudgetExhaustedError("max_llm_calls exceeded")

    def record_llm_call(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.check_llm_call()
        self.llm_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if self.input_tokens > self.max_input_tokens:
            raise BudgetExhaustedError("max_input_tokens exceeded")
        if self.output_tokens > self.max_output_tokens:
            raise BudgetExhaustedError("max_output_tokens exceeded")

    @dataclass
    class ParallelSlot:
        budget: AnalysisBudget

        def __enter__(self) -> None:
            if self.budget.active_parallel >= self.budget.max_parallel_agents:
                raise BudgetExhaustedError("max_parallel_agents exceeded")
            self.budget.active_parallel += 1

        def __exit__(self, *_exc: object) -> None:
            self.budget.active_parallel = max(0, self.budget.active_parallel - 1)

    def parallel_slot(self) -> ParallelSlot:
        return AnalysisBudget.ParallelSlot(self)


@dataclass
class TraceAccumulator:
    """Mutable trace metrics mirrored into graph state."""

    agent_steps: int = 0
    llm_calls: int = 0
    mcp_tool_calls: int = 0
    retrieval_queries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def bump_llm(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.llm_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
