# Engineering Journal (M0–M3)

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
