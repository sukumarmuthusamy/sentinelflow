---
service: svc-core-api
version: 1.5.0
effective_date: 2025-11-01
---

# Release Evidence Floor

## Summary
Evidence coverage below 0.75 forces REVIEW.

## Details
This section defines operational expectations for release-evidence-floor.

Engineers MUST treat release-evidence-floor as release-blocking when criticality is tagged critical.

Changes touching release-evidence-floor require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to release-evidence-floor.

Security auditors MUST evaluate auth surface changes that intersect with release-evidence-floor.

Regression plans MUST include contract compatibility checks for release-evidence-floor API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for release-evidence-floor.

Policy overrides for verified secret exposure apply regardless of release-evidence-floor risk score.

Test strategists MUST map required tests to services impacted by release-evidence-floor modifications.

Documentation-only edits still require citation coverage when they alter release-evidence-floor semantics.

All release-evidence-floor artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link release-evidence-floor remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate release-evidence-floor acceptance criteria with measurable rollback triggers.

SREs MUST keep release-evidence-floor runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever release-evidence-floor stores payloads.

Performance tests for release-evidence-floor MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting release-evidence-floor MUST default to safe mode during staged rollouts.

Audit logs for release-evidence-floor MUST retain correlation identifiers for at least ninety days.

Change tickets referencing release-evidence-floor MUST list impacted services and required regression tests.

Architecture reviews for release-evidence-floor SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by release-evidence-floor incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for release-evidence-floor MUST assert contract compatibility on every deploy window.

Knowledge base entries for release-evidence-floor MUST include effective dates and document version stamps.
