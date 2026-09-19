# SentinelFlow

Evidence-grounded multi-agent release-risk intelligence platform.

SentinelFlow ingests a proposed software change and produces an **evidence-cited impact analysis**, a **prioritized regression plan**, and an **auditable pass / review / block decision**. The release verdict is computed by a **deterministic risk engine** (dependency graph, contract diff, security scanners, policy gate) — not invented by an LLM. Language models are reserved for interpretation and retrieval (M4+).

| Milestone | Status | What shipped |
|---|---|---|
| **M0** | Complete | Typed contracts, FastAPI skeleton, domain-pack loader, CI (ruff, mypy, pytest) |
| **M1** | Complete | Deterministic signal layer: import graph, blast radius, oasdiff/buf, security scanners, risk engine, policy gate |
| **M2** | Complete | `sentinelflow-context` MCP server (7 tools, 3 resources), typed client adapter |
| **M3** | Complete | Hybrid RAG, pgvector + Qdrant adapters, Gemini embeddings, reranker ablation (none vs Voyage), eval harness |
| **M4** | Not started | LangGraph agents, triage planner, evidence verifier, temperature sweep |

Canonical architecture and roadmap: [SENTINELFLOW_PLAN.md](SENTINELFLOW_PLAN.md).  
Real obstacles and tradeoffs from M0–M3: [docs/ENGINEERING_JOURNAL.md](docs/ENGINEERING_JOURNAL.md).

---

## Prerequisites

| Requirement | Used for |
|---|---|
| **Python 3.12+** | Application and tests (`pyproject.toml`) |
| **Docker Desktop** (or Docker Engine) | `compose.yaml` Postgres + Qdrant (M3); **oasdiff** via `tufin/oasdiff` image (M1) |
| **GEMINI_API_KEY** | M3 embeddings (ingestion + retrieval) — [Google AI Studio](https://aistudio.google.com/apikey) |
| **VOYAGE_API_KEY** | Optional — M3 Voyage reranker comparison arm only — [voyageai.com](https://www.voyageai.com/) |

M0–M2 and M1 fixture evaluation require **no API keys**. M3 requires Gemini + running Docker services.

---

## Setup

### 1. Clone and install

**With [uv](https://github.com/astral-sh/uv) (recommended):**

```bash
uv sync --extra dev
```

**With pip:**

```bash
pip install -e ".[dev]"
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set GEMINI_API_KEY before M3 ingest/eval
```

All supported variables are listed in [`.env.example`](.env.example). Summary:

| Variable | Required | Default / notes |
|---|---|---|
| `DEBUG` | No | `false` |
| `DOMAINS_ROOT` | No | `domains` |
| `DEFAULT_DOMAIN_PACK` | No | first sorted pack, or `healthcare-claims` in example |
| `DATABASE_URL` | M3 pgvector | `postgresql+psycopg://sentinelflow:sentinelflow@localhost:5433/sentinelflow` — pgvector adapter converts to `asyncpg` internally |
| `POSTGRES_*` | No | localhost dev credentials matching `compose.yaml` |
| `QDRANT_URL` | M3 Qdrant arm | `http://localhost:6333` |
| `QDRANT_API_KEY` | No | empty for local Qdrant |
| `GEMINI_API_KEY` | **M3** | no default — set in `.env` |
| `EMBEDDING_MODEL` | No | `gemini-embedding-2` |
| `EMBEDDING_DIMENSIONS` | No | `768` |
| `EMBEDDING_CACHE_PATH` | No | `.sentinelflow/embedding_cache.sqlite` (gitignored) |
| `RERANKER_MODEL` | No | `Qwen/Qwen3-Reranker-0.6B` (local, CPU-only) |
| `VOYAGE_API_KEY` | No | Voyage rerank arm only |
| `VOYAGE_RERANK_MODEL` | No | `rerank-2.5-lite` |
| `VECTOR_STORE_BACKEND` | No | `pgvector` or `qdrant` |
| `RETRIEVAL_TOP_K` | No | `10` |
| `RERANK_TOP_N` | No | `5` |
| `SENTINELFLOW_AUDIT_LOG_PATH` | No | `.sentinelflow/audit/gate_decisions.jsonl` if unset |

Runtime output under `.sentinelflow/` and `evals/retrieval/reports/` is gitignored.

### 3. Run tests (any milestone)

```bash
uv run ruff check app mcp_server tests
uv run mypy app
uv run pytest
```

### 4. Run the API (M0+)

```bash
uv run uvicorn app.main:app --reload
```

---

## M0 — Contracts and skeleton

- **Domain packs** under `domains/<pack>/`: `domain.yaml`, `dependencies.yaml`, `policy.yaml`, change fixtures.
- **CI** (`.github/workflows/ci.yml`): ruff, mypy, pytest on push/PR — no external services required.

---

## M1 — Deterministic signal layer

M1 runs with **zero LLM calls and zero API keys**. Change fixtures under `domains/<pack>/fixtures/changes/` carry pre-recorded scanner and contract-diff signals for offline CI; live tools are used when installed.

**Evaluate a fixture (Python):**

```python
from pathlib import Path
from app.deterministic import evaluate_change_fixture

result = evaluate_change_fixture(
    Path("domains/healthcare-claims/fixtures/changes/CHG-H001"),
    fixture_only=True,  # offline CI mode — uses signals/*.json
)
print(result.risk_score, result.decision)
```

**CLI:**

```bash
python scripts/run_fixture.py healthcare-claims CHG-H002
```

### External tools (not pip packages)

Optional for local development. CI and `fixture_only=True` use committed signals when tools are absent.

| Tool | Purpose | Install |
|---|---|---|
| **oasdiff** | OpenAPI breaking-change detection | **Docker (recommended):** `docker pull tufin/oasdiff` — invoked as `docker run --rm tufin/oasdiff breaking ...` |
| **buf** | Protobuf breaking-change detection | [github.com/bufbuild/buf](https://github.com/bufbuild/buf) — `brew install bufbuild/buf/buf` or release binary. Stubbed with TODO until domain packs ship `.proto` assets. |
| **gitleaks** | Secret detection | [github.com/gitleaks/gitleaks](https://github.com/gitleaks/gitleaks) — `brew install gitleaks` or GitHub release |
| **semgrep** | Static analysis (CE rules) | [github.com/semgrep/semgrep](https://github.com/semgrep/semgrep) — `pip install semgrep` or `brew install semgrep` |
| **osv-scanner** | Dependency vulnerability scan | [google/osv-scanner](https://google.github.io/osv-scanner/) — `go install github.com/google/osv-scanner/cmd/osv-scanner@latest` or GitHub release |

**Container vs local:**

- **oasdiff** — always runs via the `tufin/oasdiff` Docker image when Docker is available; there is no PyPI package.
- **gitleaks, semgrep, osv-scanner** — subprocess wrappers expect binaries on `PATH`.
- **buf** — subprocess when installed; otherwise returns `tool=unavailable` and fixtures supply offline signals.

### Python dependencies (pip)

Installed via `pip install -e ".[dev]"` or `uv sync`:

- `grimp` — resolver-accurate Python import graph
- `networkx` — merged dependency graph and reverse-BFS blast radius
- `tree-sitter`, `tree-sitter-python`, `tree-sitter-javascript` — heuristic import edges for non-Python files

---

## M2 — MCP boundary

The M1 deterministic layer is exposed through the **`sentinelflow-context`** MCP server (`mcp` 2.2.0, `MCPServer`) and consumed via the typed client adapter in `app/mcp_client/` ([ADR-0001](docs/adr/0001-mcp-client-adapter.md)).

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

---

## M3 — Retrieval

Hybrid RAG over domain-pack knowledge corpora with a swappable vector store port, Gemini embeddings, and reranker ablation.

### Prerequisites — start services first

```bash
docker compose up -d postgres qdrant
```

Copy `.env.example` to `.env` and set at minimum **`GEMINI_API_KEY`**. Optional: **`VOYAGE_API_KEY`** for the Voyage `rerank-2.5-lite` comparison arm.

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

# Full M3 eval: pgvector vs Qdrant + reranker ablation (none / Voyage)
python evals/retrieval/run_eval.py --skip-local-reranker

# Reranker ablation only (when corpora are already ingested)
python evals/retrieval/run_eval.py --skip-ingest --skip-store-comparison --skip-local-reranker
```

**Eval flags:**

| Flag | Effect |
|---|---|
| `--skip-ingest` | Skip re-indexing (use existing vectors) |
| `--skip-store-comparison` | Skip pgvector vs Qdrant arms |
| `--skip-reranker-ablation` | Skip reranker arms entirely |
| `--skip-local-reranker` | Omit local cross-encoder arm (recommended on laptops without GPU/RAM for Qwen) |
| `--pack <id>` | Limit to one domain pack (repeatable) |

Labeled queries: `evals/retrieval/labels/*-{easy,hard}.jsonl`. Metrics: recall@5, recall@10, MRR, nDCG@10 (reported per easy / hard / all). Latest numbers: `evals/retrieval/reports/latest.json` (gitignored — produced locally by `run_eval.py`).

### Local reranker (documented, not used at eval scale on constrained hardware)

We evaluated local cross-encoder reranking and chose hosted Voyage for ablation on this dev machine. Details: [docs/ENGINEERING_JOURNAL.md](docs/ENGINEERING_JOURNAL.md#local-cross-encoder-reranking-memory-architecture-and-throughput).

| Stage | Finding |
|---|---|
| Initial CPU attempt | `BAAI/bge-reranker-v2-m3` and `Qwen/Qwen3-Reranker-0.6B` both failed with `OSError: paging file too small` under default Windows paging settings. |
| Paging fix | Increasing the paging file allowed **Qwen3-Reranker-0.6B to load on CPU** (`LOCAL_RERANKER_DEVICE = "cpu"`). |
| GPU offload | NVIDIA Quadro M1000M (Maxwell, **sm_50**) is below PyTorch's minimum supported compute capability (**sm_75+**); CUDA wheels detect the GPU but inference fails with `no kernel image is available for execution on the device`. |
| Throughput | After load (~2 min), a single retrieve+rerank pass (`k=10`) took **~14.5 minutes/query** — ~17 hours for the full local ablation alone. |

**Operational choice:** use **`--skip-local-reranker`** for none vs Voyage ablation. Local cross-encoders remain in code for environments with sufficient CPU/GPU.

### Architecture

| Component | Location |
|---|---|
| Vector store port | `app/rag/vector_store.py` |
| pgvector adapter (default) | `app/rag/pgvector_store.py` — `PGVectorStore`, tsvector + GIN, RRF |
| Qdrant adapter | `app/rag/qdrant_store.py` — `RetrievalMode.HYBRID`, BM25 sparse + server fusion |
| Embeddings | `app/rag/embeddings.py` — `gemini-embedding-2`, 768-dim, SQLite cache |
| Rerankers | `app/rag/reranker.py` — `Qwen/Qwen3-Reranker-0.6B` (local, CPU-only), Voyage (hosted) |
| Eval harness | `evals/retrieval/` |

---

## M4 — Agents and graph (not yet started)

Next milestone: LangGraph orchestration, five specialist agents, triage planner, evidence verifier, and empirical Gemini temperature sweep. See [SENTINELFLOW_PLAN.md § M4](SENTINELFLOW_PLAN.md).
