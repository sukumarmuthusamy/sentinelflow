# SentinelFlow

Evidence-grounded multi-agent release-risk intelligence platform.

See [SENTINELFLOW_PLAN.md](SENTINELFLOW_PLAN.md) for the canonical architecture and roadmap.

## Local development

```bash
pip install -e ".[dev]"
ruff check app mcp_server tests
mypy app
pytest
uvicorn app.main:app --reload
```

If you use [uv](https://github.com/astral-sh/uv):

```bash
uv sync --extra dev
uv run ruff check app tests
uv run mypy app
uv run pytest
```

## M1 — Deterministic signal layer

M1 runs with **zero LLM calls and zero API keys**. Change fixtures under `domains/<pack>/fixtures/changes/` carry pre-recorded scanner and contract-diff signals for offline CI; live tools are used when installed.

Evaluate a fixture:

```python
from pathlib import Path
from app.deterministic import evaluate_change_fixture

result = evaluate_change_fixture(
    Path("domains/healthcare-claims/fixtures/changes/CHG-H001"),
    fixture_only=True,  # offline CI mode
)
print(result.risk_score, result.decision)
```

### External tools (not pip packages)

These are optional for local development. CI uses fixture signals when they are absent.

| Tool | Purpose | Install |
|---|---|---|
| **oasdiff** | OpenAPI breaking-change detection | **Docker (recommended):** `docker pull tufin/oasdiff` — invoked as `docker run --rm tufin/oasdiff breaking ...` |
| **buf** | Protobuf breaking-change detection | [github.com/bufbuild/buf](https://github.com/bufbuild/buf) — `brew install bufbuild/buf/buf` or download release binary. Stubbed with TODO until domain packs ship `.proto` assets. |
| **gitleaks** | Secret detection | [github.com/gitleaks/gitleaks](https://github.com/gitleaks/gitleaks) — `brew install gitleaks` or GitHub release |
| **semgrep** | Static analysis (CE rules) | [github.com/semgrep/semgrep](https://github.com/semgrep/semgrep) — `pip install semgrep` or `brew install semgrep` |
| **osv-scanner** | Dependency vulnerability scan | [google/osv-scanner](https://google.github.io/osv-scanner/) — `go install github.com/google/osv-scanner/cmd/osv-scanner@latest` or GitHub release |

**Container vs local:**

- **oasdiff** — always runs via the `tufin/oasdiff` Docker image when Docker is available; there is no PyPI package.
- **gitleaks, semgrep, osv-scanner** — subprocess wrappers expect binaries on `PATH`.
- **buf** — subprocess when installed; otherwise returns `tool=unavailable` and fixtures supply offline signals.

### Python dependencies (pip)

Installed via `pip install -e ".[dev]"`:

- `grimp` — resolver-accurate Python import graph
- `networkx` — merged dependency graph and reverse-BFS blast radius
- `tree-sitter`, `tree-sitter-python`, `tree-sitter-javascript` — heuristic import edges for non-Python files

## M2 — MCP boundary

The M1 deterministic layer is exposed through the **`sentinelflow-context`** MCP server (`mcp` 2.2.0, `MCPServer`) and consumed via the typed client adapter in `app/mcp_client/` (ADR-0001).

### Run the MCP server

```bash
# stdio transport (for MCP Inspector / Claude Desktop)
python -m mcp_server.sentinelflow_context

# or with the MCP dev CLI + Inspector UI
mcp dev mcp_server/sentinelflow_context/server.py
```

Verify discovery (7 tools + 3 resources):

```bash
python scripts/verify_mcp_inspector.py
```

### Tools

| Tool | Status |
|---|---|
| `get_change_package` | **Fixture mode:** reads `manifest.yaml` under `domains/<pack>/fixtures/changes/`. **GitHub mode:** stubbed with explicit `ToolError` (deferred — plan section 15). |
| `get_dependency_graph` | Fully wired to M1 `dependency_graph.py` |
| `diff_contracts` | Fully wired to M1 `contract_diff.py` (fixture signals by default) |
| `list_tests` | Reads fixture manifest test inventory |
| `run_tests` | **Dry-run:** returns planned execution + hardened docker flags. **Live execution:** deferred to M7. |
| `run_security_scan` | Fully wired to M1 `security_scanners.py` (fixture signals by default) |
| `record_gate_decision` | Append-only JSONL audit log with idempotency key |

### Resources

- `service-catalog://current` — services from the active domain pack
- `release-policy://current` — `policy.yaml` thresholds and overrides
- `dependency-graph://current` — seed graph from `dependencies.yaml`

### Client adapter

```python
from app.mcp_client import SentinelFlowMcpAdapter
from mcp_server.sentinelflow_context.server import mcp

async with SentinelFlowMcpAdapter(mcp) as client:
    result = await client.call_tool(
        "get_change_package",
        {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"},
    )
```

Business/validation failures surface as `ToolError` (`is_error=True` on the wire). Protocol failures map to `McpProtocolError`.
