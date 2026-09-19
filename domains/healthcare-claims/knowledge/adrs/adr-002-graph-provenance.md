---
service: svc-core-api
version: 1.0.0
effective_date: 2025-09-01
---

# ADR-002-GRAPH-PROVENANCE

## Decision
Tag dependency edges with grimp vs tree-sitter provenance.

## Context
This section defines operational expectations for adr-002-graph-provenance.

Engineers MUST treat adr-002-graph-provenance as release-blocking when criticality is tagged critical.

Changes touching adr-002-graph-provenance require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to adr-002-graph-provenance.

Security auditors MUST evaluate auth surface changes that intersect with adr-002-graph-provenance.

Regression plans MUST include contract compatibility checks for adr-002-graph-provenance API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for adr-002-graph-provenance.

Policy overrides for verified secret exposure apply regardless of adr-002-graph-provenance risk score.

Test strategists MUST map required tests to services impacted by adr-002-graph-provenance modifications.

Documentation-only edits still require citation coverage when they alter adr-002-graph-provenance semantics.

All adr-002-graph-provenance artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link adr-002-graph-provenance remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate adr-002-graph-provenance acceptance criteria with measurable rollback triggers.

SREs MUST keep adr-002-graph-provenance runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever adr-002-graph-provenance stores payloads.

Performance tests for adr-002-graph-provenance MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting adr-002-graph-provenance MUST default to safe mode during staged rollouts.

Audit logs for adr-002-graph-provenance MUST retain correlation identifiers for at least ninety days.

Change tickets referencing adr-002-graph-provenance MUST list impacted services and required regression tests.

Architecture reviews for adr-002-graph-provenance SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by adr-002-graph-provenance incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for adr-002-graph-provenance MUST assert contract compatibility on every deploy window.

Knowledge base entries for adr-002-graph-provenance MUST include effective dates and document version stamps.
