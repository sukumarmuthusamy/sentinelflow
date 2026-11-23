"""Synchronous HTTP client for the SentinelFlow ``/v1`` API, as the harness uses it.

Every call goes over HTTP. Errors come back as typed exceptions built from the
API's error envelope (``{"error": {code, message, details, request_id}}``); a
response body that is not that envelope is never echoed, so nothing a proxy or
another server returns (a stack trace, say) reaches the page.

The event stream is read in bounded windows: ``read_events`` connects with
``Last-Event-ID``, reads until the window ends, the job ends, or the job starts
waiting for approval, then closes the connection, which releases the server's
subscription. The next window resumes from the durable cursor, so each
browser session holds at most one subscription, and only while it is reading.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

import httpx

from ui.harness.sse import SseParser

JOB_ID = re.compile(r"^job-[0-9a-f]{32}$")
TERMINAL_EVENTS = frozenset({"job.completed", "job.failed"})
# The stream stays open while a job waits for a human. The harness stops reading
# there and polls the job instead, so an idle approval holds no subscription.
PAUSE_EVENTS = frozenset({"job.waiting_for_approval"})


class ApiUnavailableError(Exception):
    """The API could not be reached, or did not answer in time.

    ``ambiguous`` is True when a request may have reached the server before the
    connection failed (a create may then exist: retry with the same key).
    """

    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.ambiguous = ambiguous


class ApiError(Exception):
    """A sanitized API error: code, message and the request id for log lookup."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        self.request_id = request_id


@dataclass(frozen=True)
class Created:
    job: dict[str, Any]
    replayed: bool


@dataclass
class StreamBatch:
    """What one bounded read of the event stream returned."""

    events: list[dict[str, Any]] = field(default_factory=list)
    # The server ended the stream (after job.completed / job.failed).
    ended: bool = False
    # The job is waiting for approval; reading stopped on purpose.
    paused: bool = False
    # The connection broke mid-stream; the next read resumes from the cursor.
    interrupted: bool = False


def validate_job_id(job_id: str) -> str:
    value = job_id.strip()
    if not JOB_ID.match(value):
        raise ApiError(
            status=0,
            code="INVALID_JOB_ID",
            message="A job id looks like job- followed by 32 lowercase hex characters",
        )
    return value


def is_loopback_url(base_url: str) -> bool:
    host = (urlsplit(base_url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")


class SentinelFlowClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._timeout = timeout_seconds
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SentinelFlowClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- plain requests -------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._json("GET", "/health")

    def create_analysis(self, body: dict[str, Any], *, idempotency_key: str) -> Created:
        return self._create("/v1/analyses", body, idempotency_key)

    def get_analysis(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/analyses/{validate_job_id(job_id)}")

    def get_decision(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/analyses/{validate_job_id(job_id)}/decision")

    def submit_approval(
        self,
        job_id: str,
        *,
        approved: bool,
        reviewer: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"approved": approved, "reviewer": reviewer}
        if comment:
            body["comment"] = comment
        return self._json("POST", f"/v1/analyses/{validate_job_id(job_id)}/approval", body=body)

    def create_evaluation(
        self,
        *,
        domain_pack_ids: list[str] | None,
        idempotency_key: str,
    ) -> Created:
        body: dict[str, Any] = {"suite": "deterministic_fixtures"}
        if domain_pack_ids:
            body["domain_pack_ids"] = domain_pack_ids
        return self._create("/v1/evaluations", body, idempotency_key)

    def get_evaluation(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/evaluations/{validate_job_id(job_id)}")

    def get_evaluation_result(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/evaluations/{validate_job_id(job_id)}/result")

    # -- event stream ---------------------------------------------------------------

    def read_events(
        self,
        job_id: str,
        *,
        after_seq: int,
        window_seconds: float,
    ) -> StreamBatch:
        """Read the job's SSE stream for at most about ``window_seconds``, then close."""
        path = f"/v1/analyses/{validate_job_id(job_id)}/events"
        headers = {"Accept": "text/event-stream"}
        if after_seq > 0:
            headers["Last-Event-ID"] = str(after_seq)
        timeout = httpx.Timeout(self._timeout, read=max(window_seconds, 0.1))
        batch = StreamBatch()
        deadline = time.monotonic() + window_seconds
        try:
            with self._http.stream("GET", path, headers=headers, timeout=timeout) as response:
                if response.status_code != 200:
                    response.read()
                    raise _error_from(response)
                parser = SseParser()
                for line in response.iter_lines():
                    message = parser.feed_line(line)
                    if message is not None and message.data:
                        event = _decode_event(message.event, message.id, message.data)
                        if event is not None:
                            batch.events.append(event)
                            if event["type"] in TERMINAL_EVENTS:
                                batch.ended = True
                                break
                            if event["type"] in PAUSE_EVENTS:
                                batch.paused = True
                                break
                    # Stop only between frames, so no frame is cut in half.
                    if line == "" and time.monotonic() >= deadline:
                        break
                else:
                    # The server closed the stream: it only does so after the end.
                    batch.ended = True
        except httpx.ReadTimeout:
            pass  # no frame within the window: this read is over
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise _unavailable(self.base_url) from exc
        except (httpx.RemoteProtocolError, httpx.ReadError):
            batch.interrupted = True
        return batch

    # -- internals ------------------------------------------------------------------

    def _create(self, path: str, body: dict[str, Any], idempotency_key: str) -> Created:
        response = self._send(
            "POST",
            path,
            body=body,
            headers={"Idempotency-Key": idempotency_key},
            ambiguous=True,
        )
        if response.status_code not in (200, 202):
            raise _error_from(response)
        return Created(
            job=_body(response),
            replayed=response.headers.get("Idempotent-Replayed") == "true",
        )

    def _json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._send(method, path, body=body, ambiguous=method != "GET")
        if response.status_code >= 400:
            raise _error_from(response)
        return _body(response)

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        ambiguous: bool,
    ) -> httpx.Response:
        try:
            return self._http.request(method, path, json=body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise _unavailable(self.base_url) from exc
        except httpx.TransportError as exc:
            # The request may have been received: a retry must reuse the same key.
            raise ApiUnavailableError(
                f"The API at {self.base_url} did not answer ({type(exc).__name__})",
                ambiguous=ambiguous,
            ) from exc


def _unavailable(base_url: str) -> ApiUnavailableError:
    return ApiUnavailableError(
        f"Cannot reach the SentinelFlow API at {base_url}. Is it running? "
        "Start it with: uv run python -m app.api.serve",
    )


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise ApiError(
            status=response.status_code,
            code="UNEXPECTED_RESPONSE",
            message="The API returned a response the harness does not understand",
        )
    return payload


def _error_from(response: httpx.Response) -> ApiError:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        details = error.get("details")
        return ApiError(
            status=response.status_code,
            code=error["code"],
            message=str(error.get("message") or ""),
            details=details if isinstance(details, dict) else {},
            request_id=_string_or_none(error.get("request_id")),
        )
    # Not the SentinelFlow envelope: report the status only, never the body.
    return ApiError(
        status=response.status_code,
        code=f"HTTP_{response.status_code}",
        message="The server answered with an error that is not a SentinelFlow error envelope",
    )


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _decode_event(name: str | None, event_id: str | None, data: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(data)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type") or name
    if not isinstance(event_type, str):
        return None
    seq: int | None = None
    if event_id is not None and event_id.isdigit():
        seq = int(event_id)
    after = payload.get("after_seq")
    return {
        "type": event_type,
        # Only durable events carry an SSE id; live events have ``after_seq``.
        "seq": seq,
        "after_seq": after if isinstance(after, int) else None,
        "at": payload.get("at"),
        "data": payload.get("data") if isinstance(payload.get("data"), dict) else {},
    }
