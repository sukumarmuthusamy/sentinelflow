# ADR-0003: HTTP job lifecycle and progress streaming for the local harness (M5-A, M5-B)

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

Routes never touch the graph or the store. M5-B added streaming at two seams, leaving the other routes unchanged: `GraphExecutor` observes the run through `astream`, and the store hands every committed event to an in-process bus that feeds the SSE route (see Streaming).

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
- **Token accounting.** The per-analysis token budgets are charged from each call's reported usage. A report that is missing, malformed, negative, fractional or zero is treated as unknown, never as zero: that count is replaced by a conservative estimate (prompt or response characters divided by 3, at least 1), and a warning without values is logged. Accounting never raises, so a call the provider has already answered and billed is never failed or retried because of its usage report. Valid reports are used as reported.

### Approval

- `POST /v1/analyses/{id}/approval` takes `{approved: bool, reviewer, comment?}`. Booleans are strict, extra fields are forbidden, and there is no decision field.
- The approval is recorded by a `waiting_for_approval → queued` compare-and-set. A second approval gets `409 APPROVAL_ALREADY_SUBMITTED`; one made in any other state gets `INVALID_JOB_TRANSITION` or `APPROVAL_NOT_REQUIRED`.
- The resume calls `resume_analysis` on the job's original `thread_id` with only `{"approved": bool}`. Reviewer identity never enters graph state. The runner restores budgets and trace counters (M4).
- The gate idempotency key is `"<job_id>-gate"`, so the MCP audit log deduplicates even a manual re-resume.
- After the resume, the service checks that the decision and risk score equal the pre-approval result, and fails the job with `DECISION_INVARIANT_VIOLATED` otherwise. Approval gates the write; it can never change the decision.
- An approved job completes only if the gate write is **confirmed**: `recorded`, or a `duplicate` of the same idempotency key. The MCP server can answer `recorded=false, duplicate=false` without raising; the job then fails with `GATE_DECISION_NOT_RECORDED`, keeps its stored pre-approval result (decision and risk score unchanged, `gate_decision_recorded: false`), and the write is not retried, because its outcome is ambiguous. A rejection never writes, so it cannot fail this way.

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

### Streaming (M5-B)

`GET /v1/analyses/{job_id}/events` is a Server-Sent Events stream (`text/event-stream`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`). It sits behind the same access guard as the other `/v1` routes. An unknown job id, and an evaluation job id, get `404 JOB_NOT_FOUND` before any stream starts.

**How the graph is observed.** Modules: `app/graph/streaming.py`, `app/jobs/streaming.py`.

- `GraphExecutor` passes an event sink to `run_analysis` / `resume_analysis`. They then call `stream_graph`, which runs the compiled graph **once** with `astream(stream_mode=["values", "tasks"] (+ "messages"), durability="sync", version="v2")`. It returns what `ainvoke` returns: the latest values, plus `__interrupt__`. The thread id, checkpointer, `Command(resume=…)` path, budget restoration and gate idempotency key are unchanged. Without a sink, the M4 `ainvoke` path runs unchanged.
- Behaviour verified against langgraph 1.2.11:
  - `tasks` mode yields one start part and one result part per node task. `Send` branches of `run_parallel_agent` get distinct task ids. Node retries are invisible. A resumed interrupt re-runs its task with the same task id.
  - `messages` mode yields `(chunk, metadata)` for model calls inside nodes, because callbacks propagate through context variables. `langgraph_checkpoint_ns` is `<node>:<task id>`.
- **Settling.** After the run, the executor reads the thread's stored checkpoint (`read_checkpoint`) and settles the job from that, not from the in-memory return value.
  - It is `completed` only if the checkpoint has no pending node and no interrupt.
  - It is `waiting_for_approval` only if an interrupt is pending.
  - Anything else fails with `GRAPH_INCOMPLETE`.

**Events.**

| Type | Class | Payload (never state values) |
|---|---|---|
| `job.*` | durable | as M5-A |
| `node.started` / `node.completed` | durable | `node`, `task_id`, `agent`, `run` (1 = first run, 2 = resume). `node.completed` adds `status` (`ok`/`error`/`interrupted`), the **names** of written state keys, and `fragments_streamed` / `fragments_dropped` |
| `agent.token` | live | `node`, `task_id`, `agent`, `run`, `index`, `response`, `schema_name`, `channel` (`content` / `tool_call_arguments`), `format: "structured_json"`, `provisional: true`, `verified: false`, `text` |
| `stream.gap` | live | `reason: "slow_client"`, `dropped_live_events` |

Node inputs, errors and interrupt payloads are dropped in translation, so nothing a node read or raised reaches the stream.

**Ordering, persistence and replay.**

- **Durable events** share the job's gap-free `seq` with lifecycle events. They are written only while the job is `running` (`append_events` checks the status in the same transaction), so nothing can follow a run's `job.failed`. The SSE `id:` is `seq`.
- **Live events have no `id:` line**, so they never move the browser's `Last-Event-ID`, and there are no live ids to collide or leave gaps. Instead each carries `after_seq`, the durable event it follows, and `(run, index)`, which is gap-free per run as emitted.
- **Connecting:** subscribe first, then replay the store's durable events after `Last-Event-ID` (an integer, else `422`).
  - A cursor **ahead** of the job's latest committed event gets `422 INVALID_REQUEST` with `details: {field: "Last-Event-ID", latest_event_id}` before any stream starts or subscription is taken. Such an id belongs to another job or database; following it would wait for events that may never be written. Event ids only grow, so a cursor valid when checked stays valid.
  - Durable events at or below the last `seq` sent are skipped.
  - A jump in `seq` is filled from the store.
  - A live event whose `after_seq` is older than a durable event already sent is not delivered, so a fragment never appears after its node's `node.completed`.
- **End of stream:** the stream ends after `job.completed` or `job.failed`, and immediately for a terminal job whose terminal event the client already has. It stays open through `waiting_for_approval`.
  - A stream never outlives its job: a terminal event arriving at or below the cursor still ends it, and each keepalive re-reads the job and ends the stream if it is terminal. Every end releases the subscription.
- **Keepalive:** a `: keepalive` comment every `API_SSE_KEEPALIVE_SECONDS`. Each one also re-reads the store as a safety net. There is no busy-waiting: the stream blocks on its queue with a timeout.
- **Fan-out:** the store calls a listener after each commit. The bus publishes through `call_soon_threadsafe`, which is queued before the store call's own result, so a writer's events are published before it resumes.

**Slow and disconnected clients.**

- Each subscriber has a bounded queue (`API_SSE_QUEUE_SIZE`), and publishing is `put_nowait`. The graph never awaits a client.
- A full queue marks the subscriber **lagged**. It then drops new items until it has drained. It receives `stream.gap` with the number of live events it lost, then re-reads missed durable events from the store.
- At most `API_SSE_MAX_SUBSCRIBERS_PER_JOB` streams per job; beyond that, `429 TOO_MANY_SUBSCRIBERS`.
- Disconnecting cancels only that response, and the subscription is released even if the client leaves before the first frame. Subscribers only read: no number of them can start, resume, retry or cancel a job, and the run executes once.
- Errors while recording progress are logged by type and dropped. They cannot fail the run, alter the decision or touch the audit write (pinned by a test that breaks `append_events`).

**Token policy.**

- **Real fragments only.** A fragment is forwarded only when (a) it is an `AIMessageChunk`, i.e. really streamed, and (b) its run carries the gateway's metadata tag, which the gateway sets on every structured-output call along with the schema name.
  - For a non-streaming model, LangGraph emits the finished `AIMessage`. It is dropped, not replayed as tokens.
  - Thinking/reasoning content blocks are dropped; only `text` blocks and tool-call argument fragments pass.
- **Structured, not prose.** Every fragment is a piece of schema-shaped JSON (`format: "structured_json"`), labelled `provisional` and `verified: false`. The decision endpoint is authoritative.
- **Sanitized before leaving the process:**
  - control and format characters are stripped (bidi overrides, zero-width);
  - credential shapes are redacted: Google, OpenAI/OpenRouter, GitHub, Slack, AWS key ids, Bearer tokens, JWTs and private-key blocks;
  - the exact values of configured secrets are redacted.

  Configured secrets are the provider and service keys, `API_KEY`, `POSTGRES_PASSWORD`, the password inside `DATABASE_URL` and `SENTINELFLOW_GITHUB_TOKEN`; values shorter than 8 characters and the public development default are not matched, and no value is logged.

  Redaction is **streaming-aware and fail-closed** (`app/jobs/redaction.py`), with one state per response. It does not use a fixed window: it emits text only once it can no longer become part of a secret.
  - The open token run at the end of the buffer (letters, digits, `_-+/=.~%`) is held until a delimiter closes it. A closed run is redacted if it matches a known shape, is longer than 200 characters, or looks machine-generated (32+ characters of mixed-case alphanumerics with digits, or long hex).
  - The longest buffer suffix that is a prefix of a configured secret (raw or JSON-escaped) is held, so a configured value is never emitted in part, even one containing spaces or quotes.
  - Credential keywords (`password`, `api_key`, `bearer`, ...) are held with their separator until their value is complete; `//user:` is held until the URL authority ends; a private-key block is held until its END line.
  - **Bounded:** a held run longer than 1,024 characters (plus the longest configured secret) or an open block longer than 8,192 is replaced by one `[REDACTED]`, and the rest of it is discarded as it arrives instead of being released. An unterminated block at the end of a response is never shown.

  Safe text still streams incrementally. Fragments are re-chunked: delayed until the run they end in closes, and at most `API_STREAM_FRAGMENT_MAX_CHARS` each.
- **Bounded:** at most `API_STREAM_MAX_FRAGMENTS_PER_RUN` per run. Further fragments are counted in `fragments_dropped`.
- **Opt-in:** off unless `API_STREAM_TOKENS=true`. `messages` mode attaches a streaming callback handler, and LangChain then calls the provider's *streaming* API (`BaseChatModel._should_stream`). That changes the HTTP call live providers receive.
  - `langchain-openai` omits usage from streams when `base_url` is custom, which would zero OpenRouter token budgets. The OpenRouter client therefore sets `stream_usage=True`, which affects only streamed calls.
  - Neither Gemini nor OpenRouter structured-output streaming has been run live. Node and lifecycle events stream regardless of this setting.

**Not covered.**

- Token events cannot be replayed: a client that connects late sees the remaining fragments only.
- The live fan-out is per process, so an SSE client must reach the process running the job (one worker, as before).
- Redaction is shape-based. Beyond configured values and known formats it redacts long or machine-like runs, but a short credential with an unknown, word-like format is not recognised. Machine-like identifiers (long hashes, long mixed-case ids) are redacted even when harmless.

## Consequences

### Positive

- Every M4 approval and audit invariant holds over HTTP, each pinned by an ASGI-level test, including concurrent duplicate approvals, a restart between pause and approval, and a failed gate write.
- A client retry cannot buy a second analysis, and a refused request cannot burn a key.
- Lifecycle and node events are durable and ordered, so a reconnecting client resumes from `Last-Event-ID` without loss or duplicates. A slow client is told what it lost instead of stalling the graph.
- No new infrastructure: SQLite next to the existing checkpoint database.

### Negative

- **Not exactly-once execution.** Execution is at-most-once per job:
  - a crash mid-run ends the job as `JOB_INTERRUPTED`, never as a completed job;
  - LLM calls already made in that run are spent;
  - provider calls inside a run are not deduplicated.
- **Single process per job database.** The atomic create and transition hold across processes, but recovery, the run semaphore and the live event fan-out do not. `serve` enforces one worker. Running several workers against one database is unsupported.
- **No cancel or retry endpoint.** A gated job whose audit write failed (`MCP_ERROR` or `GATE_DECISION_NOT_RECORDED` after approval) keeps its decision but cannot be re-approved in M5.
- **Estimated usage over-counts.** When a provider omits usage, the estimate is deliberately high, so the budget can stop an analysis earlier than its real token use would.
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
| Persist token events | Hundreds per run; replaying stale provisional JSON adds little. Node counts record what was streamed |
| SSE ids for token events | Every reconnect would need token replay or would see id gaps; `after_seq` orders them without touching `Last-Event-ID` |
| Block the graph on slow clients (unbounded queues) | One stalled browser tab would hold a paid analysis open or grow memory without bound |
| `astream_events` (callbacks v2) | Lower-level and noisier: needs manual filtering of chain events. `tasks` + `messages` give node boundaries and chunks directly |

## References

- [SENTINELFLOW_PLAN.md](../../SENTINELFLOW_PLAN.md) §8 (M5), §10 (rejected: Celery/Redis), §6 (streaming notes)
- `app/jobs/`, `app/graph/streaming.py`, `app/persistence/job_store.py`, `app/api/`, `tests/api/`, `tests/graph/test_graph_streaming.py`
- ADR-0001 (MCP client adapter)
