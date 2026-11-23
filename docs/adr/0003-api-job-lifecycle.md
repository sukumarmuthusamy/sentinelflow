# ADR-0003: HTTP job lifecycle for the local harness (M5-A)

- **Status:** Accepted
- **Date:** 2026-11-23
- **Deciders:** SentinelFlow maintainers

## Context

M5 requires the whole demo to run over HTTP: start an analysis, poll it, read the decision, approve the gated write, trigger an evaluation, and (M5-B) stream progress. The graph already has the hard parts. `run_analysis` / `resume_analysis` run on a LangGraph thread with SQLite checkpoints and `durability="sync"`. The interrupt sits before `record_gate_decision`, the audit write is idempotent, and resume restores budgets and trace counters. The API must drive that graph without weakening any M4 invariant.

An analysis takes minutes and spends free-tier provider quota. Clients time out and retry, so a retry must never silently pay for a second analysis. The plan rules out Celery or Redis (scale-to-zero, §10): background execution with durable job state, and load-tested before a queue is added (M7).

## Decision

### Layers

| Layer | Module | Owns |
|---|---|---|
| Transport | `app/api/` | Routes, strict request schemas, error envelope, access guard, body cap |
| Service | `app/jobs/service.py` | Lifecycle rules, idempotent creation, launch, approval, recovery |
| Persistence | `app/persistence/job_store.py` | Atomic creates, compare-and-set transitions, lifecycle event log |
| Execution | `app/jobs/runtime.py` | `GraphExecutor`: the only caller of `run_analysis` / `resume_analysis`; `AnalysisRuntime` port for MCP, LLM and retrieval |

Routes never touch the graph or the store. M5-B adds streaming inside `GraphExecutor` (switching `ainvoke` to `astream`) and an SSE route that reads the event log. The service and existing routes stay as they are.

### Persistence

The store is one SQLite file, `.sentinelflow/api_jobs.sqlite`, separate from the checkpoint database and opened per operation with WAL mode. Every write runs inside `BEGIN IMMEDIATE`, so these are each one atomic step across threads and processes:

- **Create:** look up the idempotency key, enforce `API_MAX_ACTIVE_JOBS`, insert the job and its `job.queued` event.
- **Transition:** `UPDATE … WHERE status = <expected>`, plus that transition's lifecycle events, in the same transaction.

The stored projection is the public `AnalysisResult`, built field by field from deterministic state. Raw graph state (chunk text, tool payloads, error strings) stays in the checkpoint database and never reaches the job store or a response.

### Lifecycle

```
queued ──► running ──► completed
  │           │  └───► failed
  │           └──────► waiting_for_approval ──(approval)──► queued ──► running ──► …
  └─────────► failed  (shutdown or startup failure)
```

The table in `app/jobs/models.py` is closed. Terminal states have no exits, and the store refuses any move it does not list (`ValueError`, so a coding error cannot pass for a lost race). A conservative REVIEW, including a degraded run, is a **completed** analysis. A job fails only when execution itself stops: a provider quota or auth refusal, an MCP failure that escapes the graph, a change-source error, or an unexpected exception.

### Idempotency

- `Idempotency-Key` is optional on `POST /v1/analyses` and `POST /v1/evaluations`. Keys are scoped per job kind, kept indefinitely, and stored as SHA-256 hashes only.
- The fingerprint is the SHA-256 of the **validated** request with defaults filled in, so `{}` and an explicit default are the same request.
- **Same key, same fingerprint:** `200` with `Idempotent-Replayed: true` and the original job in its current status, including `failed`. Nothing is relaunched. A client retries failed work with a new key.
- **Same key, different fingerprint:** `409 IDEMPOTENCY_KEY_CONFLICT`.
- **Concurrent creates with one key:** the store's write lock admits one insert. The others read the winner and replay it or conflict. Only the inserting request launches a task.
- A request refused before insertion (validation, `PROVIDER_NOT_CONFIGURED`, `TOO_MANY_ACTIVE_JOBS`) does not consume the key.

### Execution

- Each job runs as an asyncio task in the process that accepted it, behind a semaphore of `API_MAX_CONCURRENT_RUNS`, which defaults to 1 because of free-tier pacing.
- Immediately before running, a job is acquired by a `queued → running` compare-and-set. Two launchers can never both run one job, for example the accepting process and a restart's recovery.
- The default runtime shares one gateway, with per-provider pacers and no SDK retries, across all jobs in the process.

### Approval

- `POST /v1/analyses/{id}/approval` takes `{approved: bool, reviewer, comment?}`. Booleans are strict, extra fields are forbidden, and there is no decision field.
- The approval is recorded by a `waiting_for_approval → queued` compare-and-set. A second approval gets `409 APPROVAL_ALREADY_SUBMITTED`; one made in any other state gets `INVALID_JOB_TRANSITION` or `APPROVAL_NOT_REQUIRED`.
- The resume calls `resume_analysis` on the job's original `thread_id` with only `{"approved": bool}`. Reviewer identity never enters graph state. The runner restores budgets and trace counters (M4).
- The gate idempotency key is `"<job_id>-gate"`, so the MCP audit log deduplicates even a manual re-resume.
- After the resume, the service checks that the decision and risk score equal the pre-approval result, and fails the job with `DECISION_INVARIANT_VIOLATED` otherwise. Approval gates the write; it can never change the decision.

### Recovery (on startup, one process per database)

- **`running`:** the job is marked failed with `JOB_INTERRUPTED` and **not** re-run, because re-running would repeat provider calls silently. Its checkpoint is left in place.
- **`queued`:** the job is relaunched, because no graph work had started yet. Approved resumes that had not started are relaunched the same way.
- **`waiting_for_approval`:** left untouched. An approval after a restart resumes the original thread.
- **Shutdown:** in-flight tasks are cancelled and their jobs are marked `JOB_INTERRUPTED`.

### Errors and access

- **Error envelope:** every error has the form `{"error": {code, message, details, request_id}}`. The codes live in `ErrorCode`.
- **Detail allowlist:** only an allowlisted set of detail keys can leave the process.
  - Validation errors keep `loc`, `type` and `msg`, and drop the submitted value.
  - Failed jobs carry a fixed message per code.
  - Degraded runs report `DegradationReason` codes such as `provider_unavailable`, `budget_exhausted` and `incomplete_change_data`, never the graph's exception strings. So provider faults and deterministic data gaps stay distinguishable.
- **Debug mode:** the API never enables Starlette debug mode, which would return tracebacks.
- **Access:** without `API_KEY`, `/v1` accepts loopback clients only. With it, every client needs `Authorization: Bearer`.
  - `python -m app.api.serve` binds 127.0.0.1, refuses a public bind without a key, and runs one worker.
  - Bodies are capped at `API_MAX_BODY_BYTES` before routing.

### Streaming boundary (M5-B, contracts only here)

`app/jobs/events.py` defines one envelope, `{job_id, seq, type, at, data}`, for two event families:

- **Lifecycle events** (`job.*`) are emitted in M5-A and persisted with gap-free per-job `seq`. That `seq` is the SSE `id` for `Last-Event-ID` replay.
- **`node.started`, `node.completed` and `agent.token`** are defined but **not emitted**. `EMITTED_EVENT_TYPES` excludes them, and a test pins that.

**How M5-B can produce them:**

- `astream(..., stream_mode=["updates", "messages"], version="v2", durability="sync")` exists in LangGraph 1.2.11.
- An offline probe confirmed that `messages` mode captures tokens from a chat model invoked inside a node even though the gateway does not forward the node's `RunnableConfig` (callbacks propagate through contextvars).
- Interrupts then arrive as an `__interrupt__` update, and the final state is read with `aget_state`.

**Known uncertainties and incompatibilities:**

1. **Tokens are JSON fragments, not prose.** The gateway uses `with_structured_output(include_raw=True)`, so streamed chunks are fragments of structured output: Gemini tool-call arguments, or OpenRouter `json_schema` content. Whether `ChatGoogleGenerativeAI` streams at all inside a structured-output runnable under `messages` mode has not been verified against the live provider.
2. **No subgraphs.** Agents are plain gateway calls inside nodes, not `create_agent` subgraphs, so the plan's `subgraphs=True` note does not apply. Parallel agents share the node `run_parallel_agent` and must be told apart by task id in the event metadata.
3. **The test double cannot stream.** `StaticStructuredLLM` emits no tokens, so offline token tests need a streaming fake chat model behind the gateway.
4. **Token events stay live-only.** They are too voluminous to persist, so a reconnecting client replays lifecycle and node events, not tokens.
5. **One process owns the stream.** The live fan-out is in-process, so an SSE client must reach the process that runs the job (the single-worker assumption again).

## Consequences

### Positive

- Every M4 approval and audit invariant holds over HTTP, each pinned by an ASGI-level test, including concurrent duplicate approvals, a restart between pause and approval, and a failed gate write.
- A client retry cannot buy a second analysis, and a refused request cannot burn a key.
- Lifecycle events are durable and ordered, which is the replay basis M5-B needs.
- No new infrastructure: SQLite next to the existing checkpoint database.

### Negative

- **Not exactly-once execution.** Execution is at-most-once per job:
  - a crash mid-run ends the job as `JOB_INTERRUPTED`, never as a completed job;
  - LLM calls already made in that run are spent;
  - provider calls inside a run are not deduplicated.
- **Single process per job database.** The atomic create and transition hold across processes, but recovery, the run semaphore and future live streaming do not. `serve` enforces one worker. Running several workers against one database is unsupported.
- **No cancel or retry endpoint.** A gated job whose audit write failed (`MCP_ERROR` after approval) keeps its decision but cannot be re-approved in M5-A.
- **The deterministic evaluation suite only checks consistency.** Its fixtures are labelled with the decisions the M1 integration test already enforces, so its 100% decision accuracy is by construction, not a measurement. The graph and retrieval suites need live providers and stay as M4 and M3 scripts until M6.

### Neutral

- Sync SQLite runs in worker threads (`asyncio.to_thread`), which keeps the event loop free and makes the concurrency tests race real connections.
- Job IDs are `job-<uuid4 hex>`. A job's `analysis_id` is its job ID, and its LangGraph thread is `analysis-<job_id>`.

## Alternatives considered

| Alternative | Why not chosen |
|---|---|
| Celery/RQ + Redis | Plan §10: contradicts scale-to-zero. M7 load tests decide whether a queue is needed |
| FastAPI `BackgroundTasks` | Tied to the request lifecycle; no handle for shutdown or recovery, and it cannot gate concurrency |
| Job state inside LangGraph checkpoints only | No idempotency index or event log, and status reads would deserialize graph state that holds chunk text |
| Postgres job store now | Adds a required service to a harness that otherwise runs on SQLite; the store is small enough to port in M8 |
| Re-run interrupted `running` jobs on startup | Silently repeats provider calls; the user-visible failure is the safer default |
| Lease/heartbeat ownership for multi-worker | Real distributed coordination for a single-user local harness; documented as a limit instead |

## References

- [SENTINELFLOW_PLAN.md](../../SENTINELFLOW_PLAN.md) §8 (M5), §10 (rejected: Celery/Redis), §6 (streaming notes)
- `app/jobs/`, `app/persistence/job_store.py`, `app/api/`, `tests/api/`
- ADR-0001 (MCP client adapter)
