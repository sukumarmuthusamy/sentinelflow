---
service: svc-core-api
version: 1.0.0
effective_date: 2025-08-01
---

# Override Matrix

## Policy
Verified secret exposure and critical contract breaks force BLOCK.

## Rationale
This section defines operational expectations for override-matrix.

Engineers MUST treat override-matrix as release-blocking when criticality is tagged critical.

Changes touching override-matrix require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to override-matrix.

Security auditors MUST evaluate auth surface changes that intersect with override-matrix.

Regression plans MUST include contract compatibility checks for override-matrix API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for override-matrix.

Policy overrides for verified secret exposure apply regardless of override-matrix risk score.

Test strategists MUST map required tests to services impacted by override-matrix modifications.

Documentation-only edits still require citation coverage when they alter override-matrix semantics.

All override-matrix artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link override-matrix remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate override-matrix acceptance criteria with measurable rollback triggers.

SREs MUST keep override-matrix runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever override-matrix stores payloads.

Performance tests for override-matrix MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting override-matrix MUST default to safe mode during staged rollouts.

Audit logs for override-matrix MUST retain correlation identifiers for at least ninety days.

Change tickets referencing override-matrix MUST list impacted services and required regression tests.

Architecture reviews for override-matrix SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by override-matrix incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for override-matrix MUST assert contract compatibility on every deploy window.

Knowledge base entries for override-matrix MUST include effective dates and document version stamps.
