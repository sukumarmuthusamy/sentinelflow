"""A streaming ASGI client for SSE tests.

``httpx.ASGITransport`` buffers the whole response body before returning, so it
cannot observe an open event stream. This client calls the ASGI app directly, the
way uvicorn does (ASGI spec 2.3: Starlette then watches ``receive`` for
``http.disconnect``), hands out body bytes as they are sent, parses SSE frames
incrementally, and can disconnect mid-stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from starlette.types import ASGIApp, Message

LOOPBACK = ("127.0.0.1", 50001)


@dataclass
class SseEvent:
    event: str | None = None
    id: str | None = None
    data: str = ""
    retry: int | None = None
    comment: str | None = None

    @property
    def json(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.data)
        return value


@dataclass
class SseStream:
    status: int
    headers: dict[str, str]
    raw: bytearray = field(default_factory=bytearray)


class AsgiSseClient:
    def __init__(
        self,
        app: ASGIApp,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        client: tuple[str, int] = LOOPBACK,
    ) -> None:
        self._app = app
        self._path = path
        self._headers = headers or {}
        self._client = client
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._started = asyncio.Event()
        self._disconnect = asyncio.Event()
        # Cleared by hold(): body sends then block, like a client that stopped reading
        # (TCP back-pressure), so the server-side generator stalls.
        self._flowing = asyncio.Event()
        self._flowing.set()
        self._request_sent = False
        self._task: asyncio.Task[None] | None = None
        self._buffer = ""
        self._ended = False
        self.status = 0
        self.headers: dict[str, str] = {}
        self.body = bytearray()

    async def __aenter__(self) -> AsgiSseClient:
        path, _, query = self._path.partition("?")
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"accept", b"text/event-stream"),
                *[
                    (name.lower().encode("latin-1"), value.encode("latin-1"))
                    for name, value in self._headers.items()
                ],
            ],
            "client": self._client,
            "server": ("testserver", 80),
        }
        self._task = asyncio.create_task(self._app(scope, self._receive, self._send))
        started = asyncio.ensure_future(self._started.wait())
        await asyncio.wait({started, self._task}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        started.cancel()
        if not self._started.is_set():
            if self._task.done():
                self._task.result()  # surface the app's exception
            msg = "no response start within 10s"
            raise AssertionError(msg)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.disconnect()

    async def _receive(self) -> Message:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {
                name.decode("latin-1").lower(): value.decode("latin-1")
                for name, value in message.get("headers", [])
            }
            self._started.set()
            return
        if message["type"] == "http.response.body":
            await self._flowing.wait()
            body = message.get("body", b"")
            if body:
                self.body.extend(body)
                await self._chunks.put(body)
            if not message.get("more_body", False):
                await self._chunks.put(None)

    def hold(self) -> None:
        self._flowing.clear()

    def release(self) -> None:
        self._flowing.set()

    async def disconnect(self) -> None:
        self._flowing.set()
        self._disconnect.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(self._task, timeout=10)

    @property
    def finished(self) -> bool:
        return self._task is not None and self._task.done()

    async def next_event(self, timeout: float = 10.0) -> SseEvent | None:
        """Next frame (event or comment); ``None`` once the response has ended."""
        while True:
            if "\n\n" in self._buffer:
                frame, self._buffer = self._buffer.split("\n\n", 1)
                return _parse(frame)
            if self._ended:
                return None
            chunk = await asyncio.wait_for(self._chunks.get(), timeout)
            if chunk is None:
                self._ended = True
                continue
            self._buffer += chunk.decode("utf-8")

    async def events_until_end(self, timeout: float = 20.0) -> list[SseEvent]:
        """Every frame until the server closes the stream."""
        frames: list[SseEvent] = []

        async def collect() -> None:
            while (frame := await self.next_event(timeout)) is not None:
                frames.append(frame)

        await asyncio.wait_for(collect(), timeout)
        return frames

    async def wait_for(
        self,
        predicate: Any,
        *,
        timeout: float = 10.0,
    ) -> list[SseEvent]:
        """Frames up to and including the first one matching ``predicate``."""
        frames: list[SseEvent] = []

        async def collect() -> None:
            while True:
                frame = await self.next_event(timeout)
                if frame is None:
                    msg = "stream ended before the awaited event"
                    raise AssertionError(msg)
                frames.append(frame)
                if predicate(frame):
                    return

        await asyncio.wait_for(collect(), timeout)
        return frames


def _parse(frame: str) -> SseEvent:
    event = SseEvent()
    data: list[str] = []
    for line in frame.split("\n"):
        if line.startswith(":"):
            event.comment = line[1:].strip()
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event.event = value
        elif name == "id":
            event.id = value
        elif name == "data":
            data.append(value)
        elif name == "retry":
            event.retry = int(value)
    event.data = "\n".join(data)
    return event


def durable(frames: list[SseEvent]) -> list[SseEvent]:
    return [frame for frame in frames if frame.id is not None]


def named(frames: list[SseEvent], name: str) -> list[SseEvent]:
    return [frame for frame in frames if frame.event == name]
