"""A real SentinelFlow API on a loopback socket, for the harness's HTTP tests.

The app is ``create_app`` with the same test doubles as ``tests/api`` (static or
streaming fake LLM, in-process MCP server on fixtures, fake retriever), served by
uvicorn in a background thread on ``127.0.0.1:<ephemeral port>``. The harness
talks to it over real TCP, exactly as it talks to ``python -m app.api.serve``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from app.jobs.service import JobService
from app.main import create_app
from app.model_gateway.llm import LLMResult, StaticStructuredLLM
from fastapi import FastAPI
from mcp_server.sentinelflow_context.settings import clear_mcp_server_settings_cache

from tests.api.conftest import FakeRuntime, api_settings
from tests.graph.conftest import static_llm_responses


class GateLLM(StaticStructuredLLM):
    """Static LLM whose calls wait on a thread event: holds a job in ``running``.

    Released from the test thread (the server runs in another thread and loop).
    """

    def __init__(self) -> None:
        super().__init__(static_llm_responses())
        self.entered = threading.Event()
        self.gate = threading.Event()

    async def generate(self, **kwargs: Any) -> LLMResult[Any]:
        self.entered.set()
        await asyncio.to_thread(self.gate.wait, 30)
        return await super().generate(**kwargs)


@dataclass
class LiveApi:
    base_url: str
    app: FastAPI
    runtime: FakeRuntime
    audit_log_path: Path

    @property
    def service(self) -> JobService:
        service: JobService = self.app.state.job_service
        return service

    def job_count(self) -> int:
        store = self.service.store
        with store._connect() as connection:  # test-side check of the server's store
            row = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()
        return int(row[0])

    def wait_for(self, job_id: str, statuses: set[str], *, timeout: float = 30) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with httpx.Client(base_url=self.base_url, timeout=5) as http:
            while True:
                kind = "analyses" if job_id in self._analyses() else "evaluations"
                view: dict[str, Any] = http.get(f"/v1/{kind}/{job_id}").json()
                if view.get("status") in statuses:
                    return view
                if time.monotonic() > deadline:
                    raise AssertionError(f"{job_id} stayed {view.get('status')}")
                time.sleep(0.05)

    def released(self, job_id: str, *, timeout: float = 5) -> bool:
        """True once the server holds no SSE subscription for the job.

        Over TCP the server learns about a closed connection asynchronously.
        """
        deadline = time.monotonic() + timeout
        while self.service.bus.subscriber_count(job_id) != 0:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def _analyses(self) -> set[str]:
        store = self.service.store
        with store._connect() as connection:
            rows = connection.execute("SELECT job_id FROM jobs WHERE kind = 'analysis'").fetchall()
        return {str(row[0]) for row in rows}


LiveApiFactory = Callable[..., AbstractContextManager[LiveApi]]


@pytest.fixture
def live_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LiveApiFactory]:
    audit = tmp_path / "audit" / "gate_decisions.jsonl"
    monkeypatch.setenv("SENTINELFLOW_AUDIT_LOG_PATH", str(audit))
    clear_mcp_server_settings_cache()

    @contextmanager
    def _serve(*, runtime: FakeRuntime | None = None, **overrides: Any) -> Iterator[LiveApi]:
        settings = api_settings(tmp_path, **overrides)
        chosen = runtime or FakeRuntime()
        app = create_app(settings, runtime=chosen)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="on",
            timeout_graceful_shutdown=5,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="live-api", daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("the test API server did not start")
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        try:
            yield LiveApi(
                base_url=f"http://127.0.0.1:{port}",
                app=app,
                runtime=chosen,
                audit_log_path=audit,
            )
        finally:
            server.should_exit = True
            thread.join(timeout=20)

    yield _serve
    clear_mcp_server_settings_cache()


def closed_port_url() -> str:
    """A loopback URL nothing listens on (the port was just released)."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"
