---
service: svc-payment-api
version: 1.5.0
effective_date: 2025-11-01
---

# Refund Double Charge

## Summary
Refund API must be idempotent per payment intent.

## Details
This section defines operational expectations for refund-double-charge.

Engineers MUST treat refund-double-charge as release-blocking when criticality is tagged critical.

Changes touching refund-double-charge require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to refund-double-charge.

Security auditors MUST evaluate auth surface changes that intersect with refund-double-charge.

Regression plans MUST include contract compatibility checks for refund-double-charge API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for refund-double-charge.

Policy overrides for verified secret exposure apply regardless of refund-double-charge risk score.

Test strategists MUST map required tests to services impacted by refund-double-charge modifications.

Documentation-only edits still require citation coverage when they alter refund-double-charge semantics.

All refund-double-charge artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link refund-double-charge remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate refund-double-charge acceptance criteria with measurable rollback triggers.

SREs MUST keep refund-double-charge runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever refund-double-charge stores payloads.

Performance tests for refund-double-charge MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting refund-double-charge MUST default to safe mode during staged rollouts.

Audit logs for refund-double-charge MUST retain correlation identifiers for at least ninety days.

Change tickets referencing refund-double-charge MUST list impacted services and required regression tests.

Architecture reviews for refund-double-charge SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by refund-double-charge incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for refund-double-charge MUST assert contract compatibility on every deploy window.

Knowledge base entries for refund-double-charge MUST include effective dates and document version stamps.
