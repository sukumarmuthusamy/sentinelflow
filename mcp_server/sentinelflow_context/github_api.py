"""Read-only GitHub REST client for the live change source.

Only GET requests are issued, redirects are not followed, and every failure is
raised as a ``ToolError`` with a fixed message. Tokens, request headers and
response bodies are never echoed into errors.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.mcpserver.exceptions import ToolError

API_VERSION = "2022-11-28"
USER_AGENT = "sentinelflow-change-source"
FILES_PER_PAGE = 100
MAX_JSON_BYTES = 5_000_000
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")

# Tests swap in an httpx.MockTransport; production uses the default network transport.
_transport_override: httpx.BaseTransport | None = None


def set_transport_for_tests(transport: httpx.BaseTransport | None) -> None:
    """Route all GitHub requests through ``transport`` (None restores the network)."""
    global _transport_override
    _transport_override = transport


class GitHubNotFoundError(ToolError):
    """The repository, pull request or file does not exist or is not visible."""


class GitHubContentTooLargeError(ToolError):
    """A response exceeded the configured byte limit."""


def _reset_hint(response: httpx.Response) -> str:
    retry_after = response.headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        return f"retry after {int(retry_after)}s"
    reset = response.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        when = datetime.fromtimestamp(int(reset), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"limit resets at {when}"
    return "retry later"


def raise_for_github_status(response: httpx.Response, *, what: str) -> None:
    """Map a non-success GitHub response to a sanitized ToolError."""
    status = response.status_code
    if status < 300:
        return
    rate_limited = status == 429 or (
        status == 403
        and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
        )
    )
    if rate_limited:
        msg = (
            f"GitHub API rate limit reached while reading {what} ({_reset_hint(response)}). "
            "Set SENTINELFLOW_GITHUB_TOKEN for a higher limit."
        )
        raise ToolError(msg)
    if status == 401:
        raise ToolError("GitHub rejected the configured credentials (401)")
    if status == 403:
        msg = f"GitHub denied read access to {what} (403)"
        raise ToolError(msg)
    if status == 404:
        msg = f"{what} not found, or not visible with the configured GitHub access"
        raise GitHubNotFoundError(msg)
    if 300 <= status < 400:
        msg = f"GitHub API redirected the request for {what} ({status}); redirects are not followed"
        raise ToolError(msg)
    if status >= 500:
        msg = f"GitHub API unavailable while reading {what} ({status})"
        raise ToolError(msg)
    msg = f"GitHub API returned {status} for {what}"
    raise ToolError(msg)


class GitHubApi:
    """Minimal GET-only client for pull requests, their files and file contents."""

    def __init__(self, *, base_url: str, token: str | None, timeout_seconds: float) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=_transport_override,
        )
        self.requests_made = 0

    def __enter__(self) -> GitHubApi:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.close()

    def _get_bytes(
        self,
        path: str,
        *,
        what: str,
        params: dict[str, str | int] | None = None,
        accept: str | None = None,
        max_bytes: int,
    ) -> bytes:
        headers = {"Accept": accept} if accept else None
        self.requests_made += 1
        try:
            with self._client.stream("GET", path, params=params, headers=headers) as response:
                raise_for_github_status(response, what=what)
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    msg = f"{what} exceeds the {max_bytes}-byte limit"
                    raise GitHubContentTooLargeError(msg)
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        msg = f"{what} exceeds the {max_bytes}-byte limit"
                        raise GitHubContentTooLargeError(msg)
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.TimeoutException as exc:
            msg = f"GitHub API timed out while reading {what}"
            raise ToolError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"GitHub API unreachable while reading {what}"
            raise ToolError(msg) from exc

    def _get_json(
        self,
        path: str,
        *,
        what: str,
        params: dict[str, str | int] | None = None,
    ) -> Any:
        body = self._get_bytes(path, what=what, params=params, max_bytes=MAX_JSON_BYTES)
        try:
            return json.loads(body)
        except ValueError as exc:
            msg = f"GitHub API returned a malformed response for {what}"
            raise ToolError(msg) from exc

    def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        what = f"pull request #{number} in {repository}"
        payload = self._get_json(f"/repos/{repository}/pulls/{number}", what=what)
        if not isinstance(payload, dict):
            msg = f"GitHub API returned an unexpected shape for {what}"
            raise ToolError(msg)
        return payload

    def list_pull_request_files(
        self,
        repository: str,
        number: int,
        *,
        max_files: int,
    ) -> list[dict[str, Any]]:
        """Return up to ``max_files`` changed-file records, following pagination."""
        what = f"files of pull request #{number} in {repository}"
        files: list[dict[str, Any]] = []
        page = 1
        while len(files) < max_files:
            payload = self._get_json(
                f"/repos/{repository}/pulls/{number}/files",
                what=what,
                params={"per_page": FILES_PER_PAGE, "page": page},
            )
            if not isinstance(payload, list):
                msg = f"GitHub API returned an unexpected shape for {what}"
                raise ToolError(msg)
            files.extend(item for item in payload if isinstance(item, dict))
            if len(payload) < FILES_PER_PAGE:
                break
            page += 1
        return files[:max_files]

    def get_file_content(
        self,
        repository: str,
        path: str,
        *,
        ref: str,
        max_bytes: int,
    ) -> bytes:
        """Raw file bytes at ``ref``; raises GitHubNotFoundError if absent there."""
        what = f"file '{path}' at {ref[:12]}"
        return self._get_bytes(
            f"/repos/{repository}/contents/{quote(path, safe='/')}",
            what=what,
            params={"ref": ref},
            accept="application/vnd.github.raw",
            max_bytes=max_bytes,
        )
