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

## M3 — Retrieval

Hybrid RAG over domain-pack knowledge corpora with a swappable vector store port, Gemini embeddings, and reranker ablation.

### Prerequisites — start these services first

```bash
docker compose up -d postgres qdrant
```

Copy `.env.example` to `.env` and set at minimum:

- `GEMINI_API_KEY` — required for `gemini-embedding-2` embeddings (ingestion + retrieval)
- `VOYAGE_API_KEY` — **optional**, only needed for the Voyage `rerank-2.5-lite` comparison arm (separate key from Gemini; [200M free tokens on signup](https://www.voyageai.com/))

Postgres listens on **localhost:5433** (pgvector). Qdrant listens on **localhost:6333**.

### Knowledge corpora

Each domain pack has a synthetic corpus under `domains/<pack>/knowledge/` (requirements, contracts, ADRs, test plans, policies, runbooks, incidents, ownership). Regenerate with:

```bash
python scripts/bootstrap_knowledge_corpus.py
```

### Ingest and evaluate

```bash
# Ingest both packs into pgvector + Qdrant (requires GEMINI_API_KEY + running services)
python scripts/ingest_knowledge.py

# Full M3 eval: pgvector vs Qdrant + reranker ablation (none / Voyage on this machine)
python evals/retrieval/run_eval.py --skip-local-reranker

# Reranker ablation only (when corpora are already ingested)
python evals/retrieval/run_eval.py --skip-ingest --skip-store-comparison --skip-local-reranker
```

Labeled queries live in `evals/retrieval/labels/*-{easy,hard}.jsonl` (easy + hard batches per pack). Metrics: recall@5, recall@10, MRR, nDCG@10.

**Local reranker decision (documented, not used at eval scale here):** We evaluated local cross-encoder reranking on this development machine and chose hosted Voyage for ablation and comparison instead.

| Stage | Finding |
|---|---|
| Initial CPU attempt | `BAAI/bge-reranker-v2-m3` and `Qwen/Qwen3-Reranker-0.6B` both failed with `OSError: paging file too small` under default Windows paging settings. |
| Paging fix | Increasing the paging file allowed **Qwen3-Reranker-0.6B to load on CPU** (`LOCAL_RERANKER_DEVICE = "cpu"`). |
| GPU offload | NVIDIA Quadro M1000M (Maxwell, **sm_50**) is below PyTorch's minimum supported compute capability (**sm_75+**); CUDA wheels detect the GPU but inference fails with `no kernel image is available for execution on the device`. No viable GPU path on this hardware. |
| Throughput | After load (~2 min), a single retrieve+rerank pass (`k=10`) took **~14.5 minutes/query** on this 2015-era mobile CPU — ~17 hours for the full local ablation alone. |

**Operational choice:** M3 reranker ablation on this machine runs **none vs Voyage** (`rerank-2.5-lite`) via `--skip-local-reranker`. The local cross-encoder path remains in code for environments with sufficient CPU/GPU capacity; it is intentionally excluded from eval sweeps here.

### Architecture

| Component | Location |
|---|---|
| Vector store port | `app/rag/vector_store.py` |
| pgvector adapter (default) | `app/rag/pgvector_store.py` — `PGVectorStore`, tsvector + GIN, RRF |
| Qdrant adapter | `app/rag/qdrant_store.py` — `RetrievalMode.HYBRID`, BM25 sparse + server fusion |
| Embeddings | `app/rag/embeddings.py` — `gemini-embedding-2`, 768-dim |
| Rerankers | `app/rag/reranker.py` — `Qwen/Qwen3-Reranker-0.6B` (local, CPU-only), Voyage (hosted; used for ablation here) |
| Eval harness | `evals/retrieval/` |
