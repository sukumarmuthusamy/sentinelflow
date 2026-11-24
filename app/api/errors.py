"""One machine-readable error envelope for every API failure.

``{"error": {"code", "message", "details", "request_id"}}``. Handlers never echo
request input, exception text or tracebacks: validation errors keep the location,
message and type of each problem and drop the offending value; unexpected exceptions
become ``INTERNAL_ERROR`` with the type name logged server-side only.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import ErrorCode, SentinelFlowError

logger = logging.getLogger(__name__)

HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 422,
    ErrorCode.INPUT_ERROR: 422,
    ErrorCode.REQUEST_TOO_LARGE: 413,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.NON_LOCAL_CLIENT: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.DOMAIN_PACK_NOT_FOUND: 404,
    ErrorCode.DOMAIN_PACK_INVALID: 422,
    ErrorCode.CHANGE_NOT_FOUND: 404,
    ErrorCode.CHANGE_SOURCE_NOT_CONFIGURED: 422,
    ErrorCode.PROVIDER_NOT_CONFIGURED: 503,
    ErrorCode.IDEMPOTENCY_KEY_CONFLICT: 409,
    ErrorCode.TOO_MANY_ACTIVE_JOBS: 429,
    ErrorCode.JOB_NOT_FOUND: 404,
    ErrorCode.INVALID_JOB_TRANSITION: 409,
    ErrorCode.APPROVAL_NOT_REQUIRED: 409,
    ErrorCode.APPROVAL_ALREADY_SUBMITTED: 409,
    ErrorCode.RESULT_NOT_AVAILABLE: 409,
    ErrorCode.TOO_MANY_SUBSCRIBERS: 429,
    ErrorCode.INTERNAL_ERROR: 500,
}

# Detail keys that may leave the process. Anything else a raiser attaches (paths,
# parser output, exception text) is dropped here as a second line of defence.
_SAFE_DETAIL_KEYS = frozenset(
    {
        "approved",
        "change_id",
        "domain_pack_id",
        "errors",
        "field",
        "job_id",
        "latest_event_id",
        "limit_bytes",
        "max_active_jobs",
        "max_subscribers",
        "missing_settings",
        "source",
        "status",
    },
)


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBody


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value) if value else None


def error_response(
    request: Request,
    code: ErrorCode,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    safe = {key: value for key, value in (details or {}).items() if key in _SAFE_DETAIL_KEYS}
    request_id = _request_id(request)
    body = ErrorResponse(
        error=ErrorBody(code=code, message=message, details=safe, request_id=request_id),
    )
    merged = dict(headers or {})
    if request_id:
        # Set here too: a 500 is sent by Starlette's outermost error middleware, past
        # the middleware that normally adds the header.
        merged["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=HTTP_STATUS.get(code, 500),
        content=body.model_dump(mode="json"),
        headers=merged,
    )


def _validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    for item in exc.errors()[:20]:
        # "input" and "ctx" can echo the submitted value; only shape is reported.
        problems.append(
            {
                "loc": [str(part) for part in item.get("loc", ())],
                "type": str(item.get("type", "")),
                "msg": str(item.get("msg", ""))[:200],
            },
        )
    return problems


async def _handle_domain(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, SentinelFlowError)
    return error_response(request, exc.code, exc.message, exc.details)


async def _handle_validation(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return error_response(
        request,
        ErrorCode.INVALID_REQUEST,
        "Request validation failed",
        {"errors": _validation_errors(exc)},
    )


async def _handle_http(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404:
        return error_response(request, ErrorCode.NOT_FOUND, "Not found")
    if exc.status_code == 405:
        return error_response(request, ErrorCode.METHOD_NOT_ALLOWED, "Method not allowed")
    return error_response(request, ErrorCode.INVALID_REQUEST, "Bad request")


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path)
    return error_response(request, ErrorCode.INTERNAL_ERROR, "Internal error")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(SentinelFlowError, _handle_domain)
    app.add_exception_handler(RequestValidationError, _handle_validation)
    app.add_exception_handler(StarletteHTTPException, _handle_http)
    app.add_exception_handler(Exception, _handle_unexpected)
