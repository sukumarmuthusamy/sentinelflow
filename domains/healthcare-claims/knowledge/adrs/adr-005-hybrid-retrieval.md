---
service: svc-analytics
version: 1.0.0
effective_date: 2025-09-01
---

# ADR-005-HYBRID-RETRIEVAL

## Decision
Hybrid retrieval with reranking for incident similarity.

## Context
This section defines operational expectations for adr-005-hybrid-retrieval.

Engineers MUST treat adr-005-hybrid-retrieval as release-blocking when criticality is tagged critical.

Changes touching adr-005-hybrid-retrieval require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to adr-005-hybrid-retrieval.

Security auditors MUST evaluate auth surface changes that intersect with adr-005-hybrid-retrieval.

Regression plans MUST include contract compatibility checks for adr-005-hybrid-retrieval API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for adr-005-hybrid-retrieval.

Policy overrides for verified secret exposure apply regardless of adr-005-hybrid-retrieval risk score.

Test strategists MUST map required tests to services impacted by adr-005-hybrid-retrieval modifications.

Documentation-only edits still require citation coverage when they alter adr-005-hybrid-retrieval semantics.

All adr-005-hybrid-retrieval artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link adr-005-hybrid-retrieval remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate adr-005-hybrid-retrieval acceptance criteria with measurable rollback triggers.

SREs MUST keep adr-005-hybrid-retrieval runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever adr-005-hybrid-retrieval stores payloads.

Performance tests for adr-005-hybrid-retrieval MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting adr-005-hybrid-retrieval MUST default to safe mode during staged rollouts.

Audit logs for adr-005-hybrid-retrieval MUST retain correlation identifiers for at least ninety days.

Change tickets referencing adr-005-hybrid-retrieval MUST list impacted services and required regression tests.

Architecture reviews for adr-005-hybrid-retrieval SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by adr-005-hybrid-retrieval incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for adr-005-hybrid-retrieval MUST assert contract compatibility on every deploy window.

Knowledge base entries for adr-005-hybrid-retrieval MUST include effective dates and document version stamps.
