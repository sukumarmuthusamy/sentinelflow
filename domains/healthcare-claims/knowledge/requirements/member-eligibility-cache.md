---
service: svc-core-api
version: 1.9.0
effective_date: 2025-11-01
---

# Member Eligibility Cache

## Summary
Eligibility lookups must respect cache invalidation events.

## Details
This section defines operational expectations for member-eligibility-cache.

Engineers MUST treat member-eligibility-cache as release-blocking when criticality is tagged critical.

Changes touching member-eligibility-cache require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to member-eligibility-cache.

Security auditors MUST evaluate auth surface changes that intersect with member-eligibility-cache.

Regression plans MUST include contract compatibility checks for member-eligibility-cache API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for member-eligibility-cache.

Policy overrides for verified secret exposure apply regardless of member-eligibility-cache risk score.

Test strategists MUST map required tests to services impacted by member-eligibility-cache modifications.

Documentation-only edits still require citation coverage when they alter member-eligibility-cache semantics.

All member-eligibility-cache artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link member-eligibility-cache remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate member-eligibility-cache acceptance criteria with measurable rollback triggers.

SREs MUST keep member-eligibility-cache runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever member-eligibility-cache stores payloads.

Performance tests for member-eligibility-cache MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting member-eligibility-cache MUST default to safe mode during staged rollouts.

Audit logs for member-eligibility-cache MUST retain correlation identifiers for at least ninety days.

Change tickets referencing member-eligibility-cache MUST list impacted services and required regression tests.

Architecture reviews for member-eligibility-cache SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by member-eligibility-cache incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for member-eligibility-cache MUST assert contract compatibility on every deploy window.

Knowledge base entries for member-eligibility-cache MUST include effective dates and document version stamps.
