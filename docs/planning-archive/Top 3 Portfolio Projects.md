## Top 3 Portfolio Projects

All three are LLM-backed, use FastAPI, and are designed to be interview-defensible under follow-up questions about scale — not demo-ware. Ranking and build order are at the end of this section.

---

### Project 1: DocuMind — Agentic RAG Knowledge Assistant with MCP Tool Layer

**1. One-line pitch:** A production-grade "ask questions over your organization's documents and have an agent take action on the answers" assistant — the single most common shape of GenAI job in this entire dataset (56.6% of all JDs).

**2. Problem it solves / who'd use it:** Knowledge workers waste hours searching scattered documentation (policies, tickets, runbooks, product docs) and then manually acting on what they find (filing a ticket, scheduling a follow-up, updating a record). DocuMind answers grounded questions over a private corpus *and* can take a small set of real actions via tool calls — mirroring what GitHub (Trust & Safety agents), Guidehouse (RCM workflow agents), ClearpointCo (agent sandbox), and Randstad's "enterprise search + AI agents" posting are all independently describing.

**3. Why a FAANG interviewer would find this compelling:** It's not "a chatbot on my docs" — it demonstrates retrieval-quality engineering (chunking strategy, re-ranking, source attribution), agent reliability engineering (tool-call error handling, guardrails against hallucinated actions), and observability (this directly showcases the #3 most-universal skill in the whole dataset, Monitoring/Observability at 63.2%). A FAANG interviewer reads this as "this person understands that the hard part of RAG isn't the LLM call, it's everything around it" — which is exactly the system-design maturity signal (42.1% overall, 50% FAANG) the data says they're screening for.

**4. Architecture:**
- **Ingestion pipeline:** Documents (PDF/Markdown/HTML) → chunking (semantic, not fixed-size) → embedding (open-source sentence-transformer model, free) → stored in a vector DB.
- **Retrieval layer:** Hybrid search (vector similarity + keyword/BM25) → re-ranking → top-k context assembly with source citations.
- **RAG generation:** Context + query → LLM (see stack below) → grounded answer with inline citations.
- **Agent/tool layer (MCP):** An MCP server exposes 2–3 tools (e.g., "create_ticket," "schedule_followup," "update_record" — can be mocked against a local SQLite "CRM" for the portfolio version). The agent decides, based on the RAG answer, whether a tool call is warranted, and executes it through the MCP tool-calling interface rather than hardcoded function calls — this is precisely the plausible-fit MCP use case the brief calls for.
- **API layer:** FastAPI exposes `/query` (RAG + optional tool execution) and `/ingest` (add documents) endpoints, async throughout.
- **Observability layer:** Every query logged with retrieval scores, latency breakdown (retrieval vs. generation vs. tool-call time), and a lightweight eval harness that flags low-confidence or ungrounded answers.

**5. Tech stack (mapped to matrix skills):**
| Component | Choice | Matrix skill it demonstrates |
|---|---|---|
| LLM | Gemini free-tier API (best fit: generous free quota, strong function-calling support) | LLMs (68.4%), Prompt Engineering (26.3%) |
| Embeddings | Open-source (e.g., a sentence-transformers model, local/free) | Embeddings (23.7%) |
| Vector DB | Free-tier hosted (e.g., a managed vector DB free tier) or local (Chroma/FAISS) for the demo | Vector Databases (23.7%) |
| Agent orchestration | LangGraph | AI Agents/Agentic Workflows (51.3%), LangChain/LlamaIndex ecosystem (23.7%) |
| Tool layer | MCP server | Model Context Protocol (5.3%, high-signal emerging skill) |
| Backend | FastAPI, async endpoints | REST/APIs (48.7%), FastAPI (11.8% explicit, required by brief) |
| Containerization | Docker | Docker (38.2%) |
| Observability | Structured logging + a simple metrics dashboard | Monitoring/Observability (63.2%, the #3 overall skill) |

**6. Scalability plan (concrete, not buzzwords):**
- **Async/concurrency:** All I/O (LLM calls, vector search, tool calls) is `async`/non-blocking in FastAPI; use a connection pool for the vector DB and a bounded semaphore to cap concurrent LLM calls so free-tier rate limits don't cascade into failures.
- **Caching:** Cache embeddings for unchanged documents (avoid re-embedding on every ingest); cache identical/near-identical query results for a short TTL (e.g., Redis, free-tier) since knowledge-base questions cluster heavily around a small set of FAQs in practice.
- **Horizontal scalability:** Stateless FastAPI containers behind a load balancer; the vector DB and any session state live outside the app container, so you can run N replicas without sticky sessions.
- **Observability/logging:** Structured (JSON) logs with trace IDs threading through retrieval → generation → tool-call, so a single slow or wrong answer can be root-caused end-to-end — this is the specific "observability at scale" muscle DoorDash/Amazon/Capital One JDs are testing for.
- **Cost control:** Token-count budgets per request, a cheap/fast model for query classification ("does this even need retrieval?") before invoking the more expensive generation call, and a hard cap on tool-call chains to prevent runaway agent loops.
- **Under real traffic:** The bottleneck will be the LLM API rate limit, not your code — the design should make that limit visible (queue + backpressure + graceful degradation to "try again shortly") rather than silently failing.

**7. Deployment plan (specific free-tier path):** FastAPI app containerized with Docker → deployed to **Google Cloud Run** (GCP's $300 credit covers this comfortably; Cloud Run's scale-to-zero is ideal for a portfolio project with bursty traffic) → vector DB on a free-tier hosted instance or run in-container for the demo → simple frontend (a minimal React or plain HTML page) on **Vercel or Netlify's free tier**. Hugging Face Spaces is a solid lightweight alternative if you want a single-URL demo instead of separate frontend/backend deploys.

**8. Stretch goals:** Add a second retrieval mode (GraphRAG over a small knowledge graph, tying into the "Knowledge Graphs/GraphRAG" trend at 5.3%); add a self-evaluation loop where the agent critiques its own answer before returning it (LLM-as-judge pattern, 17.1% overall); add multi-document-type ingestion (PDF tables, not just text).

**9. Estimated build time:** 2–3 weeks for a solid v1 (ingestion + RAG + basic agent + FastAPI + Docker); +1 week for the MCP tool layer and observability polish. **~3–4 weeks total.**

**10. Resume/LinkedIn bullets:**
- "Built and deployed an agentic RAG assistant (LangGraph + MCP tool integration) achieving grounded, source-cited answers over a 500+ document corpus, with async FastAPI serving and end-to-end request tracing."
- "Designed a retrieval + tool-calling pipeline with hybrid search, re-ranking, and cost-aware model routing, deployed on Cloud Run with horizontal autoscaling and Redis-backed response caching."

---

### Project 2: SentinelML — Real-Time Anomaly Detection Service with an Agentic Investigation Copilot

**1. One-line pitch:** A hybrid system that pairs a classical ML model doing real-time fraud/anomaly scoring with an LLM agent that investigates flagged cases and writes a human-readable explanation — the "probabilistic + LLM" pattern that OptumInsight, Mutual of Omaha, MeeBoss, and Shields Group Search are independently hiring for.

**2. Problem it solves / who'd use it:** Pure classical-ML fraud/anomaly systems flag transactions but leave a human analyst to manually dig through logs to understand *why*. Pure-LLM systems are too slow/expensive to score every transaction and too unreliable to trust for the scoring decision itself. SentinelML uses classical ML for what it's good at (fast, cheap, calibrated real-time scoring) and an LLM agent for what it's good at (synthesizing evidence into a coherent investigation narrative, referencing policy/RAG context) — an analyst or ops team would use this to triage flagged events faster.

**3. Why a FAANG interviewer would find this compelling:** This is the project that proves you're not "an LLM-wrapper engineer." A huge fraction of this dataset's highest-paying, most senior postings (OptumInsight $145.5K–$249.5K, Equifax staff-level, MeeBoss $175–220K) explicitly want engineers who can do *both* rigorous classical ML (feature engineering, drift detection, precision/recall tradeoffs) *and* GenAI — not either/or. Demonstrating you can own that full hybrid stack, including the harder-to-fake parts (model evaluation discipline, drift monitoring), is a direct signal against the system-design and MLOps maturity bar (Data Structures & Algorithms Δ+31.2, System Design 50% FAANG) that separates senior candidates from LLM-tutorial graduates.

**4. Architecture:**
- **Feature pipeline:** Synthetic transaction-stream generator (for portfolio purposes) → feature engineering (rolling aggregates, velocity checks, categorical encodings) → feature store (simple, e.g. a Postgres table with versioned feature definitions).
- **Scoring model:** A gradient-boosted model (XGBoost) trained offline on a public fraud dataset (e.g., a Kaggle credit-card-fraud dataset), served via FastAPI for real-time inference; instrumented with drift detection comparing live feature distributions to training distributions.
- **RAG layer:** Fraud/compliance policy documents and historical case notes are embedded and retrievable — this is where RAG earns its place (grounding the agent's explanation in actual policy rather than a hallucinated one).
- **Agent layer:** On a flagged transaction, an agent gathers (a) the model's score + top contributing features (via SHAP-style explainability), (b) relevant policy context (RAG), and (c) similar historical cases, then produces a structured investigation summary and a recommended action (approve/hold/escalate) — an MCP tool exposes "lookup_case_history" and "check_policy" as callable tools rather than hardcoded retrieval, so the agent itself decides what evidence it needs.
- **API layer:** FastAPI `/score` (fast path, ML only, <100ms target) and `/investigate` (slower path, ML + RAG + agent, for flagged cases only) — this separation is itself a scalability decision worth calling out explicitly in an interview.

**5. Tech stack (mapped to matrix skills):**
| Component | Choice | Matrix skill it demonstrates |
|---|---|---|
| Classical ML | XGBoost + scikit-learn | XGBoost (3.9% but Valuable-add), scikit-learn (25.0%), Feature Engineering (15.8%), Model Evaluation (18.4%) |
| Drift monitoring | Statistical distribution comparison, logged and alerted | Drift Detection (9.2%), Monitoring/Observability (63.2%) |
| LLM | OpenRouter free-tier model (best fit: lets you A/B different open models cheaply for the explanation-generation task without lock-in) | LLMs (68.4%) |
| RAG | Embeddings + vector DB over policy docs | RAG (36.8%), Embeddings/Vector DBs (23.7% each) |
| Agent + tools | LangGraph agent with an MCP tool server | Agents (51.3%), MCP (5.3%), Tool Use/Function Calling (13.2%) |
| Backend | FastAPI, two-tier endpoints | REST/APIs (48.7%) |
| Experiment tracking | MLflow (model versions, offline eval metrics) | MLflow (11.8%), Model Registry/Versioning (13.2%) |

**6. Scalability plan:**
- **Async/concurrency:** The fast-path `/score` endpoint is synchronous-and-cheap by design (no LLM in the hot path); the slow-path `/investigate` is fully async and queued, so agent/LLM latency never blocks real-time scoring — this separation *is* the scalability answer, and it's worth stating explicitly.
- **Caching:** Cache RAG retrieval results for repeated policy lookups; cache SHAP explanations per model version+feature-signature so repeat-pattern cases don't re-compute explainability from scratch.
- **Horizontal scalability:** The scoring service is stateless and trivially replicable; the investigation queue (e.g., a simple task queue) lets you scale investigation workers independently from scoring throughput — this mirrors exactly how a real fraud system would be built, and shows you understand that not all parts of a pipeline scale the same way.
- **Observability/logging:** Feature-distribution dashboards, model score-distribution drift alerts, and agent-decision audit logs (critical in a compliance-adjacent domain — this maps directly to the Regulated Industry/Compliance skill at 34.2% overall).
- **Cost control:** LLM is only invoked for flagged cases (a small % of total volume by design), not every transaction — this is the single biggest cost lever and is worth calling out as a deliberate architecture decision, not an afterthought.
- **Under real traffic:** The scoring path needs to hold sub-100ms p99 latency even at high transaction volume; the investigation path can tolerate seconds of latency since it's human-in-the-loop anyway — knowing which SLA applies where is exactly the "cost/latency tradeoff" maturity these JDs test for.

**7. Deployment plan:** FastAPI scoring service in Docker → **Google Cloud Run** (fast-path service, autoscaling on request volume) with the XGBoost model artifact loaded at startup; investigation-agent worker as a separate Cloud Run service or a Modal function (good fit for bursty, occasionally-long-running agent tasks on a free tier); vector DB free-tier hosted instance; MLflow tracking server on a small free-tier VM or Hugging Face Spaces.

**8. Stretch goals:** Add a feedback loop where analyst decisions (approve/reject the agent's recommendation) retrain/recalibrate the scoring model; add a second data modality (e.g., simple graph features for collusion-network detection, tying into the Knowledge Graphs trend); build a small dashboard visualizing drift over time.

**9. Estimated build time:** 2 weeks for the classical ML scoring pipeline + drift monitoring; 2 weeks for the RAG/agent investigation layer; 1 week for deployment/observability polish. **~5 weeks total** (this is the most effort-intensive of the three).

**10. Resume/LinkedIn bullets:**
- "Built a hybrid fraud-detection system combining a real-time XGBoost scoring service (sub-100ms p99) with an LLM investigation agent that grounds explanations in policy documents via RAG, reducing manual case-review time in simulation."
- "Implemented production ML lifecycle tooling — feature versioning, drift detection, and MLflow-tracked model registry — alongside an MCP-based agentic tool layer for automated case investigation."

---

### Project 3: EvalOps — Multi-Provider LLM Evaluation, Observability & Cost-Routing Platform

**1. One-line pitch:** A meta-platform that evaluates, benchmarks, and monitors *other* LLM applications — automatically scoring outputs for accuracy/groundedness/hallucination, tracking cost and latency across providers, and routing requests to the cheapest model that clears a quality bar. Directly mirrors what LawPro.ai, Cadence, DoorDash, and Booz Allen are all describing as a distinct, senior-leaning responsibility.

**2. Problem it solves / who'd use it:** Every team building LLM features eventually hits the same wall: "is the new prompt/model actually better, or does it just feel better?" and "our LLM bill is growing faster than usage." EvalOps gives a team a systematic, repeatable way to answer both — a platform engineer or ML team lead would plug their app's inputs/outputs into it to get regression detection, quality scoring, and cost/latency benchmarking across providers.

**3. Why a FAANG interviewer would find this compelling:** This is the project most likely to read as "senior" rather than "bootcamp graduate," because it's infrastructure *for* AI systems rather than an AI system itself — precisely the kind of platform-thinking Optum's Lead AI/ML Engineer, OptumInsight's AI Architect, and LawPro.ai's "own the evaluation, selection, and continuous optimization of LLMs" posting are describing. It directly showcases the single most universal skill in the entire 76-JD dataset (Monitoring/Observability, 63.2%) applied specifically to the domain (LLM evaluation frameworks, 17.1%, 76.9% hard-required when mentioned) that's graduating fastest from "nice to have" to "expected." It also signals multi-provider fluency and cost-consciousness — a concrete "not just an OpenAI wrapper" credential.

**4. Architecture:**
- **Ingestion:** Teams submit a batch of (prompt, expected-behavior-or-reference-answer, actual-output) triples via API or a small upload UI.
- **Evaluation engine:** Runs multiple evaluators in parallel — deterministic checks (format validity, length, banned-content regex), embedding-similarity-to-reference (uses the vector DB), and LLM-as-judge scoring (a second LLM call scores groundedness/relevance/coherence against a rubric) — RAG is used here specifically to ground the judge's scoring against a rubric/policy doc rather than the model's untethered opinion, which is the honest, justified use of RAG in this project (skip note: RAG is *not* used for the primary app output here, since this project's output is evaluation reports, not chat answers — grounding the *judge* is the deliberate design choice).
- **Cost/latency benchmarking:** The same prompt set is run against multiple providers/models (via OpenRouter, which conveniently exposes many models behind one API — a natural fit for this project specifically) with cost, latency, and quality score all logged side-by-side.
- **Routing layer:** A lightweight router that, given a new request, picks the cheapest model whose recent quality scores (from the eval engine) clear a configurable threshold for that task type — an MCP tool interface exposes "run_eval," "get_model_recommendation," and "flag_regression" so other agentic systems (like Projects 1 and 2 above) could, in principle, call into EvalOps as a tool themselves — the most natural, load-bearing MCP fit of the three projects.
- **Dashboard/API:** FastAPI serves both a programmatic API and the data backing a small dashboard (regression alerts, cost trends, provider comparison).

**5. Tech stack (mapped to matrix skills):**
| Component | Choice | Matrix skill it demonstrates |
|---|---|---|
| Multi-provider LLM access | OpenRouter free-tier models (best fit: purpose-built for comparing many models behind one interface, which is the entire point of this project) | LLMs (68.4%) |
| Judge grounding | RAG over a scoring rubric/policy doc | RAG (36.8%) — used deliberately, not by default |
| Eval frameworks | Custom harness inspired by RAGAS/DeepEval patterns | LLM Evaluation Frameworks (17.1%) |
| Agent/tool interface | MCP server exposing eval tools to external callers | MCP (5.3%), Tool Use (13.2%) |
| Backend | FastAPI, async batch + streaming endpoints | REST/APIs (48.7%) |
| Observability | Cost/latency/quality dashboards, regression alerting | Monitoring/Observability (63.2%) |
| Storage | Postgres for eval run history, vector DB for rubric grounding | Vector Databases (23.7%), SQL (23.7%) |

**6. Scalability plan:**
- **Async/concurrency:** Evaluation runs are embarrassingly parallel (each provider/prompt pair is independent) — use `asyncio.gather` with a bounded concurrency pool per provider to respect each provider's own rate limits without serializing everything.
- **Caching:** Cache judge scores for identical (prompt, output, rubric-version) triples — re-scoring identical content is pure waste; invalidate the cache whenever the rubric or judge model changes.
- **Horizontal scalability:** Evaluation workers are stateless and queue-driven, so throughput scales linearly with worker count — a natural fit to demonstrate a producer/consumer queue pattern explicitly.
- **Observability/logging:** This project's entire value proposition *is* observability, so the bar is higher — every eval run should be traceable back to exact prompt version, model version, and rubric version, which is the kind of reproducibility rigor senior JDs (Equifax, Optum Lead, OptumInsight) are explicitly screening for.
- **Cost control:** The routing layer's entire purpose is cost control — but also cache aggressively on the eval side, since LLM-as-judge calls are themselves a real cost that's easy to let spiral.
- **Under real traffic:** The interesting scaling question here isn't request volume, it's *provider diversity* — new models ship constantly, and the system needs to onboard a new provider/model without a redesign. Building the provider interface as a clean abstraction (not hardcoded per-provider logic scattered through the codebase) is the concrete design decision to point to.

**7. Deployment plan:** FastAPI in Docker → **Cloud Run** (GCP free credits) for the API and evaluation workers; Postgres via a free-tier hosted instance; vector DB (rubric grounding) via Chroma in-container or a free-tier hosted option; a lightweight dashboard on **Vercel free tier** reading from the API. Given this project is lower-traffic and more batch-oriented than Projects 1–2, **Modal** or **Hugging Face Spaces** are strong lightweight alternatives if you want to avoid managing a persistent Cloud Run service at all.

**8. Stretch goals:** Add a "regression bisection" feature that automatically identifies which prompt/model/rubric change caused a quality drop; add human-feedback collection to calibrate the LLM-judge against real human ratings over time; expose EvalOps as a genuine MCP server that Projects 1 and 2 actually call in a follow-up iteration, demonstrating a real multi-project agentic ecosystem.

**9. Estimated build time:** 1.5 weeks for the core eval engine + multi-provider benchmarking; 1 week for the routing layer; 1 week for the MCP interface + dashboard. **~3.5 weeks total.**

**10. Resume/LinkedIn bullets:**
- "Built a multi-provider LLM evaluation and cost-routing platform benchmarking 5+ models across quality, latency, and cost, with an MCP tool interface allowing other agentic systems to programmatically request evaluations."
- "Designed an LLM-as-judge evaluation harness with RAG-grounded scoring rubrics and automated regression detection, reducing (in simulation) the manual effort of comparing prompt/model changes."

---

## Portfolio Ranking & Build Order

| Rank | Project | Est. Build Time | Impact-to-Effort | Rationale |
|---|---|---|---|---|
| **1 — Build first** | DocuMind (Agentic RAG + MCP) | 3–4 weeks | **Highest** | Directly hits the dominant sub-domain (56.6% of the entire market). Broadest interviewer appeal — every company from startups to FAANG can relate to it immediately. Moderate build complexity relative to payoff. |
| **2 — Build second** | SentinelML (Hybrid ML + Agentic) | 5 weeks | High, but effort-heavy | Highest differentiation value — proves classical-ML depth that pure-GenAI portfolios lack, directly targeting the highest-paying senior postings in this dataset (Optum, OptumInsight, MeeBoss). Worth the extra week of effort specifically *because* fewer other candidates will have it. |
| **3 — Build third** | EvalOps (LLM Evaluation Platform) | 3.5 weeks | Highest ceiling, narrower audience | The most "senior-reads-as-senior" project of the three and the cleanest MCP use case, but it's platform-for-AI rather than an AI product, which resonates most with MLOps/platform-focused interviewers specifically rather than universally. Best as a third project once 1–2 are solid, to round out the portfolio from "I can build AI features" to "I can build the infrastructure that makes AI features trustworthy at scale." |

**Recommendation: build DocuMind first.** It's the fastest path to a complete, demoable, interview-ready project that maps to the single largest cluster of jobs in this entire dataset, and every skill you build for it (RAG, agents, FastAPI, Docker, MCP) is directly reused as the foundation for Projects 2 and 3 — so building in this order compounds rather than starting from scratch each time.
