"""ASGI-level harness for the M5 API: real app, real routes, injected dependencies.

Every test drives the FastAPI app over HTTP (``httpx`` + ``ASGITransport``) with the
real job store and graph. The analysis runtime is a test double: a static
structured LLM, the in-process ``sentinelflow-context`` MCP server on fixtures and a
fake retriever. No test reaches a provider, GitHub or the network.

The harness is an async context manager used inside each test rather than an async
fixture, because the MCP client's anyio task groups must open and close in one task.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.core.settings import Settings
from app.graph.state import AnalysisRequest
from app.jobs.evaluation import EvaluationRunner, run_deterministic_fixture_suite
from app.jobs.runtime import AnalysisSession
from app.jobs.service import JobService
from app.main import create_app
from app.model_gateway.llm import LLMResult, StaticStructuredLLM, StructuredLLM
from fastapi import FastAPI
from mcp_server.sentinelflow_context.settings import clear_mcp_server_settings_cache
from pydantic import BaseModel

from tests.contract.conftest import mcp_session
from tests.graph.conftest import static_llm_responses
from tests.graph.fakes import DEFAULT_CHUNKS, FakeRetriever

LOOPBACK = ("127.0.0.1", 50000)


class FakeRuntime:
    """``AnalysisRuntime`` double; counts sessions (one per graph execution)."""

    def __init__(
        self,
        llm: StructuredLLM | None = None,
        *,
        wrap_mcp: Callable[[Any], Any] | None = None,
        ready_error: BaseException | None = None,
        session_error: BaseException | None = None,
        stream_tokens: bool = False,
    ) -> None:
        self.llm: StructuredLLM = llm or StaticStructuredLLM(static_llm_responses())
        self.stream_tokens = stream_tokens
        self.wrap_mcp = wrap_mcp
        self.ready_error = ready_error
        self.session_error = session_error
        self.sessions = 0
        self.ready_checks: list[AnalysisRequest] = []

    def check_ready(self, request: AnalysisRequest) -> None:
        self.ready_checks.append(request)
        if self.ready_error is not None:
            raise self.ready_error

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[AnalysisSession]:
        if self.session_error is not None:
            raise self.session_error
        async with mcp_session() as mcp:
            client = self.wrap_mcp(mcp) if self.wrap_mcp is not None else mcp
            yield AnalysisSession(
                mcp=client,
                llm=self.llm,
                retriever=FakeRetriever(DEFAULT_CHUNKS),
                enable_retrieval=False,
                stream_tokens=self.stream_tokens,
            )

    def session(self) -> AbstractAsyncContextManager[AnalysisSession]:
        self.sessions += 1
        return self._open()


class BlockingLLM(StaticStructuredLLM):
    """Static LLM whose calls wait until ``release()``: holds a job in ``running``."""

    def __init__(self, responses: dict[type[BaseModel], BaseModel] | None = None) -> None:
        super().__init__(responses or static_llm_responses())
        self.entered = asyncio.Event()
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def generate(self, **kwargs: Any) -> LLMResult[Any]:
        self.entered.set()
        await self._gate.wait()
        return await super().generate(**kwargs)


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    runtime: FakeRuntime
    settings: Settings
    audit_log_path: Path
    responses: list[httpx.Response] = field(default_factory=list)

    @property
    def service(self) -> JobService:
        service: JobService = self.app.state.job_service
        return service

    async def idle(self) -> None:
        await self.service.wait_idle()

    async def create(
        self,
        body: dict[str, Any] | None = None,
        *,
        key: str | None = None,
    ) -> httpx.Response:
        headers = {"Idempotency-Key": key} if key is not None else {}
        payload = body or {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"}
        return await self.client.post("/v1/analyses", json=payload, headers=headers)

    async def run_to_rest(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create an analysis, let it run to completion or approval, return its view."""
        created = await self.create(body)
        assert created.status_code == 202, created.text
        await self.idle()
        view = await self.client.get(created.headers["Location"])
        assert view.status_code == 200
        payload: dict[str, Any] = view.json()
        return payload

    def audit_lines(self) -> list[str]:
        if not self.audit_log_path.exists():
            return []
        text = self.audit_log_path.read_text(encoding="utf-8")
        return [line for line in text.splitlines() if line.strip()]


HarnessFactory = Callable[..., AbstractAsyncContextManager[Harness]]


def api_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "graph_checkpoint_path": tmp_path / "checkpoints.sqlite",
        "api_job_db_path": tmp_path / "jobs.sqlite",
        "analysis_max_llm_calls": 50,
        # Never read the developer's .env: no key reaches these tests.
        "gemini_api_key": None,
        "openrouter_api_key": None,
        "api_key": None,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HarnessFactory:
    audit = tmp_path / "audit" / "gate_decisions.jsonl"
    monkeypatch.setenv("SENTINELFLOW_AUDIT_LOG_PATH", str(audit))
    clear_mcp_server_settings_cache()

    @asynccontextmanager
    async def _open(
        *,
        runtime: FakeRuntime | None = None,
        settings: Settings | None = None,
        client: tuple[str, int] = LOOPBACK,
        evaluation_runner: EvaluationRunner = run_deterministic_fixture_suite,
        raise_app_exceptions: bool = True,
        **setting_overrides: Any,
    ) -> AsyncIterator[Harness]:
        cfg = settings or api_settings(tmp_path, **setting_overrides)
        chosen = runtime or FakeRuntime()
        app = create_app(cfg, runtime=chosen, evaluation_runner=evaluation_runner)
        transport = httpx.ASGITransport(
            app=app,
            client=client,
            raise_app_exceptions=raise_app_exceptions,
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://testserver") as http,
        ):
            yield Harness(
                app=app,
                client=http,
                runtime=chosen,
                settings=cfg,
                audit_log_path=audit,
            )

    yield _open
    clear_mcp_server_settings_cache()
