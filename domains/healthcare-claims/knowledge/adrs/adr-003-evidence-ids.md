---
service: svc-core-api
version: 1.0.0
effective_date: 2025-09-01
---

# ADR-003-EVIDENCE-IDS

## Decision
Stable document and section IDs for citation validation.

## Context
This section defines operational expectations for adr-003-evidence-ids.

Engineers MUST treat adr-003-evidence-ids as release-blocking when criticality is tagged critical.

Changes touching adr-003-evidence-ids require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to adr-003-evidence-ids.

Security auditors MUST evaluate auth surface changes that intersect with adr-003-evidence-ids.

Regression plans MUST include contract compatibility checks for adr-003-evidence-ids API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for adr-003-evidence-ids.

Policy overrides for verified secret exposure apply regardless of adr-003-evidence-ids risk score.

Test strategists MUST map required tests to services impacted by adr-003-evidence-ids modifications.

Documentation-only edits still require citation coverage when they alter adr-003-evidence-ids semantics.

All adr-003-evidence-ids artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link adr-003-evidence-ids remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate adr-003-evidence-ids acceptance criteria with measurable rollback triggers.

SREs MUST keep adr-003-evidence-ids runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever adr-003-evidence-ids stores payloads.

Performance tests for adr-003-evidence-ids MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting adr-003-evidence-ids MUST default to safe mode during staged rollouts.

Audit logs for adr-003-evidence-ids MUST retain correlation identifiers for at least ninety days.

Change tickets referencing adr-003-evidence-ids MUST list impacted services and required regression tests.

Architecture reviews for adr-003-evidence-ids SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by adr-003-evidence-ids incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for adr-003-evidence-ids MUST assert contract compatibility on every deploy window.

Knowledge base entries for adr-003-evidence-ids MUST include effective dates and document version stamps.
