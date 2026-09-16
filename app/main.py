"""FastAPI application entrypoint."""

from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.settings import get_settings

settings = get_settings()

app = FastAPI(
    title="SentinelFlow",
    version="0.1.0",
    description="Evidence-grounded release-risk intelligence platform",
    debug=settings.debug,
)

app.include_router(health_router)
