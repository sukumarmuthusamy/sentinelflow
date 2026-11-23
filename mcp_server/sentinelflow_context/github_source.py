"""GitHub pull requests as change packages, behind the sentinelflow-context server.

A pull request is fetched read-only, normalized into the same ``ChangePackageResponse``
shape that fixtures produce, and analysed with the existing deterministic tools.

Normalization mirrors the fixture conventions so a fixture and its equivalent PR are
comparable: a code file originates at its owning service, a non-code file originates
at the file itself (and is isolated from graph traversal), and ``module_service_map``
records which service owns each changed file.

Everything that comes from GitHub is untrusted: paths are validated before use, the
title is sanitized and kept out of agent input, the body is never stored, and scanner
output is redacted. Whenever data or tooling needed for a deterministic signal is
missing, the gap is recorded on the package instead of being filled with a default.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

import yaml
from app.core.domain_pack import DomainPack, is_non_code_path
from app.deterministic.contract_diff import OasDiffOptions, parse_oasdiff_output
from app.deterministic.dependency_graph import (
    build_dependency_graph,
    graph_edges,
    resolve_change_blast_radius,
    resolve_impacted_services,
)
from app.deterministic.models import (
    ContractChange,
    ContractDiffResult,
    SecurityFindingRecord,
    SecurityScanResult,
)
from app.deterministic.subprocess_util import (
    command_available,
    docker_available,
    run_command,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mcp_server.sentinelflow_context.github_api import (
    REPOSITORY_PATTERN,
    GitHubApi,
    GitHubContentTooLargeError,
    GitHubNotFoundError,
)
from mcp_server.sentinelflow_context.models import (
    ChangePackageResponse,
    DependencyGraphResponse,
    GitHubPullRequestRef,
)
from mcp_server.sentinelflow_context.settings import McpServerSettings

LIVE_SOURCE_FILE = "live_source.yaml"
GITHUB_CHANGE_ID_PATTERN = re.compile(r"^PR-([1-9][0-9]{0,8})$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

MAX_FILES = 300
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_SCAN_BYTES = 8_000_000
MAX_PATH_LENGTH = 512
MAX_TITLE_LENGTH = 256
BLAST_RADIUS_MAX_DEPTH = 5
CACHE_TTL_SECONDS = 300.0
CACHE_MAX_ENTRIES = 16

GITLEAKS_IMAGE = "zricethezav/gitleaks:v8.30.1"

# Dependency manifests and lockfiles. A change to one needs a vulnerability scan,
# which GitHub mode does not run, so it is recorded as a gap.
DEPENDENCY_MANIFESTS = frozenset(
    {
        "requirements.txt",
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "pipfile.lock",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "go.mod",
        "go.sum",
        "cargo.lock",
    },
)

GITHUB_NOTES = (
    "Test inventory is not derived in GitHub mode: required_tests and available_tests "
    "are empty, so the test-gap term contributes 0.",
    "semgrep SAST is not run in GitHub mode; secret scanning (gitleaks) is.",
)

_CRITICALITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Legal in git but unsafe to materialize on every OS (drive letters, NTFS streams).
_UNPORTABLE_CHARS = re.compile(r'[\\:*?"<>|]')


# --------------------------------------------------------------------------- config


class ServiceRule(BaseModel):
    """Which changed paths belong to one service of the pack."""

    model_config = ConfigDict(extra="forbid")

    service: str
    paths: list[str] = Field(default_factory=list)
    contracts: list[str] = Field(default_factory=list)


class GitHubLiveSource(BaseModel):
    """The ``github`` section of a pack's ``live_source.yaml``."""

    model_config = ConfigDict(extra="forbid")

    repository: str
    services: list[ServiceRule] = Field(min_length=1)

    @field_validator("repository")
    @classmethod
    def _valid_repository(cls, value: str) -> str:
        if not REPOSITORY_PATTERN.match(value):
            msg = "repository must be 'owner/name'"
            raise ValueError(msg)
        return value

    def service_for(self, path: str) -> str | None:
        """Owning service of ``path`` (first matching rule wins), or None."""
        for rule in self.services:
            for pattern in (*rule.paths, *rule.contracts):
                if path == pattern or (pattern.endswith("/") and path.startswith(pattern)):
                    return rule.service
        return None

    def is_contract(self, path: str) -> bool:
        return any(path in rule.contracts for rule in self.services)


def load_live_source(pack: DomainPack) -> GitHubLiveSource:
    """Load and validate the pack's GitHub live-source configuration."""
    path = pack.root / LIVE_SOURCE_FILE
    if not path.is_file():
        msg = f"domain pack '{pack.pack_id}' has no GitHub live source configured"
        raise ToolError(msg)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        section = raw.get("github") if isinstance(raw, dict) else None
        config = GitHubLiveSource.model_validate(section)
    except (yaml.YAMLError, ValidationError) as exc:
        msg = f"GitHub live source for domain pack '{pack.pack_id}' is invalid"
        raise ToolError(msg) from exc
    known = {service.name for service in pack.domain.services}
    unknown = sorted({rule.service for rule in config.services} - known)
    if unknown:
        msg = f"GitHub live source maps paths to unknown services: {', '.join(unknown)}"
        raise ToolError(msg)
    return config


# ------------------------------------------------------------------- input hygiene


def parse_github_change_id(change_id: str) -> int:
    """``PR-<number>`` -> number. The repository always comes from pack config."""
    match = GITHUB_CHANGE_ID_PATTERN.match(change_id)
    if match is None:
        msg = "GitHub change_id must look like 'PR-<number>'"
        raise ToolError(msg)
    return int(match.group(1))


def safe_repo_path(raw: object) -> str | None:
    """Return ``raw`` if it is a safe relative POSIX path, else None."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_PATH_LENGTH:
        return None
    if _CONTROL_CHARS.search(raw) or _UNPORTABLE_CHARS.search(raw) or raw.startswith("/"):
        return None
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return raw


def sanitize_text(raw: object, *, limit: int) -> str:
    """Single-line, control-character-free, truncated text."""
    text = _CONTROL_CHARS.sub(" ", str(raw or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_dependency_manifest(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name in DEPENDENCY_MANIFESTS or (
        name.startswith("requirements") and name.endswith(".txt")
    )


# ------------------------------------------------------------------ signal tools


class SignalTools(Protocol):
    """External deterministic tools. ``None`` means unavailable or failed."""

    def diff_openapi(self, base_spec: Path, revision_spec: Path) -> ContractDiffResult | None:
        """oasdiff verdict for two specs in the same directory."""
        ...

    def scan_secrets(self, root: Path) -> SecurityScanResult | None:
        """gitleaks findings for the files under ``root``."""
        ...


def _docker_image_present(image: str) -> bool:
    if not docker_available():
        return False
    return run_command(["docker", "image", "inspect", image], timeout_seconds=30).ok


def parse_gitleaks_report(payload: object, *, root: Path) -> SecurityScanResult:
    """Normalize a gitleaks JSON report. Matched secret text is never copied."""
    findings: list[SecurityFindingRecord] = []
    items = payload if isinstance(payload, list) else []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        rule_id = sanitize_text(item.get("RuleID") or "secret", limit=128)
        raw_file = str(item.get("File") or "").replace("\\", "/")
        root_posix = root.as_posix().rstrip("/") + "/"
        for prefix in (root_posix, "/scan/"):
            if raw_file.startswith(prefix):
                raw_file = raw_file[len(prefix) :]
        line = item.get("StartLine")
        detail = sanitize_text(item.get("Description") or rule_id, limit=200)
        if isinstance(line, int):
            detail = f"{detail} (line {line})"
        findings.append(
            SecurityFindingRecord(
                id=f"gitleaks-{index}",
                scanner="gitleaks",
                severity="critical",
                title=rule_id,
                detail=detail,
                file_path=safe_repo_path(raw_file),
                rule_id=rule_id,
            ),
        )
    return SecurityScanResult(
        findings=findings,
        max_severity="critical" if findings else "none",
        verified_secret_exposure=bool(findings),
    )


class LocalSignalTools:
    """oasdiff and gitleaks from PATH or locally present Docker images.

    Images are never pulled implicitly (``--pull=never``), containers run without
    network, and any non-success exit is reported as unavailable, not as "clean".
    """

    def __init__(self, *, timeout_seconds: int = 120) -> None:
        self._timeout = timeout_seconds

    def diff_openapi(self, base_spec: Path, revision_spec: Path) -> ContractDiffResult | None:
        image = OasDiffOptions().docker_image
        if not _docker_image_present(image):
            return None
        mount = base_spec.parent.resolve()
        argv = [
            "docker", "run", "--rm", "--network=none", "--pull=never",
            "-v", f"{mount}:/specs:ro", "-w", "/specs", image,
            "breaking", base_spec.name, revision_spec.name, "--format", "json",
        ]  # fmt: skip
        result = run_command(argv, timeout_seconds=self._timeout)
        if not result.ok:
            return None
        return parse_oasdiff_output(result.stdout)

    def scan_secrets(self, root: Path) -> SecurityScanResult | None:
        with tempfile.TemporaryDirectory(prefix="sf-gitleaks-") as report_dir:
            report = Path(report_dir) / "report.json"
            common = ["--report-format", "json", "--exit-code", "2", "--no-banner", "--redact"]
            if command_available("gitleaks"):
                argv = ["gitleaks", "dir", str(root), "--report-path", str(report), *common]
            elif _docker_image_present(GITLEAKS_IMAGE):
                argv = [
                    "docker", "run", "--rm", "--network=none", "--pull=never",
                    "-v", f"{root.resolve()}:/scan:ro",
                    "-v", f"{Path(report_dir).resolve()}:/report",
                    GITLEAKS_IMAGE, "dir", "/scan", "--report-path", "/report/report.json", *common,
                ]  # fmt: skip
            else:
                return None
            result = run_command(argv, timeout_seconds=self._timeout)
            # --exit-code 2 separates "leaks found" (2) from tool errors (1).
            if result.returncode not in {0, 2} or not report.is_file():
                return None
            try:
                payload = json.loads(report.read_text(encoding="utf-8") or "[]")
            except ValueError:
                return None
            return parse_gitleaks_report(payload, root=root)


_tools_override: SignalTools | None = None


def set_signal_tools_for_tests(tools: SignalTools | None) -> None:
    """Replace oasdiff/gitleaks with test doubles (None restores the local tools)."""
    global _tools_override
    _tools_override = tools


def _signal_tools() -> SignalTools:
    return _tools_override or LocalSignalTools()


# ------------------------------------------------------------------ resolution


class ResolvedGitHubChange(BaseModel):
    """A normalized pull request plus the deterministic signals computed for it."""

    package: ChangePackageResponse
    module_service_map: dict[str, str]
    contract_diff: ContractDiffResult
    security: SecurityScanResult


_cache: dict[tuple[str, str, int], tuple[float, ResolvedGitHubChange]] = {}
_cache_lock = threading.Lock()


def clear_github_cache() -> None:
    with _cache_lock:
        _cache.clear()


@contextmanager
def _workspace() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="sf-github-") as tmp:
        yield Path(tmp)


def _write_inside(root: Path, relative: str, data: bytes) -> None:
    target = (root / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        msg = "refusing to write outside the scan workspace"
        raise ToolError(msg)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _primary_service(owned: list[str], live: GitHubLiveSource, pack: DomainPack) -> str:
    counts = Counter(owned)
    order = {rule.service: index for index, rule in enumerate(live.services)}
    tiers = {service.name: service.criticality for service in pack.domain.services}
    return min(
        counts,
        key=lambda name: (
            -counts[name],
            _CRITICALITY_RANK.get(tiers.get(name, "low"), 3),
            order.get(name, len(order)),
        ),
    )


def _merge_contract_results(results: list[ContractDiffResult]) -> ContractDiffResult:
    changes: list[ContractChange] = [change for result in results for change in result.changes]
    err = sum(result.err_count for result in results)
    return ContractDiffResult(
        tool="oasdiff",
        err_count=err,
        warn_count=sum(result.warn_count for result in results),
        info_count=sum(result.info_count for result in results),
        changes=changes,
        breaking=err > 0,
    )


def _sha(value: object, *, what: str) -> str:
    if not isinstance(value, str) or not SHA_PATTERN.match(value):
        msg = f"GitHub API returned an invalid {what}"
        raise ToolError(msg)
    return value


def _resolve(
    api: GitHubApi,
    *,
    live: GitHubLiveSource,
    pack: DomainPack,
    change_id: str,
    number: int,
    tools: SignalTools,
) -> ResolvedGitHubChange:
    repository = live.repository
    pr = api.get_pull_request(repository, number)
    base_obj, head_obj = pr.get("base"), pr.get("head")
    base: dict[str, Any] = base_obj if isinstance(base_obj, dict) else {}
    head: dict[str, Any] = head_obj if isinstance(head_obj, dict) else {}
    base_sha = _sha(base.get("sha"), what="base commit")
    head_sha = _sha(head.get("sha"), what="head commit")
    head_repo_obj = head.get("repo")
    head_name = head_repo_obj.get("full_name") if isinstance(head_repo_obj, dict) else None
    head_repo: str | None = (
        head_name if isinstance(head_name, str) and REPOSITORY_PATTERN.match(head_name) else None
    )
    declared_files = pr.get("changed_files")
    declared = declared_files if isinstance(declared_files, int) and declared_files >= 0 else None

    gaps: list[str] = []
    notes: list[str] = list(GITHUB_NOTES)

    raw_files = api.list_pull_request_files(repository, number, max_files=MAX_FILES)
    if declared is None:
        gaps.append("GitHub did not report the pull request's changed-file count")
    elif declared > len(raw_files):
        gaps.append(
            f"GitHub listed {len(raw_files)} of {declared} changed files "
            f"(analysis limit {MAX_FILES}); the remainder was not analysed",
        )

    files: list[dict[str, Any]] = []
    rejected_paths = 0
    for item in raw_files:
        path = safe_repo_path(item.get("filename"))
        if path is None:
            rejected_paths += 1
            continue
        previous = item.get("previous_filename")
        files.append(
            {
                "path": path,
                "status": str(item.get("status") or "modified"),
                "previous": safe_repo_path(previous) if previous is not None else None,
                "has_patch": isinstance(item.get("patch"), str),
                "changes": item.get("changes") if isinstance(item.get("changes"), int) else 0,
            },
        )
    if rejected_paths:
        gaps.append(
            f"{rejected_paths} changed file(s) had an unsafe or invalid path and were skipped",
        )

    rules = pack.domain.non_code_paths
    owners: dict[str, str] = {}
    unmapped: list[str] = []
    for entry in files:
        service = live.service_for(entry["path"])
        if service is None:
            unmapped.append(entry["path"])
            if not is_non_code_path(entry["path"], rules):
                gaps.append(
                    f"changed file '{entry['path']}' is not attributed to any service in "
                    f"pack '{pack.pack_id}', so its blast radius is unknown",
                )
        else:
            owners[entry["path"]] = service
        if _is_dependency_manifest(entry["path"]) and entry["status"] != "removed":
            gaps.append(
                f"dependency manifest '{entry['path']}' changed; GitHub mode does not run a "
                "dependency vulnerability scan",
            )
    if not owners:
        msg = (
            f"pull request #{number} changes no file owned by a service in domain pack "
            f"'{pack.pack_id}'"
        )
        raise ToolError(msg)

    without_patch = sorted(e["path"] for e in files if not e["has_patch"] and e["changes"] > 0)
    if without_patch:
        notes.append(
            f"GitHub omitted the diff for {len(without_patch)} file(s); full file contents "
            "were analysed instead",
        )

    primary = _primary_service(list(owners.values()), live, pack)
    origin_nodes: list[str] = []
    for path in sorted(owners):
        node = path if is_non_code_path(path, rules) else owners[path]
        if node not in origin_nodes:
            origin_nodes.append(node)

    # Contract compatibility: base vs head of each configured contract file that changed.
    contract_results: list[ContractDiffResult] = []
    contract_touched = False
    for entry in files:
        path = entry["path"]
        if path not in owners or not live.is_contract(path):
            continue
        contract_touched = True
        if entry["status"] == "added":
            notes.append(f"contract '{path}' is new; there is no base version to diff")
            continue
        if entry["status"] == "removed":
            gaps.append(f"contract '{path}' was removed; compatibility was not computed")
            continue
        if head_repo is None:
            gaps.append(f"head repository unavailable; contract '{path}' was not diffed")
            continue
        try:
            base_bytes = api.get_file_content(
                repository, entry["previous"] or path, ref=base_sha, max_bytes=MAX_FILE_BYTES
            )
            head_bytes = api.get_file_content(
                head_repo, path, ref=head_sha, max_bytes=MAX_FILE_BYTES
            )
        except (GitHubNotFoundError, GitHubContentTooLargeError) as exc:
            gaps.append(f"contract '{path}' could not be read ({exc}); not diffed")
            continue
        with _workspace() as tmp:
            (tmp / "base.yaml").write_bytes(base_bytes)
            (tmp / "revision.yaml").write_bytes(head_bytes)
            result = tools.diff_openapi(tmp / "base.yaml", tmp / "revision.yaml")
        if result is None:
            gaps.append(f"oasdiff unavailable; compatibility of contract '{path}' not computed")
            continue
        contract_results.append(result)
    if contract_results:
        contract_diff = _merge_contract_results(contract_results)
    elif contract_touched:
        contract_diff = ContractDiffResult(tool="unavailable")
    else:
        contract_diff = ContractDiffResult(tool="not_applicable")

    # Secret scan over the head version of every added or modified file.
    to_scan = [entry for entry in files if entry["status"] != "removed"]
    security = SecurityScanResult()
    if to_scan and head_repo is None:
        gaps.append("head repository unavailable; changed files were not scanned for secrets")
    elif to_scan and head_repo is not None:
        with _workspace() as tmp:
            total = 0
            written = 0
            for entry in to_scan:
                try:
                    data = api.get_file_content(
                        head_repo, entry["path"], ref=head_sha, max_bytes=MAX_FILE_BYTES
                    )
                except (GitHubNotFoundError, GitHubContentTooLargeError) as exc:
                    gaps.append(f"'{entry['path']}' could not be read for scanning ({exc})")
                    continue
                total += len(data)
                if total > MAX_TOTAL_SCAN_BYTES:
                    gaps.append(
                        f"changed files exceed the {MAX_TOTAL_SCAN_BYTES}-byte scan limit; "
                        "the remainder was not scanned",
                    )
                    break
                try:
                    _write_inside(tmp, entry["path"], data)
                except OSError:
                    gaps.append(f"'{entry['path']}' could not be staged for scanning")
                    continue
                written += 1
            if written:
                scanned = tools.scan_secrets(tmp)
                if scanned is None:
                    gaps.append("gitleaks unavailable; changed files were not scanned for secrets")
                else:
                    security = scanned
    else:
        notes.append("no added or modified files to scan for secrets")

    pr_state = sanitize_text(pr.get("state") or "unknown", limit=16)
    package = ChangePackageResponse(
        change_id=change_id,
        source="github",
        domain_pack_id=pack.pack_id,
        description="",
        category="unknown",
        primary_service=primary,
        changed_files=sorted(entry["path"] for entry in files),
        origin_nodes=origin_nodes,
        github=GitHubPullRequestRef(
            repository=repository,
            number=number,
            title=sanitize_text(pr.get("title"), limit=MAX_TITLE_LENGTH),
            state=pr_state,
            draft=pr.get("draft") is True,
            base_sha=base_sha,
            head_sha=head_sha,
            html_url=f"https://github.com/{repository}/pull/{number}",
            changed_file_count=declared if declared is not None else len(raw_files),
            files_without_patch=without_patch,
            unmapped_files=sorted(unmapped),
        ),
        gaps=gaps,
        notes=notes,
    )
    return ResolvedGitHubChange(
        package=package,
        module_service_map=dict(sorted(owners.items())),
        contract_diff=contract_diff,
        security=security,
    )


def resolve_github_change(
    settings: McpServerSettings,
    *,
    pack: DomainPack,
    change_id: str,
) -> ResolvedGitHubChange:
    """Fetch and normalize a pull request, reusing a recent result for the same PR.

    The short cache keeps one analysis on a single snapshot (all tools see the same
    head commit) and avoids re-reading GitHub for every agent's tool call.
    """
    number = parse_github_change_id(change_id)
    live = load_live_source(pack)
    key = (pack.pack_id, live.repository, number)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
    with GitHubApi(
        base_url=settings.github_api_url,
        token=settings.github_token,
        timeout_seconds=settings.github_timeout_seconds,
    ) as api:
        resolved = _resolve(
            api,
            live=live,
            pack=pack,
            change_id=change_id,
            number=number,
            tools=_signal_tools(),
        )
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
        _cache[key] = (now, resolved)
    return resolved


def github_dependency_graph(
    resolved: ResolvedGitHubChange,
    pack: DomainPack,
) -> DependencyGraphResponse:
    """Blast radius from the pack graph; no remote checkout, so no import edges."""
    package = resolved.package
    graph = build_dependency_graph(
        domain_pack=pack,
        repo_root=None,
        module_service_map=resolved.module_service_map,
    )
    blast_radius = resolve_change_blast_radius(
        graph,
        package.origin_nodes,
        max_depth=BLAST_RADIUS_MAX_DEPTH,
        domain_pack=pack,
    )
    impacted = resolve_impacted_services(
        graph,
        blast_radius,
        domain_pack=pack,
        module_service_map=resolved.module_service_map,
        primary_service=package.primary_service,
        origin_nodes=package.origin_nodes,
    )
    return DependencyGraphResponse(
        change_id=package.change_id,
        domain_pack_id=pack.pack_id,
        edges=graph_edges(graph),
        blast_radius=blast_radius,
        impacted_services=impacted,
    )
