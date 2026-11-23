"""Offline GitHub API double built from recorded responses of the seed repository."""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from app.deterministic.contract_diff import parse_oasdiff_output
from app.deterministic.models import ContractDiffResult, SecurityScanResult
from mcp_server.sentinelflow_context import github_api, github_source

RECORDED = (
    Path(__file__).resolve().parents[1] / "fixtures" / "github" / "sentinelflow-demo-platform"
)
REPO = "sukumarmuthusamy/sentinelflow-demo-platform"

_PULL = re.compile(r"^/repos/([^/]+/[^/]+)/pulls/(\d+)$")
_FILES = re.compile(r"^/repos/([^/]+/[^/]+)/pulls/(\d+)/files$")
_CONTENTS = re.compile(r"^/repos/([^/]+/[^/]+)/contents/(.+)$")

# A representative `oasdiff breaking --format json` row: oasdiff encodes ERR as 3.
# Scripted, not produced by a real oasdiff run.
SCRIPTED_OASDIFF_ERR = json.dumps(
    [
        {
            "id": "request-property-removed",
            "text": "removed the required request property 'member_id'",
            "level": 3,
            "operation": "POST",
            "path": "/v1/core-api/claims",
        },
    ],
)

Responder = Callable[[httpx.Request], httpx.Response]


class FakeGitHub:
    """Routes GitHub REST paths to recorded data, with per-path overrides."""

    def __init__(self) -> None:
        self.pulls: dict[int, dict[str, Any]] = {}
        self.files: dict[int, list[dict[str, Any]]] = {}
        self.contents: dict[str, bytes] = {}
        self.overrides: dict[str, Responder] = {}
        self.requests: list[httpx.Request] = []

    @classmethod
    def recorded(cls) -> FakeGitHub:
        fake = cls()
        for number in (1, 7):
            fake.pulls[number] = json.loads(
                (RECORDED / f"pull_{number}.json").read_text(encoding="utf-8")
            )
            fake.files[number] = json.loads(
                (RECORDED / f"pull_{number}_files.json").read_text(encoding="utf-8")
            )
        contents = json.loads((RECORDED / "contents.json").read_text(encoding="utf-8"))
        fake.contents = {key: value.encode("utf-8") for key, value in contents.items()}
        return fake

    def pull(self, number: int) -> dict[str, Any]:
        return self.pulls[number]

    def clone_pull(self, source: int, number: int) -> None:
        """Copy a recorded PR to a new number so a test can modify it freely."""
        self.pulls[number] = copy.deepcopy(self.pulls[source]) | {"number": number}
        self.files[number] = copy.deepcopy(self.files[source])

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.overrides:
            return self.overrides[path](request)
        if match := _FILES.match(path):
            number = int(match.group(2))
            if match.group(1) != REPO or number not in self.files:
                return httpx.Response(404, json={"message": "Not Found"})
            per_page = int(request.url.params.get("per_page", "30"))
            page = int(request.url.params.get("page", "1"))
            rows = self.files[number][(page - 1) * per_page : page * per_page]
            return httpx.Response(200, json=rows)
        if match := _PULL.match(path):
            number = int(match.group(2))
            if match.group(1) != REPO or number not in self.pulls:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=self.pulls[number])
        if match := _CONTENTS.match(path):
            key = f"{request.url.params.get('ref')}:{match.group(2)}"
            if match.group(1) != REPO or key not in self.contents:
                return httpx.Response(404, json={"message": "Not Found"})
            body = self.contents[key]
            return httpx.Response(200, content=body, headers={"content-length": str(len(body))})
        return httpx.Response(404, json={"message": "Not Found"})


class FakeSignalTools:
    """Scripted oasdiff / gitleaks. ``None`` simulates an unavailable tool."""

    def __init__(
        self,
        *,
        contract: ContractDiffResult | None = None,
        secrets: SecurityScanResult | None = None,
    ) -> None:
        self.contract = contract if contract is not None else parse_oasdiff_output("[]")
        self.secrets = secrets if secrets is not None else SecurityScanResult()
        self.contract_unavailable = False
        self.scanner_unavailable = False
        self.diffed: list[tuple[bytes, bytes]] = []
        self.scanned_files: list[str] = []
        self.scanned_roots: list[Path] = []

    def diff_openapi(self, base_spec: Path, revision_spec: Path) -> ContractDiffResult | None:
        self.diffed.append((base_spec.read_bytes(), revision_spec.read_bytes()))
        return None if self.contract_unavailable else self.contract

    def scan_secrets(self, root: Path) -> SecurityScanResult | None:
        self.scanned_roots.append(root)
        self.scanned_files.extend(
            sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
        )
        return None if self.scanner_unavailable else self.secrets


@contextmanager
def github_offline(
    fake: FakeGitHub | None = None,
    tools: FakeSignalTools | None = None,
) -> Iterator[tuple[FakeGitHub, FakeSignalTools]]:
    """Point the GitHub source at ``fake`` and ``tools``; restore everything after."""
    fake = fake or FakeGitHub.recorded()
    tools = tools or FakeSignalTools()
    github_source.clear_github_cache()
    github_api.set_transport_for_tests(httpx.MockTransport(fake.handle))
    github_source.set_signal_tools_for_tests(tools)
    try:
        yield fake, tools
    finally:
        github_api.set_transport_for_tests(None)
        github_source.set_signal_tools_for_tests(None)
        github_source.clear_github_cache()
