---
service: svc-core-api
version: 1.0.0
effective_date: 2025-08-01
---

# Release Thresholds

## Policy
PASS below 40, REVIEW below 70, BLOCK otherwise.

## Rationale
This section defines operational expectations for release-thresholds.

Engineers MUST treat release-thresholds as release-blocking when criticality is tagged critical.

Changes touching release-thresholds require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to release-thresholds.

Security auditors MUST evaluate auth surface changes that intersect with release-thresholds.

Regression plans MUST include contract compatibility checks for release-thresholds API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for release-thresholds.

Policy overrides for verified secret exposure apply regardless of release-thresholds risk score.

Test strategists MUST map required tests to services impacted by release-thresholds modifications.

Documentation-only edits still require citation coverage when they alter release-thresholds semantics.

All release-thresholds artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link release-thresholds remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate release-thresholds acceptance criteria with measurable rollback triggers.

SREs MUST keep release-thresholds runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever release-thresholds stores payloads.

Performance tests for release-thresholds MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting release-thresholds MUST default to safe mode during staged rollouts.

Audit logs for release-thresholds MUST retain correlation identifiers for at least ninety days.

Change tickets referencing release-thresholds MUST list impacted services and required regression tests.

Architecture reviews for release-thresholds SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by release-thresholds incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for release-thresholds MUST assert contract compatibility on every deploy window.

Knowledge base entries for release-thresholds MUST include effective dates and document version stamps.
