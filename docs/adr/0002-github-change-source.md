# ADR-0002: GitHub change source inside the sentinelflow-context server

- **Status:** Accepted
- **Date:** 2026-11-23
- **Deciders:** SentinelFlow maintainers

## Context

M2 requires a live GitHub change source behind the same MCP port as fixtures. Agents must reach change data only through the MCP client, and a fixture and its equivalent pull request must run through the same interface. The plan named GitHub's official MCP server (read-only toolset) as that source.

The deterministic layer runs inside `sentinelflow-context`. oasdiff needs the base and head versions of each contract. gitleaks needs the changed files on disk. The dependency graph needs the changed paths mapped to the pack's services. So whatever fetches the PR must deliver its data to the place where those tools run.

## Decision

`sentinelflow-context` reads pull requests itself, using GET-only calls to the GitHub REST API (`mcp_server/sentinelflow_context/github_api.py`). It normalizes each PR into the existing `ChangePackageResponse` and serves it through the same seven tools, which take `source="github"`. On the client side, `SourceBoundMcpClient` pins `source` on every change-scoped call, so agent code is unchanged and a run cannot mix sources.

- The repository and the path-to-service rules come from `domains/<pack>/live_source.yaml`. A tool argument can never point the server at another repository.
- A token is optional: public repositories are readable without one. When one is set (`SENTINELFLOW_GITHUB_TOKEN`), it should be a fine-grained token with read-only *Pull requests* and *Contents* access.
- Missing data or tooling is reported in `gaps`. The graph then degrades to REVIEW instead of reading the absence as clean.

### Hardening (independent review, 2026-11-23)

- **Repository boundary.** All requests, including head-file reads for a pull request from a fork, go to the configured repository. A fork's head commit is read through the base repository at its SHA, which GitHub serves for PR heads (verified against a public fork PR: base repository at the fork's head SHA returned 200, and an unknown SHA returned 404). `GitHubApi` refuses any other repository, so a token never reaches a fork. An unreadable head commit, or missing or malformed repository metadata, is a gap. A base repository that differs from the configured one is an error.
- **Renames.** A renamed file is attributed at both its old and new paths. Its `previous_filename` must pass the same path checks as `filename`. A contract moved away from its declared path, or one with no usable base path, is a gap.
- **Scanners.** oasdiff and gitleaks run from registry-digest-pinned images in locked-down, uniquely named containers that are always force-removed. Scanner output is parsed strictly. A timeout, failure or malformed output raises `ScannerError`, which becomes a gap. gitleaks gets an explicit default configuration and ignores PR-supplied `.gitleaks.toml`, `.gitleaksignore` and inline `gitleaks:allow` suppressions.

## Consequences

### Positive

- One MCP boundary: agents and the deterministic node both read change data through the MCP client.
- REST responses are versioned (`X-GitHub-Api-Version`), so offline tests can replay recorded responses, including rate-limit, permission and truncation cases.
- The public seed repository needs no credentials, which keeps the live smoke test free of secrets.

### Negative

- This is a small custom GitHub client, which the plan had chosen to avoid. It covers three endpoints (PR, PR files, file contents) and nothing else.
- Unauthenticated use is limited to 60 requests per hour. Each file scanned costs a request.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| GitHub official MCP server as a second client connection | Its output would have to be forwarded into `sentinelflow-context` for oasdiff and gitleaks anyway. It always needs a token, even for public repositories. Its tool output shapes are not versioned contracts. It remains a drop-in option behind `GitHubApi` if live CI or check-run data is needed later |
| Agents call GitHub directly | Bypasses the MCP boundary, which is the M2 exit criterion |
| Clone the repository | Fetches full history and every file when the analysis only needs the changed files |

## References

- [SENTINELFLOW_PLAN.md](../../SENTINELFLOW_PLAN.md), section 6 (MCP boundary) and section 8 (M2)
- [ADR-0001](0001-mcp-client-adapter.md)
