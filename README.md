# SentinelFlow

Evidence-grounded multi-agent release-risk intelligence platform.

SentinelFlow ingests a proposed software change and produces an **evidence-cited impact analysis**, a **prioritized regression plan**, and an **auditable pass / review / block decision**. The release verdict is computed by a **deterministic risk engine** (dependency graph, contract diff, security scanners, policy gate) — not invented by an LLM. Language models are reserved for interpretation and retrieval (M4+).

| Milestone | Status | What shipped |
|---|---|---|
| **M0** | Complete | Typed contracts, FastAPI skeleton, domain-pack loader, CI (ruff, mypy, pytest) |
| **M1** | Complete | Deterministic signal layer: import graph, blast radius, oasdiff/buf, security scanners, risk engine, policy gate |
| **M2** | Complete | `sentinelflow-context` MCP server (7 tools, 3 resources), typed client adapter, read-only GitHub PR change source behind the same tools |
| **M3** | Complete | Hybrid RAG, pgvector + Qdrant adapters, Gemini embeddings, reranker ablation (none vs Voyage), eval harness |
| **M4** | Complete (merged in #1) | LangGraph workflow, five agents, triage planner, checkpoint + approval interrupt. A post-hardening live check ran on an OpenRouter stand-in model across 4 fixtures: both expected BLOCKs held, the 2 expected PASS fixtures returned REVIEW, and 0 invalid citations survived. Evidence coverage was 0% on the templated corpus. The primary Gemini model is not yet evaluated |
| **M5** | Built on branch, final review pending | M5-A: versioned HTTP API with durable jobs, `Idempotency-Key`, approval resume over HTTP, an evaluation trigger and machine-readable errors. M5-B: SSE progress stream with durable node events, `Last-Event-ID` replay and opt-in live structured-output fragments. M5-C: Streamlit harness that drives all of it over HTTP only. Live provider streaming is not yet verified |

Canonical architecture and roadmap: [SENTINELFLOW_PLAN.md](SENTINELFLOW_PLAN.md).  
Real obstacles and tradeoffs from M0–M5: [docs/ENGINEERING_JOURNAL.md](docs/ENGINEERING_JOURNAL.md).

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
uv sync --extra dev --extra ui   # also the M5-C Streamlit harness
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
uv run ruff check app mcp_server ui tests
uv run mypy app
uv run mypy ui
uv run pytest
```

`tests/ui` needs the `ui` extra; without it the Streamlit tests are skipped.

### 4. Run the API (M0+)

```bash
uv run python -m app.api.serve          # 127.0.0.1:8000, one worker (see M5)
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
| **oasdiff** | OpenAPI breaking-change detection | **Docker:** `docker pull tufin/oasdiff@sha256:e6afae33552b0c2cbd1d00883cdbf03b8b9b05ebb4d2f8d8cb8392ddb48837d5` (build `059301e`, pinned by digest; never pulled implicitly) |
| **buf** | Protobuf breaking-change detection | [github.com/bufbuild/buf](https://github.com/bufbuild/buf) — `brew install bufbuild/buf/buf` or release binary. Stubbed with TODO until domain packs ship `.proto` assets. |
| **gitleaks** | Secret detection | GitHub mode: `docker pull zricethezav/gitleaks:v8.30.1@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f` (or a gitleaks 8.30.1 binary on PATH). M1 fixture scans: [github.com/gitleaks/gitleaks](https://github.com/gitleaks/gitleaks) |
| **semgrep** | Static analysis (CE rules) | [github.com/semgrep/semgrep](https://github.com/semgrep/semgrep) — `pip install semgrep` or `brew install semgrep` |
| **osv-scanner** | Dependency vulnerability scan | [google/osv-scanner](https://google.github.io/osv-scanner/) — `go install github.com/google/osv-scanner/cmd/osv-scanner@latest` or GitHub release |

**Container vs local:**

- **oasdiff** — runs only from the digest-pinned `tufin/oasdiff` image, in a locked-down container (`app/deterministic/docker_sandbox.py`); there is no PyPI package. Any failure or unparseable output is `tool=unavailable`, never a clean diff.
- **Scanner containers** run with `--pull=never --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges --pids-limit=64 --memory=512m --cpus=1`, as an unprivileged user, with read-only inputs. Each gets a unique `sf-scan-<uuid>` name and is force-removed by that name after every run, including timeouts and cancellations.
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
| `get_change_package` | **Fixture mode:** reads `manifest.yaml` under `domains/<pack>/fixtures/changes/`. **GitHub mode:** normalizes a pull request (see below). |
| `get_dependency_graph` | Fully wired to M1 `dependency_graph.py` |
| `diff_contracts` | Fully wired to M1 `contract_diff.py` (fixture signals by default; oasdiff on base/head in GitHub mode) |
| `list_tests` | Reads fixture manifest test inventory (empty in GitHub mode) |
| `run_tests` | **Dry-run:** returns planned execution + hardened docker flags. **Live execution:** deferred to M7. Refused in GitHub mode. |
| `run_security_scan` | Fully wired to M1 `security_scanners.py` (fixture signals by default; gitleaks on the PR head in GitHub mode) |

Every change-scoped tool takes `source` (`fixture` by default, or `github`).
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

### GitHub change source

`AnalysisRequest(source="github", change_id="PR-7", domain_pack_id=...)` analyses a pull request instead of a fixture ([ADR-0002](docs/adr/0002-github-change-source.md)). Agents are unchanged: the runner pins `source` on every change-scoped MCP call, so a run cannot mix sources and agents never choose where data comes from.

- **Configuration.** The repository and the path-to-service rules come from `domains/<pack>/live_source.yaml`, never from tool input. Both packs point at the synthetic seed repo `sukumarmuthusamy/sentinelflow-demo-platform`.
- **Access.** GET requests to the GitHub REST API only. `SENTINELFLOW_GITHUB_TOKEN` is optional; for private repos use a fine-grained token with read-only *Pull requests* and *Contents* access. The token is never logged or echoed in errors.
- **Normalization.** Same `ChangePackageResponse` as fixtures. A code file originates at its owning service and a non-code file at the file itself, as in the fixtures. The PR title is sanitized and kept out of agent input; the body is not stored.
- **Signals.** oasdiff runs on the base and head versions of each changed contract (for a renamed or copied contract, the base is its original path). gitleaks runs on the head version of each changed file. Both run from digest-pinned local images in locked-down containers. gitleaks gets an explicit default configuration, so a `.gitleaks.toml`, a `.gitleaksignore` or an inline `gitleaks:allow` in the pull request cannot hide a finding. The default rule allowlists are unchanged. The test inventory is empty and semgrep is not run; both are reported as notes.
- **Repository boundary.** Every request goes to the configured repository, including for a pull request from a fork: the head commit is read through the base repository at its SHA, and the client refuses any other repository, so a token never reaches a fork. If GitHub does not serve the fork's head commit there, the reads become gaps.
- **Renames.** A renamed file counts at both paths, so moving code out of a service still impacts that service. An unsafe or missing previous path, or a contract moved away from its declared path, is a gap.
- **Incomplete data degrades.** A missing tool, a scanner that times out, fails or returns invalid output, an unreadable or oversized file, a truncated file list, an unattributed code file or a changed dependency manifest is recorded in `gaps`. The run then degrades to REVIEW before any LLM call (`override_applied = incomplete_change_data`) and never weakens a deterministic BLOCK. A missing PR, bad credentials, a rate limit or an API outage stops the run with the reason.
- **Live check:** `uv run python scripts/github_smoke.py --output <file>` compares fixture/PR pairs deterministically (no LLM calls). Two runs from 2026-11-23 are recorded:
  - `evals/github/m2_live_smoke.json`, without scanners. Every PR ended at REVIEW, because the missing oasdiff and gitleaks are recorded as gaps instead of being read as clean scans.
  - `evals/github/m2_live_smoke_tools.json`, with `tufin/oasdiff` and `zricethezav/gitleaks:v8.30.1`. There were no gaps, and two pairs matched their fixtures:
    - seed PR 1 reached PASS 10, like CHG-H001;
    - seed PR 8 reached BLOCK `critical-contract-break` from a real oasdiff ERR, like CHG-E002.

    Two did not, and the cause is in the seed scenarios, not the pipeline:
    - oasdiff rates seed PR 7's removed request property as WARN, not ERR, so it passes, while CHG-H002 blocks;
    - gitleaks' default allowlists skip seed PR 10's `…EXAMPLE` AWS key, so it passes, while CHG-H003 blocks.

  Seed PR 2 differs from CHG-E001 in blast radius because of a fixture inconsistency. Thresholds, scanner rules and expected labels were not changed to make these pairs match.

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

## M4 — Agents and graph

Callable from Python (no HTTP yet): `app/graph/` wires intake → deterministic signals → triage planner → parallel agents (`Send` + `defer=True` merge) → Evidence Verifier → optional repair (`Overwrite` on gaps) → risk engine → policy gate → report → `interrupt()` → `record_gate_decision`.

- **Run sample scenarios:** `uv run python -m scripts.run_m4_sample` (requires `GEMINI_API_KEY`, pgvector for retrieval). Add `--agent-model nvidia/nemotron-3.5-super-vl:free` with `OPENROUTER_API_KEY` set to run the agents on OpenRouter; any `vendor/model` ID routes there. Results go to `evals/graph/m4_hardened_sample_results.json` by default, and the script refuses to overwrite an existing results file unless `--overwrite` is passed. Each row records the deciding and matched policy overrides, grounding rejections, injection-screened chunk IDs and failed LLM calls. Values a run did not measure are `null`.
- **Call budget:** `ANALYSIS_MAX_LLM_CALLS` caps logical LLM calls and holds under parallel fan-out. Each call reserves capacity atomically before it starts and settles as completed or failed, and failed calls are not refunded. Token limits are checked against settled usage before each call, so they can be overshot only by calls already in flight.
- **Temperature sweep:** `uv run python -m scripts.temperature_sweep [--model <id>] [--out <path>]`
- **Post-hardening live validation (2026-11-23, same models, 4 fixtures):** results in `evals/graph/m4_hardened_sample_results.json`.
  - CHG-E003 and CHG-E002 are BLOCK. CHG-E003 also matched two REVIEW overrides that a first-match gate would have applied.
  - CHG-E002 paused for approval, resumed on the same thread and wrote exactly one audit record.
  - 0 invalid citations survived, and no fixture labels appeared in agent output.
  - Evidence coverage is 0.0 on every fixture. The knowledge corpus is templated and contains no change-specific facts, so the verifier correctly found nothing to support. The two documentation fixtures therefore return REVIEW (`weak-evidence`) instead of PASS. This is a calibration finding for M6, analyzed in the journal.
- **Live results (2026-11-22, agents on `nvidia/nemotron-3.5-super-vl:free`, triage on `gemini-3.5-flash-lite`):** two full five-scenario runs, 10 trajectories, 0 invalid citations and 0 invented evidence IDs; 9/10 decisions match fixture labels; approval interrupt and resume verified live. Results in `evals/graph/m4_sample_results*.json`; analysis in [docs/ENGINEERING_JOURNAL.md](docs/ENGINEERING_JOURNAL.md). `gemini-3.8-flash` not yet measured (free-tier daily cap). These runs predate the M4 hardening pass (fixture labels reached agent prompts and the verifier did not read chunk text), so they show the graph runs end to end, not decision quality.
- **Gate integrity (offline tests):** the policy gate takes the strictest of the score and all matched overrides, independent of `policy.yaml` order; the Evidence Verifier reads bounded chunk text and a `supported` judgment must carry a verbatim quote that code checks against the cited chunk; agents never see fixture descriptions, categories, expected decisions or synthetic verification fields; failures degrade to REVIEW without weakening a BLOCK. Prompts are `v3`.
- **Tests:** `tests/graph/` runs the full graph offline (static LLM, in-memory MCP, fake retriever): interrupt/resume and exactly-once audit writes, evidence grounding, label leakage, degradation paths

See [SENTINELFLOW_PLAN.md § M4](SENTINELFLOW_PLAN.md) for exit criteria.

---

## M5 — HTTP API (M5-A: contracts and job lifecycle; M5-B: SSE streaming; M5-C: harness UI)

A local development harness, not a public service. The design, guarantees and limits are in [ADR-0003](docs/adr/0003-api-job-lifecycle.md).

**Run.** `uv run python -m app.api.serve` binds `127.0.0.1:8000` with one worker. Without `API_KEY`, `/v1` answers loopback clients only. With it, every request needs `Authorization: Bearer <key>`, and `--host` may be non-loopback. OpenAPI docs are at `/docs`. **A `POST /v1/analyses` runs the real graph and spends provider quota.** It needs `GEMINI_API_KEY`, plus `OPENROUTER_API_KEY` for `vendor/model` IDs, and pgvector for retrieval. Without them the request is refused with `503 PROVIDER_NOT_CONFIGURED` before a job is created.

| Method and path | Purpose |
|---|---|
| `POST /v1/analyses` | Start an analysis. Body `{change_id, domain_pack_id, objective?, execute_tests?, record_decision?, source?}`. Returns `202` and `Location`. Optional `Idempotency-Key` |
| `GET /v1/analyses/{job_id}` | Job status: `queued`, `running`, `waiting_for_approval`, `completed` or `failed` |
| `GET /v1/analyses/{job_id}/decision` | Risk assessment, cited report, plan, degradation codes, approval state and trace. Available while waiting for approval and after completion; `409 RESULT_NOT_AVAILABLE` before that |
| `POST /v1/analyses/{job_id}/approval` | `{approved, reviewer, comment?}` (strict). Resumes the original LangGraph thread. Accepted once only |
| `GET /v1/analyses/{job_id}/events` | Server-Sent Events: lifecycle and node events (replayable with `Last-Event-ID`) and, when enabled, live `agent.token` fragments. Ends after `job.completed` / `job.failed` |
| `POST /v1/evaluations` | `{suite: "deterministic_fixtures", domain_pack_ids?}`. Runs the M1 fixture evaluation offline. Optional `Idempotency-Key` |
| `GET /v1/evaluations/{job_id}` and `.../result` | Evaluation status and per-fixture rows with a summary |

**Semantics.**

- **Replays and conflicts.** The same `Idempotency-Key` with the same validated body returns the original job (`200`, `Idempotent-Replayed: true`), even if that job failed. The same key with a different body is `409 IDEMPOTENCY_KEY_CONFLICT`.
- **REVIEW is not a failure.** A REVIEW outcome, including a degraded run, is a `completed` job. `failed` means execution stopped: provider quota or auth, MCP, the change source, or an interrupted process.
- **Restarts.** A `running` job found at startup is marked `JOB_INTERRUPTED` and is not re-run. Queued jobs are relaunched, and jobs waiting for approval stay resumable.
- **Errors.** Every error is `{"error": {code, message, details, request_id}}`.

**Example.**

```bash
curl -s -X POST localhost:8000/v1/evaluations -H 'Idempotency-Key: eval-1' -H 'content-type: application/json' -d '{}'
curl -s localhost:8000/v1/evaluations/<job_id>/result
```

**Tests.** `tests/api/` drives the ASGI app over HTTP with a static LLM, the in-process MCP server on fixtures and a fake retriever. No test makes a provider or network call. Stream tests use a streaming ASGI client (`tests/api/sse_client.py`) and `FakeStreamingGatewayLLM`, the production gateway over a chat model that really streams its JSON in chunks.

**Streaming (M5-B).** One SSE stream per analysis; the full policy is in ADR-0003 "Streaming".

- **Durable events** carry `id:` = per-job `seq` and are replayed after `Last-Event-ID`, without duplicates:
  - `job.*` lifecycle events;
  - `node.started` / `node.completed`, with the node, LangGraph task id, agent, run number, and the *names* of the state keys a node wrote.
- **Live events** carry no `id:` and are never replayed. Each has `after_seq`, the durable event it follows:
  - `agent.token`: provisional, unverified JSON fragments of an agent's structured output;
  - `stream.gap`: this client fell behind and lost live events.
- **Stream lifetime.** The stream stays open while the job waits for approval, with keepalive comments, and ends after `job.completed` or `job.failed`. Disconnecting never affects the job.
- **Token streaming is opt-in** (`API_STREAM_TOKENS=true`). It makes providers use their streaming APIs, which have not been verified live. Node and lifecycle events always stream.

```bash
curl -N localhost:8000/v1/analyses/<job_id>/events
curl -N -H 'Last-Event-ID: 12' localhost:8000/v1/analyses/<job_id>/events
```

### M5-C — Streamlit harness

A local developer harness for the API (`ui/`). It is an HTTP client only: it imports nothing from `app`, never runs the graph or reads the job store, and every action is one of the `/v1` calls above. `tests/ui/test_http_only_boundary.py` pins that. Design and limits: ADR-0003 "Harness UI".

- **Analysis:** pick a domain pack and change id (bundled fixtures are suggested; any id can be typed), the source (`fixture` or `github`) and the request options, then launch. Or open an existing job by id; that only reads it.
- **Progress:** job status and stage, one row per node task from the SSE stream (parallel agents are told apart by agent and task id), lifecycle events with their ids, and live fragments only when the server sends them, labelled provisional and unverified.
- **Decision:** PASS / REVIEW / BLOCK from the deterministic gate, shown apart from the LLM agents' supporting analysis (impacted services, cited findings, recommended tests), with evidence coverage, degradation reasons and trace metrics.
- **Approval:** only while the job is `waiting_for_approval`. You must enter a reviewer and tick a confirmation; nothing is approved automatically. The decision stays visible afterwards, with the final audit status (recorded, rejected, or not recorded after a failed write).
- **Evaluation:** runs the deterministic fixture suite and labels its result a fixture consistency check, not model-quality accuracy.

**How the page uses the API.** Launches send an `Idempotency-Key` that depends on the form contents. Pressing Launch twice, or retrying after a timeout, returns the job already created; **New submission** starts a fresh key. Reruns never send a create. While a job is queued or running, the page reads the event stream for about 1.5 s at a time from the last durable event id, then disconnects. So a session holds at most one subscription, a reconnect resumes without duplicate rows, and a job waiting for approval holds none. Fragments missed between reads are counted, never filled in.

**Install and launch.**

```bash
uv sync --extra dev --extra ui
uv run --extra ui python -m ui.harness            # http://127.0.0.1:8501, loopback only
```

Windows PowerShell is the same, plus how to point the UI at another API or pass a key:

```powershell
$env:SENTINELFLOW_API_URL = "http://127.0.0.1:8000"   # default
$env:SENTINELFLOW_API_KEY = "<only if the server sets API_KEY>"
uv run --extra ui python -m ui.harness
```

The key can also be typed into the sidebar's password field; it stays in that browser session and is never displayed. The harness sends a key only over `https://` or to a loopback address (`127.0.0.1`, `::1`, `localhost`); for any other `http://` URL it shows `INSECURE_API_URL` and sends nothing. `python -m ui.harness` binds 127.0.0.1, turns off Streamlit usage statistics and hides error details. `.streamlit/config.toml` does the same for `streamlit run ui/streamlit_app.py`.

**Local demo (offline, no credentials).**

1. Start the API with offline test doubles. The graph, job store, SSE, gate, MCP server and audit log are real; the model gives canned answers and retrieval is off:
   ```bash
   uv run --extra dev python scripts/serve_offline_demo.py --stream-tokens
   ```
2. Start the UI: `uv run --extra ui python -m ui.harness`, then open http://127.0.0.1:8501.
3. Launch `healthcare-claims` / `CHG-H001` from fixtures.
4. Watch the node rows fill in, the parallel agents, and the provisional fragments.
5. Inspect the decision: PASS for `CHG-H001`. Try `CHG-H003` for BLOCK with policy overrides, or `CHG-H004` for REVIEW.
6. Tick "Require human approval", launch, then approve or reject with a reviewer name. Check the audit status.
7. Open the Evaluation tab and run the fixture suite: 8 fixtures, all matching their labels.

If port 8000 or 8501 is taken, pass `--port` to either command and set `SENTINELFLOW_API_URL` to match.

**What needs real credentials.** With `python -m app.api.serve`, analyses call the configured providers: `GEMINI_API_KEY`, plus `OPENROUTER_API_KEY` for `vendor/model` ids. Retrieval needs pgvector from `compose.yaml`. `github` sources need the server's GitHub configuration and token. Without them, launch shows `PROVIDER_NOT_CONFIGURED` or `CHANGE_SOURCE_NOT_CONFIGURED`. The evaluation tab never needs credentials. Live structured-output streaming (`API_STREAM_TOKENS=true`) has not been verified against Gemini or OpenRouter.
