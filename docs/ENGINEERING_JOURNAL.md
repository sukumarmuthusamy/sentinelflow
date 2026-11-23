# Engineering Journal (M0–M4)

Technical notes on problems that looked like bugs, tooling failures, or “too good to be true” metrics — and the judgments that followed. For architecture and roadmap, see [SENTINELFLOW_PLAN.md](../SENTINELFLOW_PLAN.md).

---

## Non-code files inheriting upstream blast radius (CHG-H001)

**What looked wrong.** Fixture `CHG-H001` changes only `analytics_docs/readme.py`, mapped to `svc-analytics`. Blast-radius output still listed upstream services (e.g. `svc-core-api`) as impacted, as if a documentation edit had propagated through the dependency graph like a code change.

**Investigation.** The graph builder correctly skipped import edges for paths under `non_code_paths`, but two other mechanisms still treated non-code files like code: (1) `module_service_map` created `deploys_with` edges from every mapped module to its service, including docs paths; (2) reverse BFS from those modules walked incoming seed edges and pulled in upstream services.

**Root cause.** Non-code paths were excluded from *edge construction* in some places but not from *traversal semantics*. A readme under `analytics_docs/` was still a graph origin that inherited the full upstream service subgraph.

**Decision.** Treat non-code origins as **owner-scoped**, not graph-scoped:

- `link_modules_to_services` skips non-code paths entirely (no synthetic module→service edges).
- `resolve_change_blast_radius` does not run reverse BFS when *all* origins are non-code; it returns origins only.
- `resolve_impacted_services` assigns non-code files to their mapped owner service without upstream propagation.

The general rule: **non-code paths never participate in import-graph reachability; they only assert ownership of the service named in the manifest.** Regression coverage lives in `tests/unit/test_dependency_graph.py::test_non_code_origin_isolated_to_owner_service`.

---

## Eval queries that could not fail (easy batch redesign)

**What looked wrong.** Retrieval eval reported **recall@10 = 1.00** and **MRR = 1.00** on the first labeled query set. That is possible on a strong corpus, but suspicious when every query is a near-verbatim paraphrase of a single chunk’s summary line — the kind of text that hybrid search is designed to match trivially.

**Investigation.** Labels were written from the same template used to generate knowledge docs (`bootstrap_knowledge_corpus.py`). Easy queries often restated the target section’s lead sentence with minimal rewording. Gold evidence was almost always already at rank 1 before any reranking, so the eval measured “can we find the obvious string?” rather than “can we retrieve under realistic phrasing, distractors, or multi-hop intent?”

**Root cause.** **Label leakage into query formulation**, not a retrieval implementation bug (though a separate truncation bug existed — see below). The metric was honest for the questions asked; the questions were not discriminating.

**Decision.** Split eval labels per domain pack into **`{pack}-easy.jsonl`** (original smoke queries) and **`{pack}-hard.jsonl`** (scenario-style queries, distractors, multi-hop wording). The harness reports **easy**, **hard**, and **all** separately. Hard batches are where pgvector vs Qdrant and none vs Voyage actually diverge (e.g. healthcare hard recall@10: pgvector 1.00 vs qdrant 0.95 before reranking). Easy batches remain useful as regression smoke tests, not as the sole claim of retrieval quality.

---

## `retrieve()` silently capped at `rerank_top_n` (5)

**What looked wrong.** Callers passed `k=10`; eval still behaved as if only five chunks mattered. Initial suspicion: embeddings or store backend.

**Investigation.** `RetrievalService.retrieve()` fetched `k` candidates from hybrid search, then passed them to the reranker with `top_n=top_k`. Inside the no-rerank path, results were sliced with `candidates[:top_k]`, but the reranker path returned whatever the reranker emitted — and the **caller’s `k` was not consistently honored end-to-end** because the pipeline conflated retrieval width with `settings.rerank_top_n` (default 5) in an earlier version of the code.

**Root cause.** A **parameter wiring bug**: requested `k` was overridden by the reranker default cap, so recall@10 was computed on at most five visible ranks even when the store returned ten.

**Decision.** Slice and rerank to the caller’s `top_k` explicitly. Re-running eval after the fix still showed 1.00 on the easy set — confirming the eval-design issue above was independent. Hard batches exposed real rank-quality differences once both bugs were addressed.

---

## Local cross-encoder reranking: memory, architecture, and throughput

**What looked wrong.** M3 planned a reranker ablation: none vs local cross-encoder vs Voyage. Local arms never completed on the development machine — first `OSError: paging file too small` for `BAAI/bge-reranker-v2-m3`, then the same for `Qwen/Qwen3-Reranker-0.6B` on CPU.

**Investigation (CPU).** After increasing the Windows paging file, Qwen3-Reranker-0.6B **loaded successfully** on CPU (`LOCAL_RERANKER_DEVICE = "cpu"`). Throughput was measured with a single end-to-end retrieve+rerank pass (`k=10`, healthcare query `hc-001`, pgvector):

| Phase | Time |
|---|---|
| Model load | ~113 s (~1.9 min) |
| One retrieve + rerank pass | **~872 s (~14.5 min)** |

Extrapolated: ~17 hours for the local arm of the full ablation alone (71 queries × two packs), before counting Voyage and store-comparison arms.

**Investigation (GPU).** The machine’s NVIDIA Quadro M1000M (Maxwell, **compute capability sm_50**) was tried next. PyTorch CUDA wheels install and may detect the GPU, but inference fails with **`no kernel image is available for execution on the device`** — current PyTorch CUDA builds target **sm_75+**. There is no supported GPU path on this hardware without an older, unmaintained stack.

**Root cause.** Not a project bug: **environment capacity.** Local cross-encoders are viable in code but impractical on this CPU and unsupported on this GPU generation.

**Decision.** Document local reranking as **CPU-only, optional, and excluded from eval sweeps on constrained hardware** (`--skip-local-reranker`). Operational ablation: **none vs Voyage** (`rerank-2.5-lite`). Measured outcome: recall@10 stayed at 1.00 on all arms; Voyage improved MRR/nDCG on hard batches (e.g. ecommerce hard MRR 0.93 → 0.96). See [README M3 section](../README.md#m3--retrieval) and `evals/retrieval/reports/latest.json` for full numbers.

This is an explicit engineering tradeoff: **hosted reranking for evaluation velocity and reproducibility on dev laptops**, with local cross-encoders retained for environments that have the RAM and CPU/GPU to run them.

---

## M4 temperature sweep: free-tier pacing, SDK retries, and proxy model

**What looked wrong.** `scripts/temperature_sweep.py` crashed mid-run with `ExceptionGroup` after Gemini returned **429 RESOURCE_EXHAUSTED** (5 RPM) and **503 UNAVAILABLE**. A single sweep attempt could trigger many underlying HTTP calls because the Google GenAI client’s default **tenacity** retries stacked on top of 9+ sweep calls.

**Investigation.** Free tier for **`gemini-3.8-flash`** exposes both **5 RPM** and **20 requests/day per model** (`GenerateRequestsPerDayPerProjectPerModel-FreeTier`). The first unpaced run burned the daily cap via retry storms. Pacing alone is insufficient if each “attempt” still performs ~5 SDK retries.

**Root cause.** Missing **proactive spacing** (12.5s between call starts at 5 RPM) and missing **fail-soft** handling at the sweep layer; SDK default retries amplified quota usage.

**Decision.**

- Added `AsyncMinIntervalPacer` (`app/model_gateway/pacing.py`) and **`client_max_retries=0`** for eval scripts so one paced call equals one HTTP attempt.
- Sweep catches quota/transient errors per attempt, records outcome codes, and **stops cleanly** on daily quota exhaustion (remaining arms marked `skipped_quota_exhausted`).
- **Wall-clock with pacing:** at 5 RPM, interval ≈ **12.5s** between starts. **6 calls** (`--repeats 2`, 3 temperatures) ≈ **1.5–2.5 min**; **9 calls** (`--repeats 3`) ≈ **2.5–4 min** plus occasional 503/429 retry waits (65s RPM / 15s transient, at most once per attempt).

**Measured results (2026-09-29):**

| Model | Status | 0.0 | 0.2 | 1.0 | Notes |
|-------|--------|-----|-----|-----|-------|
| `gemini-3.8-flash` | Daily quota exhausted | — | — | — | Re-run after UTC day reset |
| `gemini-3-flash-preview` | Proxy run, 9 calls, 130s | 2/3 valid | 3/3 | 3/3 | One 503 on 0.0 repeat 3 |

**Provisional agent temperature:** **`0.2`** (`AGENT_LLM_TEMPERATURE`) — highest schema-validity rate on the proxy run; **confirm on `gemini-3.8-flash`** when daily quota resets (`evals/graph/temperature_sweep.json`). `gemini-3.5-flash-lite` ignores temperature (fixed sampling); not valid for this experiment.

---

## M4 review: the LLM was scoring its own evidence, and weak evidence lowered risk

**What looked wrong.** A code review of the M4 graph (before any live run) traced how the Evidence Verifier's output reached the gate. Two things contradicted the project's thesis that code, not the model, computes the verdict: (1) `evidence_coverage` and `unsupported_assertion_rate` were read straight from the verifier's own `VerificationReport`, and those numbers drove both the coverage-floor `REVIEW` override and the risk score; (2) more unsupported assertions produced a *lower* risk score.

**Investigation.** For (1), the verifier is an LLM; nothing checked that its reported coverage matched its own per-assertion judgments. A report claiming `evidence_coverage=1.0` with zero assessments sailed past the floor. For (2), `apply_weights` applied `-weight * evidence_confidence_norm`, and `evidence_confidence_norm` *is* the unsupported rate. The sign was inherited from plan section 4's formula (`- 10 * evidence_confidence_norm # penalty: unsupported-claim rate`), which reads as a discount but was fed a penalty input. The `REVIEW` override masked it only when a repair had already happened. All eight M1 fixtures were re-run after the fix to confirm no verdict changed.

**Root cause.** (1) The LLM's summary statistics crossed the deterministic boundary unchecked. (2) A term named "confidence" carried an "unsupported" quantity, and the sign followed the name rather than the value.

**Decision.**

- Coverage, unsupported rate and the critical-unsupported flag are computed in code (`app/graph/verification.py`) from the per-assertion judgments, after invalid evidence IDs are stripped. An assertion counts as supported only if the verifier assessed it by its exact ID as `supported` with at least one surviving citation. Skipped assertions count as unsupported. The verifier's self-reported numbers are kept as `llm_reported_*` for comparison in M6, never as gate inputs.
- Material assertions are the Change Analyst's hypotheses and the Security Auditor's findings, namespaced `hyp:` / `sec:`. With none, coverage is `None` (floor not applied), unless deterministic signals show a contract break or scanner finding, in which case it is `0.0`.
- The penalty term is now added. Agent output can only make the gate stricter, never lower a deterministic score. Plan section 4 is updated to match.
- Known limit: an agent that emits no assertions on a change with clean deterministic signals gets `None` coverage. That is acceptable because the verdict is then fully deterministic, but M6 should measure how often it happens.

Tests: `tests/unit/test_verification_metrics.py`, `tests/unit/test_risk_engine.py::TestEvidencePenaltySign`, `tests/graph/test_gate_integrity.py::test_verifier_self_reported_coverage_is_ignored`.

---

## M4 review: parallel fan-out starved the Evidence Investigator of change context

**What looked wrong.** The Evidence Investigator was meant to retrieve evidence for the Change Analyst's hypotheses, but the hypotheses list was always empty, so every change retrieved the same documents with the same query: the fixed `request.objective` string.

**Investigation.** `Send` fan-out copies graph state at dispatch time. The Change Analyst and Evidence Investigator run in the *same* superstep, so `state["change_impact"]` is always `None` when the investigator starts. Tracing further turned up three related wiring faults: the Security Auditor queried with the change ID (`"... CHG-H001"`), which means nothing to an embedding model; its retrieved evidence IDs never reached `retrieved_evidence_ids`, so the verifier stripped its valid citations; and the repair loop (a) queried with opaque assertion IDs rather than their text and (b) intersected newly retrieved IDs with the *old* set, discarding exactly the new evidence repair exists to find.

**Root cause.** The topology (parallel specialists, plan section 2) and the data dependency (investigator needs hypotheses) disagreed, and the fallback hid it.

**Decision.** Keep the parallel topology and source queries from the deterministic signal layer, which exists before fan-out and is specific to each change: primary service, contract-diff messages, scanner findings, downstream services, prior incidents (`app/agents/queries.py`). The Security Auditor's query uses scanner findings, and its IDs merge into `retrieved_evidence_ids`. Repair queries with the text of unsupported assertions and unions new IDs with old ones. Making the investigator wait for the Change Analyst was rejected: it serializes the slowest two agents and still would not help the Security Auditor. Prompts bumped to `v2` (v1 is kept so the earlier sweep stays reproducible).

Related fixes in the same pass: budget exhaustion inside an agent now degrades to `REVIEW` instead of raising through `RetryPolicy`, and degradation never weakens a deterministic `BLOCK`; token usage is captured from the provider (`include_raw=True`) so the token budget is real; citations stripped by the evidence-ID validator are recorded (`rejected_evidence_ids`) so "zero invented IDs" is measurable; the policy's `category_agent_map` is enforced as a floor on the triage plan.

Tests: `tests/unit/test_evidence_queries.py`, `tests/unit/test_budget.py`, `tests/graph/test_gate_integrity.py`.

---

## M4 live run: 503s spend the daily quota, and 20 requests/day cannot fit the sample

**What looked wrong.** The first live trajectory (2026-11-22, new AI Studio project) failed on `503 UNAVAILABLE` ("model is currently experiencing high demand") from `gemini-3.8-flash`. A second attempt with transient retries saw only 503s, and then the model returned `429` with "requests per day limit exceeded on Free Tier, limit: 20", retry in about 7.5 hours.

**Investigation.** A one-request probe per model showed `gemini-3-flash-preview`, `gemini-3.5-flash` and `gemini-3.5-flash-lite` answering normally while `gemini-3.8-flash` was out of daily quota, after about 20 attempts that were nearly all 503s. So 503 responses count toward the daily cap. Separately, a single trajectory costs 5–7 generation calls (four specialists, verifier, plus one repair and a re-verify when coverage is low). The five-scenario sample needs about 30 calls, more than the 20/day cap even with zero failures. Two code faults surfaced: one 503 in one parallel branch failed the whole graph (LangGraph's node `RetryPolicy` retried after 0.5 s, too soon for an overload), and `is_daily_quota_exhausted` did not recognise the November error wording, so the sample would have kept burning requests after the cap.

**Root cause.** Free-tier capacity, not a project bug. It is made worse by retries that spend quota on an overloaded model.

**Decision.**

- `GeminiStructuredLLM` takes a shared `pacer` (all parallel agents queue behind one 5 RPM limit) and `transient_retries` with growing waits (20 s, 40 s, …) for 503/5xx only. 429s and daily-quota errors fail fast.
- The quota classifier handles both wordings (`tests/unit/test_provider_errors.py`), and `run_m4_sample.py` stops at the first daily-quota error.
- On this tier the primary model cannot carry the full sample in one day. Live validation ran one trajectory on `gemini-3-flash-preview` as a stand-in for the agents: it completed in 98 s with 7 calls, 0 invented evidence IDs and 0 invalid citations. The verifier *contradicted* the Change Analyst's "zero functional risk" hypothesis for the docs-only change CHG-H001, citing three policy and ownership chunks. Computed coverage was 0.0 against a self-reported 1.0, so the gate returned REVIEW where the fixture label says PASS. That is a real disagreement between agents, which the self-reported number would have hidden. Whether the label or the policy corpus is right is an M6 calibration question.
- The same run showed the Triage Planner classifying that change as `unknown`. It only saw the change ID and service names. It now gets changed file paths and a deterministic-signal summary, but never the fixture's `category`, `description` or `expected_decision`, which are ground truth for plan-selection accuracy (`tests/unit/test_triage_input.py`).

---

## M4 live sample on OpenRouter: resume zeroed the trace, and one scenario sits on the evidence floor

**What looked wrong.** With Gemini's free tier unable to carry the sample, the agents moved to an OpenRouter free model and the first full five-scenario run (2026-11-22) passed cleanly, except that CHG-E002, the one scenario that pauses for approval and resumes, reported `llm_calls: 0` and zero tokens after a full analysis. A second run then returned REVIEW for CHG-H001, which the first run had passed.

**Investigation.**

- *Model choice.* OpenRouter lists five free models with strict `structured_outputs`; the 1.7B one was skipped as too small for the agent prompts. One probe call each to the other four with a small cited-evidence schema: `nvidia/nemotron-3.5-super-vl:free` (4.6 s, cited only the two IDs offered) and `baidu/ernie-5.5-flash-preview:free` (5.0 s, same) passed; `inclusionai/ling-3.2-flash:free` returned a sentence in the evidence-ID field; `particle-ai/particle-1:free` was rate-limited (429). Nemotron became the agent model; the Triage Planner stayed on `gemini-3.5-flash-lite`. The gateway routes by model ID: any `vendor/model` slug goes to OpenRouter, each provider behind its own pacer.
- *Zeroed trace.* `resume_analysis` rebuilds `AnalysisGraphContext`, and with it a fresh `TraceAccumulator` and `AnalysisBudget`. The post-interrupt node (`record_gate_decision`) emits `_trace_update(ctx)` from that empty accumulator, overwriting the checkpointed metrics. The budget had the same fault: a resumed run would restart its call and token counters, so the cap could in principle be exceeded across an interrupt.
- *CHG-H001 flip.* The domain pack's `evidence_floor` is 0.75. Run 1 computed coverage 0.75 (3 of 4 assertions supported, not below the floor) → PASS; run 2 computed 0.6 (3 of 5) → REVIEW via `weak-evidence`. Same model, same temperature (0.2). With four or five material assertions, one verifier judgment moves coverage by 0.2–0.25.

**Root cause.** The resume path restored graph state but not the runtime counters that live outside it. The H001 flip is not a bug: it is sampling variance on a change whose coverage sits exactly at the policy floor, made coarse by small assertion counts.

**Decision.**

- `runner.restore_counters` seeds the trace and budget from the checkpointed `trace_metrics` before resuming. `tests/graph/test_interrupt_resume.py` now asserts the trace survives resume; it failed (`0 == 5`) before the fix. Run 1's results are kept as `evals/graph/m4_sample_results_run1.json`, with its CHG-E002 trace known-wrong; run 2 (`m4_sample_results.json`) is after the fix.
- Results over both runs (10 trajectories): **0 invalid citations and 0 invented evidence IDs** in every trajectory; 9/10 decisions matched the fixture label (the miss is H001 in run 2); approval paused and resumed on the same thread with exactly one audit-log write. Computed coverage was below the verifier's self-reported number in 7 of 10 trajectories (for example 0.6 against 1.0 on CHG-H002), equal in 2 and above in 1. Each trajectory took 6–8 calls and 15–39k input tokens.
- Temperature sweep on Nemotron, Change Analyst schema, three repeats each: 0.0, 0.2 and 1.0 were all 3/3 schema-valid (`evals/graph/temperature_sweep_nemotron.json`). Schema validity does not discriminate on this model, so the script's tie-break pick of 0.0 carries no information. `AGENT_LLM_TEMPERATURE` stays at **0.2**, the value under which both full samples met the citation criteria. Separating temperatures needs a decision-stability metric over repeated full trajectories, which is M6 work.
- The gate is unchanged. Whether H001's floor-straddling coverage should be handled by more assertions, a calibrated floor, or repeated sampling is an M6 calibration question; widening the floor to make this sample pass would be fitting the policy to one run.
- Not measured: `gemini-3.8-flash`, the planned primary model, which was out of daily quota for the whole session. The default `AGENT_LLM_MODEL` is unchanged; the OpenRouter model is passed per run with `--agent-model`.
