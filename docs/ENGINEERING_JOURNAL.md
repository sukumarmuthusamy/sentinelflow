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

> **Correction (M4 hardening, next entry).** The H001 diagnosis above was incomplete. Checkpoint inspection showed the fixture manifest's synthetic `evidence_coverage: 0.9` reaching the Change Analyst's prompt in both runs, which then emitted a "10% of evidence is uncovered" hypothesis that the verifier contradicted. Fixture labels leaking into agent input was a driver of the flip, not only sampling variance.

**Decision.**

- `runner.restore_counters` seeds the trace and budget from the checkpointed `trace_metrics` before resuming. `tests/graph/test_interrupt_resume.py` now asserts the trace survives resume; it failed (`0 == 5`) before the fix. Run 1's results are kept as `evals/graph/m4_sample_results_run1.json`, with its CHG-E002 trace known-wrong; run 2 (`m4_sample_results.json`) is after the fix.
- Results over both runs (10 trajectories): **0 invalid citations and 0 invented evidence IDs** in every trajectory; 9/10 decisions matched the fixture label (the miss is H001 in run 2); approval paused and resumed on the same thread with exactly one audit-log write. Computed coverage was below the verifier's self-reported number in 7 of 10 trajectories (for example 0.6 against 1.0 on CHG-H002), equal in 2 and above in 1. Each trajectory took 6–8 calls and 15–39k input tokens.
- Temperature sweep on Nemotron, Change Analyst schema, three repeats each: 0.0, 0.2 and 1.0 were all 3/3 schema-valid (`evals/graph/temperature_sweep_nemotron.json`). Schema validity does not discriminate on this model, so the script's tie-break pick of 0.0 carries no information. `AGENT_LLM_TEMPERATURE` stays at **0.2**, the value under which both full samples met the citation criteria. Separating temperatures needs a decision-stability metric over repeated full trajectories, which is M6 work.
- The gate is unchanged. Whether H001's floor-straddling coverage should be handled by more assertions, a calibrated floor, or repeated sampling is an M6 calibration question; widening the floor to make this sample pass would be fitting the policy to one run.
- Not measured: `gemini-3.8-flash`, the planned primary model, which was out of daily quota for the whole session. The default `AGENT_LLM_MODEL` is unchanged; the OpenRouter model is passed per run with `--agent-model`.

---

## M4 hardening: BLOCK could be downgraded, the verifier never read its evidence, and fixture labels reached agents

**What looked wrong.** A review of the M4 branch, done before merging, found three gate-integrity defects. None of them showed up in the ten live trajectories, because those scenarios happened to avoid the code paths involved.

1. CHG-E003 (a high-severity dependency vulnerability, expected BLOCK) returned REVIEW once evidence coverage fell below the floor.
2. The Evidence Verifier's checkpointed notes echoed the Evidence Investigator's labels ("q0 evidence carries a support stance") instead of quoting documents. It also marked hypotheses `supported` while citing chunks that do not mention them.
3. Change Analyst hypotheses quoted numbers that exist only in fixture manifests ("10% lacks coverage" from `evidence_coverage: 0.9`).

**Investigation.**

- *Gate.* `apply_policy_gate` returned on the first matching override in `policy.yaml` order. Both packs list `weak-evidence` and `unsupported-critical-claim` (REVIEW) above `dependency-vuln-ceiling` (BLOCK). Offline, CHG-E003 goes from BLOCK to REVIEW at coverage 0.5, and to REVIEW again with an unsupported critical assertion after repair. A score above `review_max` (for example 86) with weak evidence also returned REVIEW. In the live sample every BLOCK came from `critical-contract-break` or `secret-exposure`, which are listed first, so no committed result was affected.
- *Verifier input.* `evidence_verifier_node` sent the specialists' structured outputs and the list of allowed IDs, never chunk text. The Security Auditor's chunks were not stored at all. `retrieved_chunks` was typed as a single dict with no reducer, so a second parallel writer would have raised a concurrent-update error. Citation validity was 100% only in the sense that the IDs existed.
- *Leakage.* `build_signals_from_fixture` copies three manifest fields into the runtime signals: `evidence_coverage`, `repair_attempted` and `critical_claims_unsupported`. They are synthetic stand-ins that the M1 deterministic tests need. The fields reached the Change Analyst's prompt. They also reached scoring: when no verification metrics existed, the risk engine kept the stub coverage, and `compute_norms` turned the stub `critical_claims_unsupported=True` into an unsupported rate of 1.0. Separately, `get_change_package` returned the manifest `description` and `category`, which are evaluator notes that state the answer ("Critical claim remains unsupported after the single repair attempt").

**Root cause.** Three separate gaps. Gate precedence depended on file order. The verifier was handed interpretations instead of sources. And the fixture manifest serves both as runtime input and as the answer key, with nothing separating the two.

**Decision.**

- **Gate.** Every override is evaluated. The final decision is the strictest of the score decision and all matched outcomes (PASS < REVIEW < BLOCK). Equal-severity ties break by override id, so permuting `policy.yaml` cannot change the result (a test checks all 120 orderings). `override_applied` names the override that set the decision. The new `overrides_matched` field keeps every reason for the audit trail. `policy.yaml` is not reordered; the fix does not depend on order. Degradation and the injection floor use the same `strictest_decision` helper.
- **Verifier grounding.** Chunks from the Investigator, the Security Auditor and repair merge into `retrieved_chunks`, deduplicated by evidence ID and sorted, so branch completion order does not matter. The verifier gets a bounded view: up to 40 chunks, 1,500 characters each, 40,000 in total, with specialist-cited chunks first. It can cite only chunks it was shown. It does not get the Investigator's stance labels. A `supported` judgment survives only if its new `supporting_quote` passes four checks in code (`ground_report`):
  - it is at least 20 characters;
  - it appears verbatim (case and whitespace normalized) in a cited, shown chunk;
  - it shares a salient term with the assertion;
  - its negation polarity matches the assertion's.

  These checks do not prove semantic support. They stop a valid ID, an off-topic excerpt or a contradicting excerpt from counting as support on the model's say-so. Failures go toward REVIEW, the conservative direction, and the false-negative rate is an M6 measurement. With no retrieved evidence, the verifier is not called and every assertion counts as unsupported. Security findings whose citations are all invalid are now kept with no evidence, so they count as unsupported (and, if high severity, as critical), instead of disappearing.
- **Untrusted content.** Prompts are now `v3`. Every system prompt states that tool output and retrieved documents are data, and retrieved text arrives in a field named `untrusted_retrieved_documents`. Retrieved chunks pass a deterministic screen for instruction-like text (`app/graph/injection.py`). Matching chunks are withheld from every prompt and are not citable. Any hit, and any injection report from the verifier, is a REVIEW floor: it can escalate a PASS but cannot decide BLOCK or weaken one. This is a coarse first line of defence; the adversarial suite is M7.
- **Leakage.** At runtime, `deterministic_signal_layer` resets the three verification fields (`runtime_signals`); the graph measures them itself. Agents see signals with those fields removed and a change package restricted to observable fields: ID, source, pack, primary service, changed files, origin nodes. M1 fixture evaluation is unchanged and still uses the stubs; that is what they are for.
- **Reliability, in the same pass.**
  - Degraded runs skip later LLM nodes: after the step and budget guards, at fan-out, after fan-in, and after repair.
  - Concurrent agents are limited by a semaphore instead of raising.
  - Structured-output, MCP and validation failures inside an agent, the verifier or repair degrade to REVIEW without weakening a BLOCK. Provider quota and auth errors still stop the run; they are not analysis outcomes.
  - Missing retrieval degrades instead of silently passing.
  - The risk engine always returns an assessment.
  - `run_tests` receives the allowlisted required test IDs and is always a dry run.
  - `trace_metrics.repair_count` is now populated.
  - CHG-E001's manifest gains its missing `changed_files`.
  - The temperature sweep computes validity over completed responses, so a 503 is no longer counted as a schema failure. Re-scored this way, the earlier proxy sweep favours 0.0 (2/2) over 0.2 (3/3) only by tie-break. `AGENT_LLM_TEMPERATURE` stays at 0.2 as a provisional value until the primary model is measured.
- **Earlier live results** (`evals/graph/m4_sample_results*.json`) are kept unchanged. They were produced before these fixes, with fixture labels leaking into agent prompts and an ungrounded verifier, so they show that the graph runs end to end. They are not evidence of decision quality.

Tests: `tests/unit/test_policy_gate.py`, `tests/unit/test_grounding.py`, `tests/unit/test_evidence_context.py`, `tests/unit/test_temperature_sweep_metrics.py`, `tests/graph/test_evidence_grounding.py`, `tests/graph/test_no_label_leakage.py`, `tests/graph/test_degradation_paths.py`, `tests/graph/test_interrupt_resume.py`, `tests/graph/test_test_strategist_context.py`.

## M4 reliability: parallel branches overshot the call budget, and provider timeouts were never caught

**What looked wrong.** Three problems turned up while preparing a controlled live run.

1. With `ANALYSIS_MAX_LLM_CALLS=2` and four parallel agents, a probe made five LLM calls.
2. A Triage Planner connection or timeout failure would abort the run instead of degrading it.
3. The live-sample script did not record which policy override decided the outcome or why grounding rejected a judgment.

**Investigation.**

- *Budget.* `_generate` called `check_llm_call()`, awaited the provider, then called `record_llm_call()`. Branches suspended inside the provider call had all passed the check, so none had recorded anything yet. In the full offline graph, a fake LLM that holds every call until all four branches are inside it reproduces the overshoot: five calls against a limit of four, and two to five against limits of one to four. Tokens have the same shape, but token usage cannot be known before the response, so a strict token ceiling is not achievable.
- *Retries.* LangGraph's default node `retry_on` retries any exception outside a short builtin list. A 429, a daily-quota error or a rejected API key on an LLM node would therefore re-run the node, making a second provider call against an exhausted quota.
- *Triage.* `triage_planner_node` caught only budget and structured-output errors. A wider problem sits under it: the agent nodes' `ConnectionError`/`TimeoutError` handling never matched real provider failures. `openai.APIConnectionError`, `APITimeoutError`, `httpx.TransportError` and Gemini's `ServerError` all inherit from `Exception`, not from the builtin types, so in a live run a provider timeout in any agent escaped the degradation path. Only test doubles raising the builtin types were caught.

**Decision.**

- **Reserve, then settle.** `AnalysisBudget.reserve_llm_call()` checks capacity and increments an in-flight count with no `await` in between, under a lock. The reservation settles once, as `complete` (with usage) or `fail`. In-flight, completed and failed calls all count against `max_llm_calls`. A failed call is not refunded, because it may have reached the provider; that includes cancellation. A schema-invalid response now carries its token usage on `StructuredOutputError`, so it is counted too. Gateway transient retries (5xx only) stay inside one logical call, which bounds provider attempts at `max_llm_calls × (1 + transient_retries)`. A LangGraph node retry is a new logical call with its own reservation.
- **Token semantics, stated plainly.** Token limits are checked at reservation time against settled usage. Once settled usage reaches a limit, no new call starts. The overshoot is bounded by the usage of calls already in flight, at most `max_parallel_agents`. A test pins this bound.
- **Resume.** `TraceMetrics` gains `llm_calls_failed`, and `restore_counters` restores it and clears the in-flight count. Checkpoints fall between supersteps, so no call is in flight across a resume.
- **Provider failures.** After its own retries, the gateway translates connection, timeout and 5xx failures into `ProviderUnavailableError`, a `ConnectionError` subclass, so every agent node's existing handling degrades the run to REVIEW. Quota, rate-limit and auth errors are never translated. They propagate as operational stops, and the node retry policy now skips them (`retry_node_on`).
- **Triage.** `AGENT_FAILURES` raised by the Triage Planner now degrade the run: the default plan is recorded, no specialist runs, and the deterministic gate decides with a REVIEW floor, so a dependency BLOCK stays BLOCK. A schema-invalid plan still clamps to the default plan and continues. Unexpected exceptions still propagate.
- **Instrumentation.** Each `scripts/run_m4_sample.py` row now records:
  - `risk_score`, `override_applied` and `overrides_matched`;
  - `grounding_rejections`, with reasons;
  - `injection_evidence_ids`, the screened chunk IDs, and the verifier's injection flag;
  - the retrieved chunk count and `approval_status`;
  - `llm_calls_failed`, inside `trace_metrics`.

  When verification did not run, coverage, assertion count and grounding fields are `null`, not zero. The default output is a new file, and the script refuses to overwrite an existing results file, so historical results cannot be replaced by accident.

Tests: `tests/graph/test_budget_concurrency.py` (fails without the reservation), `tests/unit/test_budget.py`, `tests/graph/test_triage_failures.py`, `tests/unit/test_provider_errors.py`, `tests/graph/test_sample_result_summary.py`, `tests/graph/test_interrupt_resume.py`.

## M4 post-hardening live validation: BLOCKs hold, and the corpus cannot support any assertion

This is the first live run on the hardened graph: commit `a6636e6`, prompts v3, source-grounded verifier, no fixture labels in agent input. Earlier live results (`m4_sample_results*.json`) predate all of that and are not comparable as measures of decision quality.

**Setup.**
- Fixtures: four, from `evals/graph/m4_hardened_scenarios.jsonl`, one pass, on 2026-11-23.
- Models: agents on `nvidia/nemotron-3.5-super-vl:free` (OpenRouter), triage on `gemini-3.5-flash-lite`, temperature 0.2.
- Limits: default budgets (25 calls, 200k input and 50k output tokens), 4 parallel agents, 120 s node timeout, 3 transient retries on 5xx only.
- Isolation: separate checkpoint and audit files. Results are in `evals/graph/m4_hardened_sample_results.json`.

**Outcome.**

| Fixture | Expected | Actual | Score | Deciding override | Matched overrides | Coverage | Calls (in/out tokens) |
|---|---|---|---|---|---|---|---|
| CHG-E003 | BLOCK | BLOCK | 42 | `dependency-vuln-ceiling` | dependency-vuln-ceiling, unsupported-critical-claim, weak-evidence | 0.0 | 7 (40,793 / 19,726) |
| CHG-H001 | PASS | REVIEW | 20 | `weak-evidence` | weak-evidence | 0.0 | 6 (38,640 / 12,144) |
| CHG-E001 | PASS | REVIEW | 48 | `weak-evidence` | weak-evidence | 0.0 | 6 (43,938 / 16,676) |
| CHG-E002 | BLOCK | BLOCK | 45 | `critical-contract-break` | critical-contract-break, unsupported-critical-claim, weak-evidence | 0.0 | 8 (40,392 / 15,308) |

**Provider usage and failures.**
- 27 logical LLM calls, 0 failed. HTTP attempts matched exactly: 23 OpenRouter completions and 4 Gemini triage calls, all 200.
- No retries and no 429/503. 20 new query embeddings; 11 queries were cache hits.
- No degradation and no errors.

**Gate integrity.**
- CHG-E003 matched `weak-evidence` and `unsupported-critical-claim` (both REVIEW, listed first in `policy.yaml`) together with `dependency-vuln-ceiling`. The first-match gate would have returned REVIEW; strictest-wins returned BLOCK. This is the downgrade path fixed in the hardening pass, now exercised live.
- CHG-E002:
  - paused at the approval interrupt and resumed on the same checkpoint thread;
  - wrote exactly one audit record, whose `analysis_id` matches the thread;
  - kept its trace across the resume (8 calls, 40,392 input tokens, repair count 1 before and after).
- CHG-E001 was triaged as `documentation` with three agents. It was misclassified as `api_contract` before its `changed_files` were added.
- Citations: no invalid citation survived the filter. One invented ID (`none::none`, CHG-E003) was rejected before it reached the report.
- No chunk was screened as instruction-like and the verifier raised no injection flags.

**No label leakage.**
- No hypothesis repeats a fixture annotation. The pre-hardening CHG-H001 "10% lacks coverage" hypothesis is gone.
- The two numeric probe hits (`0.85`, `0.9`) are the Investigator's own `relevance_score` values, not manifest stubs.

**Finding: evidence coverage is 0.0 everywhere, and that is the correct reading of this corpus.**
- The verifier saw 21–27 retrieved chunks per fixture and marked every material assertion unsupported, with no citations or quotes. Because it judged nothing `supported`, the code-side grounding check had nothing to reject (`grounding_rejections` is empty), and the LLM-reported coverage matches the computed 0.0.
- Reading the chunks confirms the verifier is right. The knowledge corpus is generated from a template (`scripts/bootstrap_knowledge_corpus.py`): every document repeats the same generic paragraphs with its own name substituted ("Test strategists MUST map required tests to services impacted by … modifications"). No retrieved chunk mentions:
  - `amount_cents` (CHG-E002);
  - the GHSA advisory or the vulnerable dependency (CHG-E003);
  - the changed README files (CHG-H001, CHG-E001).

  The contract documents describe a generic `SubmitRequest`, not the fixture contracts.
- The assertions themselves are about tool output: contract diff, blast radius, scanner findings and test inventory. These are facts the corpus does not and cannot restate.
- The Evidence Investigator still labelled most of these chunks `support`. Those are relevance judgements, not support. The verifier no longer sees those labels, and it did not reproduce them.
- The same corpus produced 0.6–1.0 coverage in the pre-hardening runs. That coverage was the verifier echoing the Investigator's stances, not evidence.

**Consequence for the PASS fixtures.**
- CHG-H001 scores 20, which is PASS by score. It becomes REVIEW only through `weak-evidence`.
- CHG-E001 scores 48, which is already REVIEW by score: heuristic blast radius 20, critical service 17.5, and the evidence-confidence penalty 10. Without the penalty it would be 38 (PASS).
  - The blast radius comes from a heuristic `svc-inventory → svc-payment-api` edge attributed to a documentation file, which the Change Analyst itself flagged as unresolved. It is the same pattern as the earlier CHG-H001 blast-radius entry.
- Both are calibration findings, not implementation failures: the gate refused to pass changes it could not ground. Thresholds, labels and the corpus were left unchanged.

**Implications for M6.**
- The corpus needs change-specific content, or the fixtures need assertions the corpus can actually support.
- Tool-derived assertions (contract diff, scanner output, blast radius) should be verifiable against the deterministic signals that produced them, rather than counted as unsupported prose.
- The doc-only blast-radius attribution needs a deterministic fix.
- Until then, a documentation change cannot reach PASS on this corpus, so PASS recall is unmeasured.

## M2 GitHub source: the stub hid a second bypass, and missing scanners read as clean

**What looked wrong.** M2 was marked complete, but `get_change_package(source="github")` raised a `ToolError` stub. The exit criterion ("the same scenario runs identically against a fixture and against a real GitHub PR") had never been exercised.

**Investigation.**

- *Boundary.* Agents read change data through the MCP client. The graph's `deterministic_signal_layer` does not: it reads the fixture directory from disk. For fixtures that is harmless, but a live source has no directory, so replacing the stub alone would not have been enough.
- *Fail-open tools.* The M1 scanner wrappers return an empty `SecurityScanResult` when gitleaks or semgrep is not installed, or when a run fails, and that result cannot be told apart from a clean scan. Fixture mode never hits this because it reads recorded signals. A live PR would have passed a secret change on any machine without gitleaks.
- *oasdiff parsing.* `oasdiff breaking --format json` encodes `level` as an integer (3 = ERR). The parser upper-cased the value and mapped anything it did not recognize to INFO, so a real breaking change would have scored as non-breaking. Fixture mode reads recorded JSON with string levels, so this never showed.

**Root cause.** GitHub mode was deferred until a seed repository existed. In the meantime, the fixture-only path made every live-tool assumption untestable.

**Decision.**

- GitHub PRs are read inside `sentinelflow-context` with GET-only REST calls and normalized into the fixture shape ([ADR-0002](adr/0002-github-change-source.md)). Each pack's `live_source.yaml` names the repository and the path-to-service rules. The runner pins `source` on change-scoped MCP calls, so no agent code changed. For live sources, the deterministic node builds its signals from the same five MCP tools the agents use. A test checks that, for all eight fixtures, this produces exactly the signals of the on-disk path.
- The GitHub path has its own fail-closed wrappers: a missing tool or a non-zero exit returns "unavailable", and the change data records that as a gap. Gaps degrade the run to REVIEW before any LLM call and never weaken a BLOCK. Images are never pulled implicitly. gitleaks output is redacted.
- The oasdiff parser now maps numeric levels. This is a fix to shared M1 code, but fixture mode is unaffected because it reads recorded JSON. The mapping comes from oasdiff's documented output and has not yet been checked against a real oasdiff run on this machine.
- Thresholds, weights and labels were left unchanged.

**Live result (2026-11-23, read-only, no token).** Seed PRs 1, 7, 2 and 8 normalized to the same primary services as CHG-H001, CHG-H002, CHG-E001 and CHG-E002. Every PR ended at REVIEW because neither oasdiff nor gitleaks is installed here. CHG-E001 and seed PR 2 also differ in blast radius: the fixture starts its docs change at `svc-inventory`, while the H001 convention, which GitHub mode follows, starts a non-code file at the file. That fixture inconsistency is recorded for M6 and not changed here.

Tests: `tests/contract/test_github_source.py`, `tests/graph/test_github_change_source.py`, `tests/unit/test_github_signal_tools.py`.

## M2 live run with real scanners: the pipeline holds, two seed scenarios do not trigger the tools

**What looked wrong.** I reran the live smoke test with `tufin/oasdiff` (digest `e6afae33…`, build `059301e`) and `zricethezav/gitleaks:v8.30.1` (digest `c00b6bd0…`). Both ran in disposable containers with `--pull=never`, `--network=none` and a read-only input mount. No gaps were left, but only two of the four equivalent pairs matched their fixtures:

- seed PR 1 matched CHG-H001 (PASS 10);
- seed PR 8 matched CHG-E002 (BLOCK, `critical-contract-break`).

Seed PR 7 and seed PR 10 both reached PASS, while their fixtures (CHG-H002 and CHG-H003) block.

**Investigation.**

- *PR 8.* Real oasdiff output encodes `level` as the integer 3, which confirms the numeric-level fix from the previous entry. Without that fix, this BLOCK would have been scored as non-breaking.
- *PR 7.* oasdiff reports one `request-property-removed` change at level 2 (WARN). It also reports some INFO changes: `api-schema-removed`, `api-version-changed` and `response-non-success-status-removed`. None of them is an ERR, so `critical-contract-break` does not fire. The seed's ground truth says oasdiff should emit ERR, and for this diff it does not.
- *PR 10.* gitleaks' trace log shows the seed's AWS key ending in `EXAMPLE` being skipped twice:
  - the `aws-access-token` rule's allowlist (`.+EXAMPLE$`) drops it;
  - the `generic-api-key` stopword `example` drops it.

  The same container command does detect a randomly generated AWS-format key, and the output is redacted. Seed PRs 11 and 12 fail the same way: the global stopword `abcdefghijklmnopqrstuvwxyz` drops their token and key bodies, because those bodies contain sequential letters.

**Root cause.** The seed scenarios were written with placeholder secrets and a contract change that the real tools classify as non-blocking. The fixtures recorded the intended verdicts rather than tool output. The pipeline is behaving correctly.

**Decision.**

- Nothing was changed to make the pairs match. Thresholds, gitleaks' default config, oasdiff's rules, the fixtures and the seed repository are all as they were.
- Fixing these scenarios belongs in the seed repository: a secret that gitleaks' default rules detect, and a contract change that oasdiff rates ERR. That decision is the owner's, so it is recorded here and not made.

A pre-existing labeling issue also surfaced. `dependency_vuln_at_or_above_ceiling` checks the overall `security.max_severity`. As a result, CHG-H003's critical secret is reported as `dependency-vuln-ceiling` rather than `secret-exposure`. The decision is the same, but the label is misleading. This is left for M6.

Evidence: `evals/github/m2_live_smoke_tools.json`.

## M2 hardening: five fail-open paths in the GitHub source, and a sixth in gitleaks' own config loading

**What looked wrong.** An independent review of `e1da9d0..a19f81f` found five medium-severity gaps, and each one reproduced locally:

- **Renames.** A rename that moved `contracts/svc-core-api.yaml` out of its declared path resolved to `svc-analytics`, with contract diff `not_applicable` and no gaps. The result was PASS.
- **Scanner output.** Truncated or unexpected oasdiff JSON, and a gitleaks report that was not a list, both parsed as clean.
- **Docker.** The images were tag-referenced (oasdiff at `latest`), and the containers ran without resource or privilege limits.
- **Forks.** For a fork PR, head contents were requested from `head.repo.full_name`, with the token attached.
- **Timeouts.** `subprocess.TimeoutExpired` escaped the scanner wrappers, and the container kept running.

**Investigation.**

- *Renames.* Normalization ignored `previous_filename` for service ownership and contract detection. An unsafe previous path was dropped silently, and the base version was then read from the new path.
- *Parsing.* Both parsers treated "not recognised" as "no findings". oasdiff output fell back to a line regex that never matches oasdiff's real text format.
- *Forks.* A probe against a public fork PR showed that the base repository's Contents API serves the fork's head SHA (200, also via `refs/pull/<n>/head`). An unknown SHA returns 404. So reading from the fork was never needed.
- *Timeouts.* The first fix removed the container by name after a timeout. A forced timeout against the real image left a container in the `Created` state: the CLI was killed between create and start, so `rm -f` ran before the container existed. Creating the container synchronously with `docker create`, starting it with `docker start --attach`, and always removing it afterwards closed the race. Four repeated forced timeouts left nothing.
- *gitleaks configuration (found while verifying the fix).* gitleaks v8.30.1 reads `.gitleaks.toml` and `.gitleaksignore` from the scan target and honours inline `gitleaks:allow`. A PR controls all three. Planting each one suppressed a real detection.
- *M1 `run_oasdiff`.* It mounted only the base spec's directory, and both fixtures keep base and revision in sibling directories under the same file name. So it diffed the base against itself and reported a clean result. This only affects `fixture_only=False`, which no default caller uses.

**Root cause.** The live path inherited M1's habit of turning anything it could not interpret into an empty result. The pull request metadata (head repository, previous path) was trusted more than the configuration.

**Decision.**

- *Parsing.* Both parsers are strict. Every scanner failure mode raises `ScannerError` with a fixed reason. That error becomes a gap (REVIEW), and it never weakens a deterministic BLOCK.
- *Repository boundary.* Every request targets the configured repository, and `GitHubApi` enforces this.
- *Renames.* Renamed files count at both paths.
- *Docker.* Images are pinned to their registry OCI index digests, which were verified with `docker buildx imagetools inspect`. A gitleaks binary on PATH is used only at exactly 8.30.1. Containers run under `app/deterministic/docker_sandbox.py`.
- *gitleaks configuration.* gitleaks gets an explicit default config and a trusted ignore path, PR files are staged one level below the target, and inline allow comments are ignored. Its default rule allowlists, such as the one for AWS `EXAMPLE` keys, are unchanged.

Tests: `tests/contract/test_github_hardening.py`, `tests/unit/test_github_signal_tools.py`, and the scanner-failure and rename cases in `tests/graph/test_github_change_source.py`.

## M5-A HTTP boundary: provider errors arrived wrapped, debug mode served tracebacks, and graph state was not safe to return

**What looked wrong.** The first ASGI test of a provider quota stop expected `PROVIDER_RATE_LIMITED` and got `JOB_EXECUTION_FAILED`, logged as `ExceptionGroup`. Writing the rest of the error tests turned up four more ways the HTTP boundary would have misreported or leaked:

- The M0 app passed `settings.debug` to FastAPI.
- A `500` response had no `X-Request-ID` header.
- Domain-pack loader errors carry local paths.
- The graph's `errors` list embeds exception text.

**Investigation.**

- *ExceptionGroup.* `GraphExecutor` runs the graph inside the MCP adapter's `async with`. The adapter's anyio task group wraps any exception that crosses its exit in an `ExceptionGroup`, whose `str()` is "unhandled errors in a TaskGroup". Every classifier in `model_gateway/errors.py` reads `str(exc)` or attributes on the outer object, so a 429 and a rejected key would both have been reported as generic failures. `scripts/run_m4_sample.py` never hit this because its `try` sits *inside* the `async with`.
- *Debug mode.* With `debug=True`, Starlette's `ServerErrorMiddleware` answers an unhandled exception with an HTML traceback before any registered handler runs. A `.env` with `DEBUG=true` would have published tracebacks over the API.
- *Request ID.* That same outermost middleware sends the `500`, so a header added by user middleware never reaches it.
- *Loader details.* `load_domain_pack` puts the pack directory path and the YAML parser's message in `details`.
- *Graph errors.* Agent failures are recorded as `"<agent> failed: <Type>: <message[:200]>"`, and provider messages can include request URLs.
- *Approval payload.* The approval node coerces its payload with `bool(...)`, so a string `"false"` would approve.

**Root cause.** M4's interfaces were written for trusted in-process callers. Exception text, loader details and loose payload coercion were harmless there, and become a leak or a bypass once HTTP clients sit on the other side.

**Decision.**

- *Classifying failures.* `classify_failure` unwraps exception groups to the first leaf before classifying. MCP and change-source types are matched before any status-code check, because an MCP error's `code` is a JSON-RPC code, not HTTP.
- *Error responses.* The API never enables debug mode. Error responses set `X-Request-ID` themselves, and the error envelope passes only allowlisted detail keys. Pack errors are re-raised with only the pack ID.
- *Degradation.* Graph errors become `DegradationReason` codes. Deterministic gap text is kept (truncated, control characters stripped), because the deterministic layer writes it itself.
- *Approval.* The approval body is strict at the API: `StrictBool`, extra fields forbidden, and no decision field. Only `{"approved": bool}` reaches the graph. The M4 node is unchanged.

Also caught by an existing guard: the domain-vocabulary leak test flagged the word "claim", used for "claiming a job", in `app/jobs` and `app/persistence`. The wording is now "acquire" and "reserve".

Tests: `tests/api/test_errors_and_security.py` and `tests/api/test_approval.py`.

## M5-B streaming: observing the run changed the provider calls, and four ways fragments could mislead or leak

**What looked wrong.** While wiring LangGraph's `messages` stream mode into the executor, a reading of `BaseChatModel._should_stream` showed that *observing* a run changes it. Then the token tests and a test-first pass over the sink turned up four more problems:

- a model that does not stream would produce a fake token;
- a credential split across chunks would pass a per-chunk filter;
- a client that disconnects before its first frame would leak its subscription;
- a lagging client that caught up after the job finished never learned it had lost fragments.

**Investigation.**

- *Provider call path.*
  - `messages` mode attaches a `_StreamingCallbackHandler`, and LangChain then calls the provider's streaming endpoint instead of the plain one. For Gemini and OpenRouter that is a different HTTP call from the one M4 validated.
  - `langchain-openai` enables `stream_usage` only for the default OpenAI base URL. With OpenRouter's `base_url`, a streamed response has no usage, so `LLMResult` tokens and the per-analysis token budget would read zero.
- *Fake tokens.* For a model without streaming (`disable_streaming=True`), LangGraph's `messages` handler still emits the finished `AIMessage` from `on_llm_end`. A translator that accepted any message would have reported a whole response as one "token".
- *Split credentials.* The fake model streams 5-9 characters per chunk, so an `AIza…` key spans five chunks, and no per-chunk regex can see it. A greedy pattern applied to a partial key redacts the prefix and lets the tail through on the next chunk.
- *Leaked subscription.* Starlette's `StreamingResponse` cancels its task group on `http.disconnect`. If that happens before the first frame, the async generator never starts, so its `finally` never runs.
- *Lost gap notice.* Recovery after lag re-read durable events from the store and returned as soon as it delivered `job.completed`, before emitting `stream.gap`. The slow-client test caught it: all durable events arrived and no gap was reported.

**Root cause.** Streaming is not a passive tap: it changes which provider API is called, it reports non-streamed output as messages, and it moves data out in pieces, which defeats checks that look at one piece at a time.

**Decision.**

- *Token streaming is opt-in.*
  - `API_STREAM_TOKENS` defaults to false. Node and lifecycle events always stream, and they need no provider change.
  - The OpenRouter client sets `stream_usage=True`, which affects only streamed calls.
  - Live streaming is documented as unverified (ADR-0003). No paid call was made to verify it.
- *Only real chunks.* A fragment is forwarded only if it is an `AIMessageChunk` from a run the gateway tagged as structured output. Thinking blocks are dropped. A `disable_streaming` test pins that a non-streaming model yields zero fragments and that the model really answered.
- *Redaction window.* Redaction runs over a 128-character held-back window per response. A match that touches the end of the buffer is held whole until it can no longer grow, and a private-key block is held until its END line. A randomized-chunking test feeds six credential shapes in 1-12 character pieces and checks that neither the key nor its tail is emitted. The cost: fragments arrive up to 128 characters late and are re-chunked, which the event contract states.
- *Subscription release.* `EventStreamResponse.__call__` closes the subscription in its own `finally`, which runs whether or not the generator started.
- *Gap first.* Recovery emits `stream.gap` before the re-read durable events, which is also where the loss happened.

Tests: `tests/api/test_events_stream.py`, `tests/graph/test_graph_streaming.py`, `tests/unit/test_stream_policy.py`.

## M5 hardening: a fixed redaction window, a cursor from the future, a silent audit miss and usage read as zero

**What looked wrong.** An independent backend review of M5-A/M5-B found four conditions, each reproduced offline before any fix:

- a 241-character JWT fed in 7-character chunks left its first 113 characters in the stream, and a 146-character configured secret leaked its prefix;
- `Last-Event-ID: 999` on a running job held the stream and its subscriber slot open forever;
- an MCP double answering `record_gate_decision` with `recorded=false, duplicate=false` let an approved job end `completed`;
- with no `usage_metadata`, six model calls settled as 0 tokens against a 1,000-token budget, and `input_tokens: "bad"` raised `ValueError` after the provider had answered.

**Investigation.**

- *Redaction.* The M5-B redactor held back the last 128 characters and released everything before them. Any credential longer than the window had a prefix outside it, which no pattern could yet match because the credential was incomplete. The configured-secret list also missed `POSTGRES_PASSWORD`, the password inside `DATABASE_URL` and `SENTINELFLOW_GITHUB_TOKEN`.
- *Cursor.* Replay read events after the cursor, found none, then waited on the queue. Every new event had `seq <= cursor` and was skipped as "already sent", including `job.completed`, so the stream never saw the end. The keepalive safety net re-read events, also after the cursor.
- *Gate write.* The graph node turns an unsuccessful write into a state error and does not set `record_decision_written`. The service settled any run that reached END as `completed`; it checked the decision invariant but not whether the approved write happened.
- *Usage.* `_usage_tokens` returned `(0, 0)` for a missing report and called `int()` on whatever it found.

**Root cause.** Each path trusted an assumption about its input: that a secret fits in a window, that a cursor names a committed event, that a non-raising MCP call succeeded, and that a provider always reports usage.

**Decision.**

- *Redaction without a window.* Text is emitted only once it cannot become part of a secret. The open token run is held until a delimiter closes it, the longest suffix that could start a configured secret is held, credential keywords and `//user:` are held until their value ends, and closed runs are classified whole (known shapes, longer than 200 characters, or machine-like). Raising the window would only move the boundary. Holding is bounded instead: past 1,024 characters (plus the longest configured secret) the held run becomes one `[REDACTED]` and the rest of it is discarded, which fails closed and caps memory. Tests feed synthetic JWTs, provider keys and configured secrets (including ones longer than the cap and ones with quotes) in 1-12-character random chunks and assert that no 8-character piece survives; the M5-B reassembly tests still pass byte for byte. Porting the original M5-B redaction cases to the new API caught one more gap: in `xxxBearer <token>` the keyword hold required a word boundary that the `Bearer` shape does not, so `Bearer ` was released before its token arrived. The holds now include an unanchored `bearer` and are re-applied until the cut stops moving, so streaming is never weaker than redacting the complete text. The cost: harmless machine-like identifiers are redacted, and a short word-like secret of unknown format is still not recognised.
- *Cursor policy.* A cursor above the job's latest committed `seq` is refused with `422 INVALID_REQUEST` (`field`, `latest_event_id`) before subscribing, so no slot is taken. A skipped terminal event and a terminal job seen at a keepalive both end the stream. A 422 was preferred over silently restarting from 0: the client's cursor is wrong, and replaying a different history under it would hide that.
- *Confirmed gate writes.* An approved, recording job completes only if the write was recorded or is an idempotent duplicate; otherwise it fails with `GATE_DECISION_NOT_RECORDED`, keeps its pre-approval result, and the write is not retried.
- *Unknown usage is estimated, never zero.* Missing, malformed, negative, fractional and zero counts are replaced by characters divided by 3, which over-counts. Accounting never raises, so an answered call is not retried. The six-call reproduction now stops at the budget and degrades to review.

Tests: `tests/unit/test_stream_redaction.py`, `tests/unit/test_usage_accounting.py`, new cases in `tests/api/test_events_stream.py` and `tests/api/test_approval.py`.

## M5-C harness: polling a stream without a server, and what a real socket showed

**What looked wrong.** The harness had to follow a live SSE stream from Streamlit, which reruns the whole script on every interaction and has no safe way to update the page from a background thread. Building it and then running it on real sockets turned up four problems:

- the first design could not stop reading a stream at a moment that was safe;
- subscriptions looked leaked in tests;
- the demo ports were already taken on the development machine;
- two pieces of UI copy were misleading.

**Investigation.**

- *Reading model.* A background thread could hold one connection open and catch every fragment. But its lifetime is not tied to a browser session, Streamlit calls from it are unsafe, and a job waiting for approval would keep a subscription per abandoned tab (the server caps them at 8). A fragment with `run_every` that reads for a bounded window and disconnects fits the rerun model. Its first version stopped on the deadline after any line, which can cut a frame between `id:` and `data:`. Durable events would be replayed, but a live fragment would be silently lost mid-frame.
- *Subscriptions.* Against the in-process ASGI client the subscription count was 0 as soon as the read returned. Against uvicorn on a real socket it was still 1: the server learns about a closed TCP connection asynchronously. The release did happen, a few milliseconds later.
- *Ports.* `127.0.0.1:8000` and `:8501` were already published by Docker Desktop and the WSL relay for other containers, and their `/health` answered without SentinelFlow's `service` field. Streamlit's own default address is all interfaces.
- *Copy.* "A completed job is not a PASS" read as a contradiction on a PASS decision. The policy-override metric truncated long override names, and status metrics truncated in a narrow pane.

**Root cause.** Streaming in a rerun-driven UI is polling, so correctness has to come from the stream's resumability (`Last-Event-ID`) and idempotent rendering, not from the connection. Tests that never cross a socket hide the asynchrony of disconnects.

**Decision.**

- Bounded reads stop only between frames and close the connection each time. Durable events are keyed by `seq`. Fragments missed between reads are counted from index gaps and never filled in. A job waiting for approval is not streamed.
- `tests/ui` runs the API under uvicorn on an ephemeral loopback port, so release is checked as "within 5 s", which is what a real client sees.
- `python -m ui.harness` pins 127.0.0.1 and `scripts/serve_offline_demo.py` binds 127.0.0.1. The README tells users to pass `--port` when 8000 or 8501 is taken. Nothing was stopped on the machine; the demo ran on 8765 and 8766.
- The copy now says the job status only means the run finished. The override moved into the gate panel, and status and stage are compact badges.

Tests: `tests/ui/` (HTTP client on a real socket, session model, HTTP-only boundary, Streamlit `AppTest` page tests).

## M5 final review fixes: a loopback check that trusted a hostname prefix

**What looked wrong.** The final review found that the harness would send `Authorization: Bearer <key>` in cleartext to any `http://` host, with only a sidebar warning. Its loopback test also accepted `http://127.evil.com:8000`.

**Investigation.** `is_loopback_url` matched `host.startswith("127.")`, a text test on a name that DNS can point anywhere. The warning was only advisory: the comparison was case-sensitive (`HTTP://` skipped it) and every click and background poll still sent the header. Both reproduced with an injected transport recording the header.

**Root cause.** The safety decision lived in the page as a hint, not in the client as a rule, and it judged a hostname by its spelling instead of parsing an address.

**Decision.**

- Loopback is the exact name `localhost` or a literal address whose `ipaddress` value is loopback. The host comes from `httpx.URL`, the parser that decides where the request goes.
- `SentinelFlowClient` refuses an API key for any URL that is neither `https://` nor loopback (`INSECURE_API_URL`), when built and again in a request hook, before anything is sent. Redirects stay off.
- The page shows that error and stops rendering the workspace, so no client is built and nothing is sent.
- Two small fixes from the same review:
  - the verdict in the HTML decision card is escaped;
  - `latest_event_id` is now on the error-detail allowlist, so a future-cursor 422 carries it as ADR-0003 already documented.

Tests: `tests/ui/test_key_transport.py`; `tests/api/test_events_stream.py::test_future_cursor_on_an_active_job_is_refused`.

## M5 live streaming smoke test: what two providers actually sent

**What looked wrong.** Token streaming had only been tested with fake streaming models. One live run was made to check it against real providers: one synthetic fixture, free-tier routes, retrieval off, and a hard cap on provider requests. It showed the two providers behave differently.

**Investigation.**

- *Gemini (triage).* The request went to `streamGenerateContent`. Its structured JSON arrived in several chunks, all before `node.completed`, each tagged with the right task and agent. The provider reported usage.
- *OpenRouter (agent).* The request streamed and returned 200 at once, but nothing visible arrived for about 20 seconds. The structured answer then came as one burst, which the server cut into fragments at `API_STREAM_FRAGMENT_MAX_CHARS`. The streamed JSON matched the checkpointed result exactly.
- *Usage.* The OpenRouter stream ended with no usage metadata even with `stream_usage=True`, so `account_usage` charged its conservative estimate instead of zero. The estimate is visible only in the server log, not in the trace returned by the API.

**Root cause.** Streaming behaviour depends on the provider and the model. A model that reasons before answering, behind an OpenAI-compatible route, can stream its connection without streaming its answer. Usage reporting on streamed responses also varies by route.

**Decision.**

- Token streaming stays opt-in and off by default, documented as verified incremental on Gemini and burst-only on the OpenRouter model tested.
- The estimate fallback (hardening M4) did its job; nothing was changed in code. Tracing whether the missing usage comes from the route or from the client's aggregation would need more live calls, so it is left as a follow-up, as is reporting estimated usage to API clients.

## M5 CI: two order-dependent tests, and one real stream defect

**What looked wrong.** On the PR's Linux runner, two tests failed that had passed locally every time:

- `test_many_subscribers_share_one_execution`: one of three SSE subscribers had none of the `agent.token` frames the others received.
- `test_full_graph_pr1_equals_fixture_h001`: the GitHub-source run called the specialist schemas in a different order from the fixture run.

While verifying the fix locally, a third test failed once: `test_stream_follows_approval_resume_on_the_original_thread` ended its stream after the resumed `job.running`, with no `job.completed`.

**Investigation.**

- *SSE fragments.* All three clients subscribed while the model was paused, but a client's first store read happens a moment later, when its stream starts. On the faster runner, the static model finished the whole run before one client's first read. That client received the full durable log, and its live fragments were older than the last durable event it had sent, so it skipped them. That is the stream's documented rule: a fragment never precedes the durable event it follows. Forcing the delay locally reproduced it exactly: 0 fragments for the late client, 66 for the others, identical durable history.
- *Graph.* The four specialists run as parallel branches. The order in which their model calls start depends on scheduling and MCP latency, not on the graph. Delaying one branch locally reproduced the reordering.
- *Missing terminal event.* On an idle keepalive tick, the stream re-read the event log and then checked the job status. If the job finished between those two reads, the stream saw a terminal status, ended, and never sent the terminal event. That breaks the rule that durable events are exactly the store's log. A store that finishes the job right after the stream's second log read reproduces it deterministically.

**Root cause.**

- The first two failures were tests comparing things the system does not promise: live-only fragments across clients, and one order among parallel calls.
- The third was a real ordering defect in the keepalive safety net of `JobEventStream`.

**Decision.**

- The keepalive check now reads the status first and the log second, the same order the stream already used when it opened. A status change commits in the same transaction as its events, so a job seen as terminal always has its terminal event in the following read.
- A regression test drives that exact interleaving. It fails without the fix.
- The SSE subscriber test now waits until every client has made its first store read before resuming, and still requires an identical durable history equal to the store's log. Each client's live fragments are then checked on their own: present, no `stream.gap`, placed right after their durable event, attributed to a started node task, provisional, and never duplicated or reordered.
- The graph test compares the calls and their counts regardless of order, keeps triage first and the verifier last, and keeps the prompt-content checks.
