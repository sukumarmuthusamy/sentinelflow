---
service: svc-core-api
version: 1.0.0
effective_date: 2025-08-01
---

# Evidence Coverage

## Policy
Coverage floor 0.75 applies before PASS verdict.

## Rationale
This section defines operational expectations for evidence-coverage.

Engineers MUST treat evidence-coverage as release-blocking when criticality is tagged critical.

Changes touching evidence-coverage require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to evidence-coverage.

Security auditors MUST evaluate auth surface changes that intersect with evidence-coverage.

Regression plans MUST include contract compatibility checks for evidence-coverage API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for evidence-coverage.

Policy overrides for verified secret exposure apply regardless of evidence-coverage risk score.

Test strategists MUST map required tests to services impacted by evidence-coverage modifications.

Documentation-only edits still require citation coverage when they alter evidence-coverage semantics.

All evidence-coverage artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link evidence-coverage remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate evidence-coverage acceptance criteria with measurable rollback triggers.

SREs MUST keep evidence-coverage runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever evidence-coverage stores payloads.

Performance tests for evidence-coverage MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting evidence-coverage MUST default to safe mode during staged rollouts.

Audit logs for evidence-coverage MUST retain correlation identifiers for at least ninety days.

Change tickets referencing evidence-coverage MUST list impacted services and required regression tests.

Architecture reviews for evidence-coverage SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by evidence-coverage incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for evidence-coverage MUST assert contract compatibility on every deploy window.

Knowledge base entries for evidence-coverage MUST include effective dates and document version stamps.
