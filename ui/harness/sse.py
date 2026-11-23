"""Incremental Server-Sent Events parser (WHATWG event-stream format)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SseMessage:
    event: str | None
    id: str | None
    data: str


class SseParser:
    """Feed decoded lines (without line terminators); get a message per blank line.

    Comment lines (``: keepalive``) and ``retry:`` are consumed silently.
    """

    def __init__(self) -> None:
        self._event: str | None = None
        self._id: str | None = None
        self._data: list[str] = []

    def feed_line(self, line: str) -> SseMessage | None:
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            self._data.append(value)
        elif name == "event":
            self._event = value
        elif name == "id" and "\0" not in value:
            self._id = value
        return None

    def _dispatch(self) -> SseMessage | None:
        if not self._data and self._event is None:
            return None
        message = SseMessage(event=self._event, id=self._id, data="\n".join(self._data))
        self._event = None
        self._id = None
        self._data = []
        return message
