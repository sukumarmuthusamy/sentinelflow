"""FastAPI application entrypoint.

``create_app`` wires settings, the job store, the analysis runtime and the routes.
Tests pass their own runtime (static LLM, fake retrieval) and temporary paths.
Importing this module has no side effects: the job database is opened and recovery
runs in the lifespan, when a server (or an ASGI test client) starts the app.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import install_error_handlers
from app.api.health import router as health_router
from app.api.security import RequestGuardMiddleware
from app.api.v1 import router as v1_router
from app.core.settings import Settings, get_settings
from app.jobs.evaluation import EvaluationRunner, run_deterministic_fixture_suite
from app.jobs.runtime import AnalysisRuntime, DefaultAnalysisRuntime
from app.jobs.service import JobService
from app.persistence.job_store import SqliteJobStore


def create_app(
    settings: Settings | None = None,
    *,
    runtime: AnalysisRuntime | None = None,
    evaluation_runner: EvaluationRunner = run_deterministic_fixture_suite,
) -> FastAPI:
    cfg = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = JobService(
            settings=cfg,
            store=SqliteJobStore(cfg.api_job_db_path),
            runtime=runtime or DefaultAnalysisRuntime(cfg),
            evaluation_runner=evaluation_runner,
        )
        app.state.job_service = service
        await service.start()
        try:
            yield
        finally:
            await service.shutdown()

    app = FastAPI(
        title="SentinelFlow",
        version="0.1.0",
        description="Evidence-grounded release-risk intelligence platform (local harness)",
        # Never cfg.debug: Starlette's debug mode answers a 500 with the traceback.
        debug=False,
        lifespan=lifespan,
    )
    app.state.settings = cfg
    install_error_handlers(app)
    app.add_middleware(RequestGuardMiddleware, max_body_bytes=cfg.api_max_body_bytes)
    app.include_router(health_router)
    app.include_router(v1_router)
    return app


app = create_app()
