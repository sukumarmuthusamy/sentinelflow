"""Per-analysis token and step budgets for the LangGraph workflow.

Call-budget semantics
---------------------
``max_llm_calls`` caps *logical* LLM calls: one ``StructuredLLM.generate`` per agent
step. A call reserves capacity before it starts and settles afterwards as completed
or failed. Reserved, completed and failed calls all count against the limit, so
parallel branches cannot all pass a check before any of them records its call, and
a failed call (which may still have reached the provider) is never refunded.

Reservation is check-and-increment with no ``await`` in between, guarded by a lock,
so it is atomic for coroutines on one event loop and for threads.

Gateway-level transient retries (5xx/overload only, ``transient_retries``) happen
inside one logical call. Provider attempts are therefore bounded by
``max_llm_calls * (1 + transient_retries)``; 429 and quota errors are never retried.

Token semantics
---------------
Token usage is only known after the provider answers, so token limits cannot be a
strict ceiling. They are enforced at reservation time against *settled* usage: once
recorded input or output tokens reach their limit, no new call starts. The overshoot
is bounded by the usage of calls already in flight, at most ``max_parallel_agents``
calls (the parallel semaphore width).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

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
    # Settled calls: completed returned a value, failed raised after reserving.
    llm_calls: int = 0
    llm_calls_failed: int = 0
    llm_calls_in_flight: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> AnalysisBudget:
        cfg = settings or get_settings()
        return cls(
            max_llm_calls=cfg.analysis_max_llm_calls,
            max_input_tokens=cfg.analysis_max_input_tokens,
            max_output_tokens=cfg.analysis_max_output_tokens,
            max_parallel_agents=cfg.analysis_max_parallel_agents,
        )

    @property
    def committed_llm_calls(self) -> int:
        """Calls that count against the limit: completed, failed and in flight."""
        return self.llm_calls + self.llm_calls_failed + self.llm_calls_in_flight

    def _exhausted_reason(self) -> str | None:
        if self.committed_llm_calls >= self.max_llm_calls:
            return "max_llm_calls exceeded"
        if self.input_tokens >= self.max_input_tokens:
            return "max_input_tokens exceeded"
        if self.output_tokens >= self.max_output_tokens:
            return "max_output_tokens exceeded"
        return None

    def check_llm_call(self) -> None:
        """Raise when no further call could start. Reserves nothing (for guards)."""
        with self._lock:
            reason = self._exhausted_reason()
        if reason is not None:
            raise BudgetExhaustedError(reason)

    def reserve_llm_call(self) -> LLMCallReservation:
        """Atomically check capacity and reserve one call, or raise."""
        with self._lock:
            reason = self._exhausted_reason()
            if reason is None:
                self.llm_calls_in_flight += 1
        if reason is not None:
            raise BudgetExhaustedError(reason)
        return LLMCallReservation(self)

    def _settle(self, *, failed: bool, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            self.llm_calls_in_flight -= 1
            if failed:
                self.llm_calls_failed += 1
            else:
                self.llm_calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens


@dataclass
class LLMCallReservation:
    """One reserved call; settle exactly once with ``complete`` or ``fail``.

    Settling never raises on overrun: the call already happened and its output is
    kept. Overruns stop the *next* reservation.
    """

    budget: AnalysisBudget
    settled: bool = False

    def _settle(self, *, failed: bool, input_tokens: int, output_tokens: int) -> None:
        if self.settled:
            msg = "LLM call reservation settled twice"
            raise RuntimeError(msg)
        self.settled = True
        self.budget._settle(failed=failed, input_tokens=input_tokens, output_tokens=output_tokens)

    def complete(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self._settle(failed=False, input_tokens=input_tokens, output_tokens=output_tokens)

    def fail(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self._settle(failed=True, input_tokens=input_tokens, output_tokens=output_tokens)


@dataclass
class TraceAccumulator:
    """Mutable trace metrics mirrored into graph state."""

    agent_steps: int = 0
    # Completed LLM calls; failed ones are counted separately.
    llm_calls: int = 0
    llm_calls_failed: int = 0
    mcp_tool_calls: int = 0
    retrieval_queries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    citations_rejected: int = 0
    repair_count: int = 0

    def bump_llm(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.llm_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def bump_llm_failed(self, *, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.llm_calls_failed += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
