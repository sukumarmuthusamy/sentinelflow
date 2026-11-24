"""Access and input limits for the local harness (M5 is not a public service).

* With no ``API_KEY`` configured, ``/v1`` answers loopback clients only. With a key,
  it requires ``Authorization: Bearer <key>`` from every client, loopback included.
  Behind a reverse proxy every client looks like loopback, so a key is required there.
* Request bodies are capped at ``api_max_body_bytes`` before routing. A body without
  ``Content-Length`` (chunked) is buffered up to the cap and refused beyond it.
* Every response carries an ``X-Request-ID`` generated here (client values are not
  trusted), and error bodies repeat it.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import uuid
from typing import Any

from fastapi import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import ErrorCode, SentinelFlowError
from app.core.settings import Settings


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def require_api_access(request: Request) -> None:
    """FastAPI dependency guarding every ``/v1`` route."""
    settings: Settings = request.app.state.settings
    configured = settings.api_key.get_secret_value().strip() if settings.api_key else ""
    if configured:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        valid = scheme.lower() == "bearer" and hmac.compare_digest(
            token.strip().encode("utf-8"),
            configured.encode("utf-8"),
        )
        if not valid:
            raise SentinelFlowError("Missing or invalid API key", code=ErrorCode.UNAUTHORIZED)
        return
    client = request.client.host if request.client else None
    if not _is_loopback(client):
        raise SentinelFlowError(
            "This API accepts loopback clients only unless API_KEY is configured",
            code=ErrorCode.NON_LOCAL_CLIENT,
        )


class RequestGuardMiddleware:
    """Pure ASGI middleware: request id and body-size cap (before any routing)."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self._app = app
        self._max = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(name.lower() == b"x-request-id" for name, _ in headers):
                    headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        declared = _content_length(scope)
        if declared is not None and declared > self._max:
            await self._reject(send_with_id, request_id)
            return
        if declared is None and scope.get("method") in {"POST", "PUT", "PATCH"}:
            body, too_large = await _buffer(receive, self._max)
            if too_large:
                await self._reject(send_with_id, request_id)
                return
            receive = _replay(body, receive)
        await self._app(scope, receive, send_with_id)

    async def _reject(self, send: Send, request_id: str) -> None:
        payload: dict[str, Any] = {
            "error": {
                "code": ErrorCode.REQUEST_TOO_LARGE.value,
                "message": "Request body too large",
                "details": {"limit_bytes": self._max},
                "request_id": request_id,
            },
        }
        body = json.dumps(payload).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            },
        )
        await send({"type": "http.response.body", "body": body})


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _buffer(receive: Receive, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return b"", True
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks), False


def _replay(body: bytes, downstream: Receive) -> Receive:
    """Hand the buffered body over once, then defer to the real channel (disconnects)."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await downstream()

    return receive
