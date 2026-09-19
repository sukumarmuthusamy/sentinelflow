---
service: svc-core-api
version: 1.6.0
effective_date: 2025-11-01
---

# Contract Breaking Block

## Summary
oasdiff ERR on critical services forces BLOCK.

## Details
This section defines operational expectations for contract-breaking-block.

Engineers MUST treat contract-breaking-block as release-blocking when criticality is tagged critical.

Changes touching contract-breaking-block require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to contract-breaking-block.

Security auditors MUST evaluate auth surface changes that intersect with contract-breaking-block.

Regression plans MUST include contract compatibility checks for contract-breaking-block API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for contract-breaking-block.

Policy overrides for verified secret exposure apply regardless of contract-breaking-block risk score.

Test strategists MUST map required tests to services impacted by contract-breaking-block modifications.

Documentation-only edits still require citation coverage when they alter contract-breaking-block semantics.

All contract-breaking-block artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link contract-breaking-block remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate contract-breaking-block acceptance criteria with measurable rollback triggers.

SREs MUST keep contract-breaking-block runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever contract-breaking-block stores payloads.

Performance tests for contract-breaking-block MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting contract-breaking-block MUST default to safe mode during staged rollouts.

Audit logs for contract-breaking-block MUST retain correlation identifiers for at least ninety days.

Change tickets referencing contract-breaking-block MUST list impacted services and required regression tests.

Architecture reviews for contract-breaking-block SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by contract-breaking-block incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for contract-breaking-block MUST assert contract compatibility on every deploy window.

Knowledge base entries for contract-breaking-block MUST include effective dates and document version stamps.
