"""AG M2 hardening: renamed files, fork boundaries and scanner failures (offline)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from app.core.domain_pack import load_domain_pack
from app.deterministic.policy_gate import apply_policy_gate
from app.deterministic.risk_engine import compute_risk_score
from mcp.server.mcpserver.exceptions import ToolError
from mcp_server.sentinelflow_context import github_api
from mcp_server.sentinelflow_context.github_api import GitHubApi
from mcp_server.sentinelflow_context.github_source import (
    ResolvedGitHubChange,
    resolve_github_change,
)
from mcp_server.sentinelflow_context.settings import (
    clear_mcp_server_settings_cache,
    get_mcp_server_settings,
)

from tests.contract.github_fakes import REPO, FakeGitHub, FakeSignalTools, github_offline

HC = "healthcare-claims"
PR1_HEAD = "553443d050718b0bc334eace4a32c1bf88708a32"
PR7_BASE = "e161b07e02c445f322a6124216e30229625c2919"
PR7_HEAD = "79454b672393b01cd1ee0799b8e3ac72a1ca01b8"
CONTRACT = "contracts/svc-core-api.yaml"
FORK = "untrusted-fork/sentinelflow-demo-platform"


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("SENTINELFLOW_GITHUB_TOKEN", raising=False)
    clear_mcp_server_settings_cache()
    yield
    clear_mcp_server_settings_cache()


def _resolve(
    fake: FakeGitHub, number: int, tools: FakeSignalTools | None = None
) -> ResolvedGitHubChange:
    with github_offline(fake, tools):
        return resolve_github_change(
            get_mcp_server_settings(), pack=load_domain_pack(HC), change_id=f"PR-{number}"
        )


def _decision(resolved: ResolvedGitHubChange) -> str:
    """Deterministic gate on the PR's signals, with the REVIEW floor gaps impose."""
    from app.deterministic.models import DeterministicSignals
    from app.deterministic.policy_gate import strictest_decision
    from app.graph.state import ReleaseDecision
    from mcp_server.sentinelflow_context.github_source import github_dependency_graph

    pack = load_domain_pack(HC)
    graph = github_dependency_graph(resolved, pack)
    signals = DeterministicSignals(
        change_id=resolved.package.change_id,
        primary_service=resolved.package.primary_service,
        blast_radius=graph.blast_radius,
        impacted_services=graph.impacted_services,
        contract_diff=resolved.contract_diff,
        security=resolved.security,
    )
    gate = apply_policy_gate(signals, pack, compute_risk_score(signals, pack))
    decision = gate.decision
    if resolved.package.gaps:
        decision = strictest_decision(decision, ReleaseDecision.REVIEW)
    return decision.value


def _content_paths(fake: FakeGitHub) -> list[str]:
    return [request.url.path for request in fake.requests if "/contents/" in request.url.path]


def _rename(fake: FakeGitHub, number: int, *, previous: Any, filename: str, **extra: Any) -> None:
    fake.clone_pull(7, number)
    row: dict[str, Any] = {"filename": filename, "status": "renamed", "changes": 1}
    if previous is not None:
        row["previous_filename"] = previous
    fake.files[number] = [row | extra]
    fake.pulls[number]["changed_files"] = 1


# ------------------------------------------------------------------ M1 renamed files


class TestRenamedFiles:
    def test_contract_renamed_away_from_its_declared_path_cannot_pass(self) -> None:
        # AG probe A: this used to resolve to svc-analytics, not_applicable, no gaps -> PASS.
        fake = FakeGitHub.recorded()
        _rename(fake, 80, previous=CONTRACT, filename="analytics_docs/old-contract.yaml")
        fake.contents[f"{PR7_HEAD}:analytics_docs/old-contract.yaml"] = b"openapi: 3.1.0\n"
        resolved = _resolve(fake, 80)
        package = resolved.package
        assert package.primary_service == "svc-core-api"
        assert "svc-core-api" in package.origin_nodes
        assert resolved.module_service_map[CONTRACT] == "svc-core-api"
        assert CONTRACT in package.changed_files
        assert resolved.contract_diff.tool == "unavailable"
        assert any(f"contract '{CONTRACT}' was renamed" in gap for gap in package.gaps)
        assert _decision(resolved) != "PASS"

    @pytest.mark.parametrize(
        "previous",
        ["../contracts/svc-core-api.yaml", "/etc/passwd", "contracts\\svc-core-api.yaml",
         "C:/contracts/x.yaml", "contracts/./x.yaml", "contracts/x\x00.yaml", "", 42],
    )  # fmt: skip
    def test_unsafe_previous_path_is_a_gap_and_never_read(self, previous: Any) -> None:
        fake = FakeGitHub.recorded()
        _rename(fake, 81, previous=previous, filename=CONTRACT, patch="@@")
        tools = FakeSignalTools()
        resolved = _resolve(fake, 81, tools)
        gaps = resolved.package.gaps
        assert any("unsafe or invalid previous path" in gap for gap in gaps)
        assert any("no usable base path" in gap for gap in gaps)
        assert tools.diffed == []  # never diffed against a guessed base
        assert all(".." not in path and "passwd" not in path for path in _content_paths(fake))
        # Only the head version (for the secret scan) was read.
        assert all(request.url.params.get("ref") == PR7_HEAD for request in fake.requests
                   if "/contents/" in request.url.path)  # fmt: skip
        assert _decision(resolved) != "PASS"

    def test_rename_without_previous_path_is_a_gap(self) -> None:
        fake = FakeGitHub.recorded()
        _rename(fake, 82, previous=None, filename=CONTRACT)
        resolved = _resolve(fake, 82)
        assert any("previous path" in gap for gap in resolved.package.gaps)
        assert resolved.contract_diff.tool == "unavailable"

    def test_cross_service_rename_keeps_the_original_service(self) -> None:
        fake = FakeGitHub.recorded()
        _rename(
            fake,
            83,
            previous="services/svc_core_api/handlers.py",
            filename="services/svc_notification/handlers.py",
        )
        fake.contents[f"{PR7_HEAD}:services/svc_notification/handlers.py"] = b"x = 1\n"
        resolved = _resolve(fake, 83)
        package = resolved.package
        assert resolved.module_service_map == {
            "services/svc_core_api/handlers.py": "svc-core-api",
            "services/svc_notification/handlers.py": "svc-notification",
        }
        assert set(package.origin_nodes) == {"svc-core-api", "svc-notification"}
        assert package.primary_service == "svc-core-api"  # critical beats high on a tie
        assert package.changed_files == [
            "services/svc_core_api/handlers.py",
            "services/svc_notification/handlers.py",
        ]
        assert package.gaps == []

    def test_file_renamed_into_a_contract_path_is_diffed_against_its_original(self) -> None:
        fake = FakeGitHub.recorded()
        _rename(fake, 84, previous="contracts/draft/core.yaml", filename=CONTRACT)
        fake.contents[f"{PR7_BASE}:contracts/draft/core.yaml"] = b"openapi: draft\n"
        tools = FakeSignalTools()
        resolved = _resolve(fake, 84, tools)
        head = FakeGitHub.recorded().contents[f"{PR7_HEAD}:{CONTRACT}"]
        assert tools.diffed == [(b"openapi: draft\n", head)]
        assert resolved.contract_diff.tool == "oasdiff"
        # The original location belongs to no service, so its blast radius is unknown.
        assert any("'contracts/draft/core.yaml' is not attributed" in gap
                   for gap in resolved.package.gaps)  # fmt: skip

    def test_contract_renamed_onto_another_declared_contract(self) -> None:
        fake = FakeGitHub.recorded()
        _rename(fake, 85, previous=CONTRACT, filename="contracts/svc-notification.yaml")
        fake.contents[f"{PR7_HEAD}:contracts/svc-notification.yaml"] = b"openapi: moved\n"
        tools = FakeSignalTools()
        resolved = _resolve(fake, 85, tools)
        base = FakeGitHub.recorded().contents[f"{PR7_BASE}:{CONTRACT}"]
        assert tools.diffed == [(base, b"openapi: moved\n")]
        assert any(f"contract '{CONTRACT}' was renamed" in gap for gap in resolved.package.gaps)
        assert _decision(resolved) != "PASS"

    def test_renamed_away_dependency_manifest_is_a_gap(self) -> None:
        fake = FakeGitHub.recorded()
        fake.clone_pull(1, 86)
        fake.files[86].append(
            {"filename": "old/requirements.txt", "previous_filename": "requirements.txt",
             "status": "renamed", "changes": 0},
        )  # fmt: skip
        fake.contents[f"{PR1_HEAD}:old/requirements.txt"] = b"httpx\n"
        resolved = _resolve(fake, 86)
        assert any("'requirements.txt' changed" in gap for gap in resolved.package.gaps)

    def test_copied_file_does_not_claim_its_untouched_original(self) -> None:
        fake = FakeGitHub.recorded()
        fake.clone_pull(1, 87)
        fake.files[87].append(
            {"filename": "analytics_docs/copy.py", "status": "copied", "changes": 0,
             "previous_filename": "services/svc_core_api/a.py"},
        )  # fmt: skip
        fake.contents[f"{PR1_HEAD}:analytics_docs/copy.py"] = b"x = 1\n"
        resolved = _resolve(fake, 87)
        assert "services/svc_core_api/a.py" not in resolved.package.changed_files
        assert "svc-core-api" not in resolved.package.origin_nodes

    def test_ordinary_changes_are_unchanged(self) -> None:
        fake = FakeGitHub.recorded()
        tools = FakeSignalTools()
        pr7 = _resolve(fake, 7, tools)
        assert pr7.package.gaps == []
        assert pr7.package.changed_files == [CONTRACT]
        assert pr7.package.origin_nodes == ["svc-core-api"]
        assert len(tools.diffed) == 1
        pr1 = _resolve(FakeGitHub.recorded(), 1)
        assert pr1.package.gaps == []
        assert pr1.contract_diff.tool == "not_applicable"


# ------------------------------------------------------------------ M4 fork boundary


class TestForkBoundary:
    def _all_requests_stay_in_repo(self, fake: FakeGitHub) -> None:
        assert fake.requests
        for request in fake.requests:
            assert request.url.path.startswith(f"/repos/{REPO}/"), request.url.path

    def test_same_repository_pr(self) -> None:
        fake = FakeGitHub.recorded()
        resolved = _resolve(fake, 7)
        self._all_requests_stay_in_repo(fake)
        assert not any("fork" in note for note in resolved.package.notes)
        assert resolved.package.gaps == []

    def test_external_fork_is_read_only_through_the_configured_repository(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "sentinelflow-test-token-" + "f" * 24
        monkeypatch.setenv("SENTINELFLOW_GITHUB_TOKEN", token)
        clear_mcp_server_settings_cache()
        fake = FakeGitHub.recorded()
        fake.clone_pull(7, 90)
        fake.pulls[90]["head"]["repo"]["full_name"] = FORK
        tools = FakeSignalTools()
        resolved = _resolve(fake, 90, tools)
        self._all_requests_stay_in_repo(fake)
        assert all(r.headers.get("authorization") == f"Bearer {token}" for r in fake.requests)
        assert not any(FORK in str(request.url) for request in fake.requests)
        assert any("comes from a fork" in note for note in resolved.package.notes)
        assert resolved.package.gaps == []
        assert len(tools.diffed) == 1  # same verdict path as a same-repo PR

    def test_inaccessible_fork_head_is_a_gap_not_an_empty_diff(self) -> None:
        fake = FakeGitHub.recorded()
        fake.clone_pull(7, 91)
        fake.pulls[91]["head"]["repo"]["full_name"] = FORK
        del fake.contents[f"{PR7_HEAD}:{CONTRACT}"]
        tools = FakeSignalTools()
        resolved = _resolve(fake, 91, tools)
        self._all_requests_stay_in_repo(fake)
        gaps = resolved.package.gaps
        assert any("head version could not be read" in gap and "fork" in gap for gap in gaps)
        assert any("could not be read for scanning" in gap for gap in gaps)
        assert tools.diffed == [] and tools.scanned_files == []
        assert resolved.contract_diff.tool == "unavailable"
        assert _decision(resolved) != "PASS"

    @pytest.mark.parametrize(
        "head_repo",
        [None, "untrusted-fork/x", {"full_name": "../../evil"}, {"full_name": 7}, {}],
    )
    def test_malformed_head_repository_metadata(self, head_repo: Any) -> None:
        fake = FakeGitHub.recorded()
        fake.clone_pull(7, 92)
        fake.pulls[92]["head"]["repo"] = head_repo
        resolved = _resolve(fake, 92)
        self._all_requests_stay_in_repo(fake)
        assert any("head repository unavailable or malformed" in g for g in resolved.package.gaps)
        assert _decision(resolved) != "PASS"

    def test_base_repository_mismatch_is_an_error(self) -> None:
        fake = FakeGitHub.recorded()
        fake.clone_pull(7, 93)
        fake.pulls[93]["base"]["repo"]["full_name"] = "someone-else/other"
        with pytest.raises(ToolError, match="different repository"):
            _resolve(fake, 93)
        assert not _content_paths(fake)

    def test_missing_base_repository_metadata_is_a_gap(self) -> None:
        fake = FakeGitHub.recorded()
        fake.clone_pull(7, 94)
        del fake.pulls[94]["base"]["repo"]
        resolved = _resolve(fake, 94)
        assert any("base repository" in gap for gap in resolved.package.gaps)

    @pytest.mark.parametrize("repository", [FORK, "other/repo", "a/b/../c", "SUKU/x"])
    def test_client_refuses_cross_repository_requests(self, repository: str) -> None:
        fake = FakeGitHub.recorded()
        github_api.set_transport_for_tests(httpx.MockTransport(fake.handle))
        try:
            with GitHubApi(
                base_url="https://api.github.com",
                token="sentinelflow-test-token-" + "g" * 24,
                timeout_seconds=5,
                allowed_repository=REPO,
            ) as api:
                with pytest.raises(ToolError, match="refusing a GitHub request"):
                    api.get_file_content(repository, CONTRACT, ref=PR7_HEAD, max_bytes=100)
                with pytest.raises(ToolError, match="refusing a GitHub request"):
                    api.get_pull_request(repository, 7)
                with pytest.raises(ToolError, match="refusing a GitHub request"):
                    api.list_pull_request_files(repository, 7, max_files=10)
                assert api.get_pull_request(REPO, 7)["number"] == 7
        finally:
            github_api.set_transport_for_tests(None)
        assert all(request.url.path.lower().startswith(f"/repos/{REPO}/") for request in
                   fake.requests)  # fmt: skip


# ------------------------------------------------------------------ M2/M5 scanner errors


class TestScannerFailures:
    @pytest.mark.parametrize(
        "reason", ["gitleaks timed out", "gitleaks failed", "gitleaks returned no valid report"]
    )
    def test_secret_scan_failure_is_an_explicit_gap(self, reason: str) -> None:
        tools = FakeSignalTools()
        tools.scanner_error = reason
        resolved = _resolve(FakeGitHub.recorded(), 1, tools)
        assert f"{reason}; changed files were not scanned for secrets" in resolved.package.gaps
        assert resolved.security.findings == []
        assert _decision(resolved) == "REVIEW"

    @pytest.mark.parametrize(
        "reason", ["oasdiff timed out", "oasdiff failed", "oasdiff returned invalid output"]
    )
    def test_contract_diff_failure_is_an_explicit_gap(self, reason: str) -> None:
        tools = FakeSignalTools()
        tools.contract_error = reason
        resolved = _resolve(FakeGitHub.recorded(), 7, tools)
        assert any(gap.startswith(f"{reason}; compatibility") for gap in resolved.package.gaps)
        assert resolved.contract_diff.tool == "unavailable"
        assert _decision(resolved) == "REVIEW"
