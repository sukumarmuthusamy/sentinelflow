## Recommended project

# **SentinelFlow: Multi-Agent Software Change Risk & Incident Intelligence Platform**

A production-style AI system that helps engineering teams:

- analyze a pull request or planned release
- retrieve relevant architecture documents, historical incidents, runbooks, and past code changes
- predict affected services and regression risk
- generate a targeted test plan
- investigate logs or incident reports
- recommend remediation steps
- require human approval before any sensitive action
- produce an evidence-backed engineering report

This is much stronger than a generic “research assistant with five agents.” It demonstrates the problems large technology companies actually care about: developer productivity, reliability, observability, security, controlled automation, evaluation, and scalable system design.

It also connects naturally with your automation-testing and AI/ML background.

---

## What the demo would look like

A user submits:

> Analyze PR #248. Identify affected components, retrieve similar historical failures, create a regression test plan, and assess whether it is safe to deploy.

SentinelFlow then:

1. Fetches the PR, changed files, commits, and repository metadata through a **GitHub MCP server**.
2. Retrieves architecture documents, coding standards, runbooks, and prior incidents using **RAG**.
3. Maps the changed code to services and dependencies.
4. Assigns specialized analyses to multiple agents.
5. Runs deterministic validations and optionally executes safe tests in an isolated environment.
6. Cross-checks every important claim against retrieved evidence.
7. Calculates a deployment risk score.
8. Returns:
   - executive summary
   - affected services
   - likely regressions
   - recommended tests
   - security concerns
   - evidence citations
   - approval or rejection recommendation
9. Pauses for human approval before actions such as creating an issue, commenting on a PR, or triggering CI.

That gives you a compelling end-to-end video and a meaningful architecture discussion in interviews.

---

# Recommended architecture

```text id="my77p6"
                         ┌─────────────────────┐
                         │ React / Next.js UI  │
                         └──────────┬──────────┘
                                    │
                             HTTPS / SSE
                                    │
                         ┌──────────▼──────────┐
                         │ FastAPI API Gateway │
                         │ Auth, limits, jobs  │
                         └──────────┬──────────┘
                                    │
                         ┌──────────▼──────────┐
                         │ LangGraph Workflow  │
                         │ Supervisor + State  │
                         └──────────┬──────────┘
                                    │
         ┌──────────────────────────┼──────────────────────────┐
         │                          │                          │
 ┌───────▼────────┐        ┌────────▼────────┐       ┌────────▼────────┐
 │ Context Agent  │        │ Code Risk Agent │       │ Test Plan Agent │
 └───────┬────────┘        └────────┬────────┘       └────────┬────────┘
         │                          │                          │
         └──────────────┬───────────┴──────────────┬───────────┘
                        │                          │
               ┌────────▼────────┐        ┌────────▼──────────┐
               │ Security Agent  │        │ Evidence Verifier │
               └────────┬────────┘        └────────┬──────────┘
                        └─────────────┬─────────────┘
                                      │
                            ┌─────────▼──────────┐
                            │ Report / Decision │
                            │ Human Approval    │
                            └─────────┬──────────┘
                                      │
              ┌───────────────────────┼────────────────────────┐
              │                       │                        │
       ┌──────▼──────┐        ┌───────▼───────┐       ┌────────▼───────┐
       │ MCP Servers │        │ Vector Store  │       │ PostgreSQL     │
       │ GitHub etc. │        │ pgvector      │       │ State/Audits   │
       └─────────────┘        └───────────────┘       └────────────────┘
```

---

## Why LangGraph rather than plain LangChain agents

Use:

- **LangChain** for model abstraction, prompts, tools, retrievers, structured output, and document processing.
- **LangGraph** for the actual orchestration.

LangGraph is designed for stateful, long-running workflows and supports persistence, conditional routing, streaming, debugging, and human-in-the-loop controls. LangChain’s own documentation also distinguishes deterministic workflows from dynamic agents, which is important here. ([langchain.com](https://www.langchain.com/langgraph?utm_source=chatgpt.com))

The workflow should be mostly controlled and graph-based:

```text id="x3y5y6"
START
  ↓
validate_request
  ↓
classify_task
  ↓
collect_context
  ↓
plan_analysis
  ↓
parallel:
    code_risk_analysis
    security_analysis
    regression_test_generation
    historical_incident_search
  ↓
merge_findings
  ↓
evidence_verification
  ↓
risk_scoring
  ↓
human_approval_required?
  ├── yes → interrupt_and_wait
  └── no
  ↓
generate_report
  ↓
END
```

Do not allow agents to freely converse until they “feel done.” That looks impressive for approximately twelve minutes and then becomes expensive jazz.

---

# Agent design

## 1. Supervisor / Orchestrator

Responsibilities:

- understand the requested task
- create a typed execution plan
- decide which specialist agents are necessary
- enforce maximum iterations and token budgets
- maintain workflow state
- route failures and retries
- request human approval when required

The supervisor should not perform every task itself.

## 2. Context and Retrieval Agent

Searches:

- architecture documentation
- runbooks
- service ownership documents
- historical incidents
- past PRs
- coding standards
- API contracts
- test documentation

It returns evidence objects rather than a plain paragraph:

```json id="nfx0el"
{
  "claim_context": "PaymentService depends on FraudService",
  "source_id": "architecture-doc-17",
  "section": "Service dependencies",
  "relevance_score": 0.91
}
```

## 3. Code Change Risk Agent

Analyzes:

- changed files
- imports and call relationships
- APIs or database schemas modified
- backward compatibility
- blast radius
- complexity
- missing error handling
- concurrency or performance concerns

For the MVP, use repository structure plus static-analysis tools. Later, add a code-property graph or dependency graph.

## 4. Regression Test Planning Agent

Produces:

- impacted functionality
- unit tests
- API tests
- integration tests
- end-to-end tests
- negative cases
- boundary cases
- data validation
- priority and rationale

This is where your QA background becomes a real differentiator.

## 5. Security Review Agent

Checks:

- secret exposure
- authentication and authorization changes
- unsafe tool calls
- input validation
- dependency vulnerabilities
- prompt-injection risks in retrieved documents
- overly permissive MCP capabilities

## 6. Evidence Verifier

This agent is critical.

It checks:

- whether every material claim has supporting evidence
- whether a source actually supports the claim
- whether multiple agents disagree
- whether the answer contains unsupported assumptions
- whether retrieved content could contain prompt injection

Low-confidence or conflicting findings are sent back for one controlled retry.

## 7. Decision and Report Agent

Produces structured output:

```json id="siizk4"
{
  "risk_level": "high",
  "risk_score": 82,
  "deployment_recommendation": "block",
  "affected_services": [],
  "required_tests": [],
  "security_findings": [],
  "evidence": [],
  "uncertainties": [],
  "human_approval_required": true
}
```

---

# Where MCP should be used

MCP is appropriate for exposing **live capabilities and operational systems** to the agents. It is an open protocol for connecting LLM applications with tools and external data sources. ([modelcontextprotocol.io](https://modelcontextprotocol.io/specification/draft?utm_source=chatgpt.com))

Use MCP for:

- GitHub repositories, PRs, issues, and commits
- file-system or document access
- CI/CD status
- issue trackers
- observability systems
- test execution
- controlled database metadata queries

For the first release, build or integrate these:

### MCP server 1: GitHub

Tools:

```text id="22zcnn"
get_pull_request
get_changed_files
get_file_content
search_repository
get_commit_history
get_workflow_status
create_pr_comment
create_issue
```

Keep write operations behind human approval.

### MCP server 2: Engineering knowledge

Resources:

```text id="g082f5"
architecture documents
runbooks
incident reports
test plans
API specifications
service catalog
```

This MCP server can expose document resources, while your RAG subsystem performs semantic retrieval over the same approved corpus.

### MCP server 3: Safe execution

Tools:

```text id="3859gz"
run_unit_tests
run_static_analysis
run_linter
run_dependency_scan
get_test_results
```

Execute these inside a restricted container. Do not give the LLM unrestricted shell access.

### MCP security controls

Treat every MCP response as untrusted input:

- tool allowlists
- per-agent permissions
- JSON-schema validation
- request timeouts
- argument sanitization
- no arbitrary command execution
- secret isolation
- audit logs
- human approval for mutations
- prompt-injection detection
- maximum response size
- network egress restrictions

MCP and agent frameworks have a meaningful security attack surface, so demonstrating permission scoping and auditability will improve the project rather than merely complicating it. ([arxiv.org](https://arxiv.org/abs/2602.01129?utm_source=chatgpt.com))

---

# Where RAG should be used

Do not use RAG merely because the acronym photographs well on LinkedIn.

Use it for information that is:

- private
- domain-specific
- too large for the prompt
- frequently updated
- required as evidence

## Recommended RAG pipeline

```text id="o16sju"
Documents
   ↓
Parsing and metadata extraction
   ↓
Structure-aware chunking
   ↓
Embedding generation
   ↓
PostgreSQL + pgvector
   ↓
Hybrid retrieval
   ├── vector similarity
   └── PostgreSQL full-text/BM25-style search
   ↓
Metadata filtering
   ↓
Reranking
   ↓
Context compression
   ↓
Evidence-grounded generation
```

### Metadata per chunk

```json id="77mq37"
{
  "document_id": "incident-2026-014",
  "document_type": "incident_report",
  "service": "payment-service",
  "repository": "commerce-platform",
  "version": "2.4",
  "created_at": "2026-06-18",
  "access_scope": "engineering",
  "chunk_index": 14
}
```

### Retrieval improvements

Implement these incrementally:

1. semantic vector search
2. keyword or full-text search
3. reciprocal-rank fusion
4. metadata filters
5. cross-encoder or LLM reranking
6. query rewriting
7. parent-document retrieval
8. evidence quality scoring

For code, ordinary text chunking is weak. Chunk by:

- class
- function
- module
- API endpoint
- configuration section
- test case

Use Tree-sitter or Python AST parsing later.

---

# Recommended technology stack

## Backend

```text id="8gwfgy"
Python 3.12
FastAPI
LangChain
LangGraph
Pydantic v2
SQLAlchemy
Alembic
HTTPX
```

## Data

```text id="b93znw"
PostgreSQL
pgvector
Redis
Google Cloud Storage or S3-compatible object storage
```

PostgreSQL plus pgvector is preferable to introducing a separate vector database in the MVP. It shows sensible engineering judgment and reduces operational overhead.

## Async processing

MVP:

```text id="1x1nlo"
FastAPI BackgroundTasks or an asyncio worker
```

Portfolio-grade version:

```text id="36gizq"
Celery or Dramatiq
Redis
```

Scale version:

```text id="r16yrm"
Cloud Tasks / Pub/Sub
separate worker services
```

## Observability

```text id="oh48ro"
OpenTelemetry
LangSmith or self-hosted traces
Prometheus metrics
Grafana dashboards
structured JSON logs
```

Track:

- request latency
- model latency
- tokens by agent
- cost by workflow
- retrieval precision
- tool failures
- retry counts
- unsupported-claim rate
- human approval rate
- workflow success rate

## Frontend

```text id="byppwb"
Next.js or React
TypeScript
Server-Sent Events for live workflow updates
```

The UI should display the graph execution:

```text id="qow1nu"
Retrieving context ✓
Analyzing code ✓
Generating tests ✓
Verifying evidence ...
```

This makes the architecture visible during demos.

---

# LLM provider strategy

Do not hard-code the application to one provider.

Create an internal model gateway:

```python id="760trh"
class ModelGateway:
    async def generate(
        self,
        task_type: str,
        messages: list,
        response_schema: type | None = None,
        requirements: ModelRequirements | None = None,
    ):
        ...
```

Then configure routing:

```yaml id="d0dh84"
models:
  planner:
    provider: gemini
    model: selected-gemini-model

  code_analysis:
    provider: nvidia
    model: selected-nemotron-or-coding-model

  summarization:
    provider: openrouter
    model: openrouter/free

  verifier:
    provider: gemini
    model: selected-gemini-model
```

## My recommendation

### Primary during development: Gemini

Use Gemini for the supervisor, structured reasoning, and verification if your API quota is sufficient.

### Secondary: NVIDIA NIM

Use NVIDIA for code analysis or tool-calling experiments. NVIDIA currently advertises free serverless APIs for development, and several model pages expose free prototype endpoints. ([build.nvidia.com](https://build.nvidia.com/explore/discover?utm_source=chatgpt.com))

### Fallback and model experimentation: OpenRouter

OpenRouter is excellent for provider abstraction and testing multiple models. It provides a unified API and model fallback capabilities. ([openrouter.ai](https://openrouter.ai/docs/guides/overview/models?utm_source=chatgpt.com))

However, do not treat OpenRouter’s free models as the production backend. Its documentation warns that free variants may have lower limits or availability, and its FAQ says they are generally unsuitable for production use. ([openrouter.ai](https://openrouter.ai/docs/guides/routing/model-variants/free?utm_source=chatgpt.com))

## Important design

Add capability-based routing:

```text id="1gduiu"
Does task require tool calling?
Does it require structured JSON?
Does it contain sensitive data?
What is the maximum cost?
What latency is acceptable?
Is the primary provider healthy?
```

Then route based on capability, not simply a fixed model name.

This provider-neutral design is far more valuable in interviews than saying, “I called Gemini from six agents.”

---

# Deployment recommendation

## Best initial option: Google Cloud Run

Deploy:

```text id="55pmbs"
Frontend             → Cloud Run
FastAPI API          → Cloud Run
Agent worker         → Cloud Run
MCP servers          → Separate Cloud Run services
PostgreSQL           → Cloud SQL
Redis                → Memorystore or small external Redis
Documents            → Cloud Storage
Secrets              → Secret Manager
Container images     → Artifact Registry
Async events         → Pub/Sub or Cloud Tasks
Monitoring           → Cloud Logging and OpenTelemetry
```

Cloud Run supports scale-to-zero and currently includes a monthly free tier. Its request-based free allowance includes up to 2 million requests and defined CPU and memory allowances. ([cloud.google.com](https://cloud.google.com/run/pricing?utm_source=chatgpt.com))

New Google Cloud customers currently receive **$300 in credits for 90 days**, not merely an unspecified permanent $300 balance. ([cloud.google.com](https://cloud.google.com/free?utm_source=chatgpt.com))

Cloud Run is the best choice here because:

- containers keep the application portable
- FastAPI works naturally
- services scale independently
- scale-to-zero reduces demo cost
- separate MCP servers can be isolated
- it provides a credible production deployment story
- migration to Kubernetes remains possible later

## Azure option

Azure is also valid, using:

```text id="6qdykx"
Azure Container Apps
Azure Database for PostgreSQL
Azure Cache for Redis
Blob Storage
Service Bus
Key Vault
Application Insights
```

One correction: the standard Azure free account currently advertises **$200 for 30 days**, subject to eligibility, rather than $100. ([azure.microsoft.com](https://azure.microsoft.com/en-us/pricing/purchase-options/azure-account?utm_source=chatgpt.com))

Azure would be a good choice when targeting Microsoft-heavy enterprise roles, but Google Cloud Run is simpler for this particular portfolio build.

## Hugging Face Spaces

Useful for:

- a quick public UI
- an early Gradio demonstration
- showcasing a model prototype

Not my choice for the full backend, database, worker, MCP, queue, and observability architecture. It can be a demo surface, not the system’s backbone.

---

# Scaling strategy

## Stage 1: Modular monolith

One repository, but clearly separated modules:

```text id="twpx3g"
sentinelflow/
├── apps/
│   ├── api/
│   ├── worker/
│   └── web/
├── orchestration/
│   ├── graph.py
│   ├── state.py
│   ├── routing.py
│   └── policies.py
├── agents/
│   ├── supervisor/
│   ├── retrieval/
│   ├── code_risk/
│   ├── security/
│   ├── test_planner/
│   └── verifier/
├── mcp_servers/
│   ├── github/
│   ├── knowledge/
│   └── execution/
├── rag/
│   ├── ingestion/
│   ├── chunking/
│   ├── embeddings/
│   ├── retrieval/
│   └── reranking/
├── model_gateway/
├── evaluation/
├── infrastructure/
├── tests/
└── docker-compose.yml
```

This is easier to build, test, and deploy.

## Stage 2: Separate workers

Split:

- API
- ingestion worker
- orchestration worker
- MCP integrations
- test-execution sandbox

## Stage 3: Event-driven architecture

```text id="ja0pbk"
API request
    ↓
job queue
    ↓
workflow workers
    ↓
tool events
    ↓
result store
    ↓
SSE/WebSocket notification
```

## Scale techniques

- stateless API containers
- persistent LangGraph checkpoints
- idempotency keys
- distributed locks
- queue-based backpressure
- per-tenant rate limits
- provider circuit breakers
- retries with exponential backoff
- retrieval caching
- semantic response caching
- batched embeddings
- asynchronous agent execution
- token and cost budgets
- database connection pooling
- tenant-aware data isolation
- dead-letter queues

---

# Features that will make FAANG-level reviewers pay attention

## 1. Typed workflow state

Use Pydantic models for every state transition:

```python id="vram4a"
class AnalysisState(BaseModel):
    request_id: UUID
    repository: str
    pull_request_number: int
    execution_plan: list[Task]
    retrieved_evidence: list[Evidence]
    code_findings: list[Finding]
    security_findings: list[Finding]
    proposed_tests: list[TestCase]
    risk_score: int | None = None
    approval_status: str = "not_required"
    errors: list[WorkflowError] = []
```

## 2. Deterministic risk engine

Do not let the LLM invent the final score.

```text id="9cla6q"
Risk score =
  25% change blast radius
  20% critical-service impact
  15% test coverage gap
  15% security severity
  10% historical incident similarity
  10% schema/API compatibility
   5% model confidence penalty
```

The LLM generates findings. Code calculates the score.

## 3. Human-in-the-loop

Require approval before:

- posting a PR comment
- opening an issue
- triggering a CI workflow
- changing repository content
- executing an expensive workflow

## 4. Failure recovery

Demonstrate:

- provider outage fallback
- malformed structured output repair
- MCP timeout
- missing documents
- retrieval with no strong evidence
- workflow checkpoint and resume
- partial result return

## 5. Security model

Include:

- tenant isolation
- least-privilege MCP tools
- short-lived credentials
- encrypted secrets
- retrieval access control
- input and output validation
- prompt-injection defenses
- tool-call audit history
- PII redaction
- dependency scanning

## 6. Evaluation framework

This is possibly the most important differentiator.

Create a benchmark dataset of 50 to 100 synthetic PR scenarios containing:

- changed files
- architecture context
- historical incidents
- known affected services
- expected tests
- expected risk category

Measure:

```text id="qvkzlp"
Affected-service precision and recall
Risk classification F1
Test recommendation coverage
Citation correctness
Unsupported-claim rate
Tool-selection accuracy
Workflow completion rate
Average latency
Tokens per successful task
Cost per workflow
```

Compare:

```text id="v3qhqv"
Single LLM baseline
Single agent + RAG
Multi-agent without verifier
Multi-agent + verifier
Hybrid deterministic workflow
```

That transforms the repository from “AI demo” into an engineering and experimentation project.

---

# Build roadmap

## Phase 1: Foundation

Build:

- FastAPI endpoints
- provider-neutral LLM gateway
- LangGraph state definition
- PostgreSQL models
- Docker Compose
- one complete PR-analysis workflow
- structured JSON responses

## Phase 2: RAG

Add:

- document ingestion
- pgvector
- architecture and incident documents
- hybrid retrieval
- source citations
- retrieval evaluation

## Phase 3: MCP

Add:

- GitHub MCP integration
- internal knowledge MCP server
- safe test-execution MCP server
- read/write permission separation
- audit logging

## Phase 4: Multi-agent orchestration

Add:

- supervisor
- code-risk agent
- test-planning agent
- security agent
- verifier
- parallel execution
- retries and conditional routing

## Phase 5: Production features

Add:

- Redis and worker queue
- checkpointing
- SSE progress streaming
- authentication
- rate limiting
- cost budgets
- OpenTelemetry
- dashboards

## Phase 6: Evaluation and cloud

Add:

- benchmark dataset
- baseline comparison
- CI/CD
- Terraform
- Cloud Run deployment
- architecture diagrams
- demo video
- engineering report

---

# What not to build initially

Avoid these traps:

- ten agents with vague responsibilities
- autonomous agents with unrestricted shell access
- one massive prompt pretending to be orchestration
- separate vector databases, graph databases, Kubernetes, Kafka, and six microservices on day one
- free-model endpoints as a production guarantee
- RAG without retrieval evaluation
- MCP tools without permissions
- “confidence score” generated directly by the LLM
- claiming FAANG scale without load tests or architecture evidence

---

# Final recommended setup

```text id="ozrzud"
Project:
SentinelFlow, Multi-Agent Change Risk and Incident Intelligence

Orchestration:
LangGraph + LangChain

API:
FastAPI

Primary LLM:
Gemini

Secondary LLM:
NVIDIA NIM

Experimentation/Fallback:
OpenRouter

RAG:
PostgreSQL + pgvector
Hybrid search
Optional reranker

MCP:
GitHub
Engineering knowledge
Safe test execution

Database:
PostgreSQL

Queue/Cache:
Redis initially
Pub/Sub or Cloud Tasks later

Frontend:
Next.js + TypeScript

Observability:
OpenTelemetry + LangSmith/Grafana

Deployment:
Docker + Google Cloud Run

Infrastructure:
Terraform

CI/CD:
GitHub Actions

Security:
Least privilege, approval gates, schema validation, audit logs

Evaluation:
Golden PR dataset + single-agent and multi-agent comparisons
```

This setup shows **LLM engineering, agent orchestration, RAG, MCP, backend engineering, cloud architecture, DevOps, security, evaluation, and testing** in one coherent product. More importantly, every technology has a reason to be there. That is exactly what separates a serious portfolio project from an acronym casserole.
