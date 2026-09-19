---
service: svc-core-api
version: 3.1.0
effective_date: 2025-12-01
---

# Regression plan for svc-core-api

## Required tests
This section defines operational expectations for regression coverage.

Engineers MUST treat regression coverage as release-blocking when criticality is tagged critical.

Changes touching regression coverage require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to regression coverage.

Security auditors MUST evaluate auth surface changes that intersect with regression coverage.

Regression plans MUST include contract compatibility checks for regression coverage API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for regression coverage.

Policy overrides for verified secret exposure apply regardless of regression coverage risk score.

Test strategists MUST map required tests to services impacted by regression coverage modifications.

Documentation-only edits still require citation coverage when they alter regression coverage semantics.

All regression coverage artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link regression coverage remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate regression coverage acceptance criteria with measurable rollback triggers.

SREs MUST keep regression coverage runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever regression coverage stores payloads.

Performance tests for regression coverage MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting regression coverage MUST default to safe mode during staged rollouts.

Audit logs for regression coverage MUST retain correlation identifiers for at least ninety days.

Change tickets referencing regression coverage MUST list impacted services and required regression tests.

Architecture reviews for regression coverage SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by regression coverage incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for regression coverage MUST assert contract compatibility on every deploy window.

Knowledge base entries for regression coverage MUST include effective dates and document version stamps.
