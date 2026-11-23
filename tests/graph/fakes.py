"""Offline test doubles for graph tests: retrieval, LLM failures, MCP failures."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from app.mcp_client.errors import McpToolError
from app.mcp_client.port import McpClientPort
from app.model_gateway.llm import LLMResult, StaticStructuredLLM, StructuredOutputError
from app.rag.models import ChunkMetadata, DocumentType, RetrievedChunk
from pydantic import BaseModel


def make_chunk(
    document_id: str,
    section_id: str,
    content: str,
    *,
    document_type: DocumentType = DocumentType.REQUIREMENT,
    service: str | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        content=content,
        metadata=ChunkMetadata(
            document_id=document_id,
            section_id=section_id,
            document_type=document_type,
            service=service,
            version="1",
            effective_date="2026-01-01",
            source_path=f"knowledge/{document_id}.md",
            domain_pack_id="test",
        ),
    )


DEFAULT_CHUNKS = [
    make_chunk(
        "req-analytics-docs",
        "s1",
        "Documentation changes to svc-analytics require no regression run.",
        service="svc-analytics",
    ),
    make_chunk(
        "rb-analytics",
        "s2",
        "Runbook: svc-analytics restarts are safe during business hours.",
        document_type=DocumentType.RUNBOOK,
        service="svc-analytics",
    ),
]


class FakeRetriever:
    """Returns fixed chunks for every query and records the queries.

    ``security_only`` chunks are returned only for the Security Auditor's query, so a
    test can prove evidence arrived through that branch and no other.
    """

    def __init__(
        self,
        chunks: list[RetrievedChunk],
        *,
        security_only: list[RetrievedChunk] | None = None,
    ) -> None:
        self._chunks = chunks
        self._security_only = security_only or []
        self.queries: list[str] = []

    async def retrieve(self, query: str, *, k: int | None = None) -> list[RetrievedChunk]:
        self.queries.append(query)
        chunks = list(self._chunks)
        if "security exposure" in query or "secrets and data exposure" in query:
            chunks += self._security_only
        return chunks[: k or len(chunks)]


class FailingLLM(StaticStructuredLLM):
    """Static LLM that raises StructuredOutputError for the given schemas."""

    def __init__(
        self,
        responses: dict[type[BaseModel], BaseModel],
        *,
        fail_on: set[type[BaseModel]],
    ) -> None:
        super().__init__(responses)
        self._fail_on = fail_on

    async def generate(self, **kwargs: Any) -> LLMResult[Any]:
        schema = kwargs["schema"]
        if schema in self._fail_on:
            self.calls.append(f"{schema.__name__}!failed")
            msg = f"{schema.__name__} did not validate: simulated"
            raise StructuredOutputError(msg)
        return await super().generate(**kwargs)


class ConcurrencyLLM(StaticStructuredLLM):
    """Static LLM that records the peak number of overlapping calls."""

    def __init__(self, responses: dict[type[BaseModel], BaseModel]) -> None:
        super().__init__(responses)
        self.in_flight = 0
        self.peak = 0

    async def generate(self, **kwargs: Any) -> LLMResult[Any]:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.02)
            return await super().generate(**kwargs)
        finally:
            self.in_flight -= 1


class FailingToolMcp:
    """Wraps a real MCP adapter and fails one tool with an MCP tool error."""

    def __init__(self, inner: McpClientPort, *, failing_tool: str) -> None:
        self._inner = inner
        self._failing_tool = failing_tool
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def call_tool_typed(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        response_model: type[Any],
    ) -> Any:
        self.calls.append(name)
        if name == self._failing_tool:
            raise McpToolError(tool_name=name, message="simulated tool failure")
        return await self._inner.call_tool_typed(name, arguments, response_model=response_model)


class GatedLLM(StaticStructuredLLM):
    """Static LLM that holds each call until ``release_at`` calls are in flight.

    Forces branches to overlap inside ``generate`` deterministically. Calls the
    budget refuses never arrive, so the gate also opens after ``timeout`` seconds.
    """

    def __init__(
        self,
        responses: dict[type[BaseModel], BaseModel],
        *,
        release_at: int,
        timeout: float = 0.5,
    ) -> None:
        super().__init__(responses)
        self._release_at = release_at
        self._timeout = timeout
        self._gate = asyncio.Event()
        self.in_flight = 0
        self.peak = 0

    async def generate(self, **kwargs: Any) -> LLMResult[Any]:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        if self.in_flight >= self._release_at:
            self._gate.set()
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._gate.wait(), timeout=self._timeout)
            return await super().generate(**kwargs)
        finally:
            self.in_flight -= 1


class RaisingLLM(StaticStructuredLLM):
    """Static LLM that raises a given exception for one schema."""

    def __init__(
        self,
        responses: dict[type[BaseModel], BaseModel],
        *,
        schema: type[BaseModel],
        error: BaseException,
    ) -> None:
        super().__init__(responses)
        self._schema = schema
        self._error = error

    async def generate(self, **kwargs: Any) -> LLMResult[Any]:
        if kwargs["schema"] is self._schema:
            self.calls.append(f"{self._schema.__name__}!raised")
            raise self._error
        return await super().generate(**kwargs)
