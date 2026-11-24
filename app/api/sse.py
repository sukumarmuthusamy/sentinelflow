"""``text/event-stream`` response for a job's event stream (ADR-0003 "Streaming")."""

from __future__ import annotations

from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from app.jobs.streaming import JobEventStream

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Reverse proxies (nginx) must not buffer an event stream.
    "X-Accel-Buffering": "no",
}


class EventStreamResponse(StreamingResponse):
    """Streams ``JobEventStream.frames()`` and always releases its subscription.

    The ``finally`` here also covers a client that disconnects before the first
    frame, when the frame generator never starts and so never runs its own cleanup.
    Disconnecting cancels only this response, never the job.
    """

    media_type = "text/event-stream"

    def __init__(self, stream: JobEventStream) -> None:
        super().__init__(stream.frames(), headers=SSE_HEADERS)
        self._stream = stream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._stream.close()
