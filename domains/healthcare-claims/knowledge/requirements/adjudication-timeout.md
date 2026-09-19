---
service: svc-core-api
version: 1.7.0
effective_date: 2025-11-01
---

# Adjudication Timeout

## Summary
Adjudication workflows must fail closed after SLA timeout.

## Details
This section defines operational expectations for adjudication-timeout.

Engineers MUST treat adjudication-timeout as release-blocking when criticality is tagged critical.

Changes touching adjudication-timeout require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to adjudication-timeout.

Security auditors MUST evaluate auth surface changes that intersect with adjudication-timeout.

Regression plans MUST include contract compatibility checks for adjudication-timeout API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for adjudication-timeout.

Policy overrides for verified secret exposure apply regardless of adjudication-timeout risk score.

Test strategists MUST map required tests to services impacted by adjudication-timeout modifications.

Documentation-only edits still require citation coverage when they alter adjudication-timeout semantics.

All adjudication-timeout artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link adjudication-timeout remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate adjudication-timeout acceptance criteria with measurable rollback triggers.

SREs MUST keep adjudication-timeout runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever adjudication-timeout stores payloads.

Performance tests for adjudication-timeout MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting adjudication-timeout MUST default to safe mode during staged rollouts.

Audit logs for adjudication-timeout MUST retain correlation identifiers for at least ninety days.

Change tickets referencing adjudication-timeout MUST list impacted services and required regression tests.

Architecture reviews for adjudication-timeout SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by adjudication-timeout incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for adjudication-timeout MUST assert contract compatibility on every deploy window.

Knowledge base entries for adjudication-timeout MUST include effective dates and document version stamps.
