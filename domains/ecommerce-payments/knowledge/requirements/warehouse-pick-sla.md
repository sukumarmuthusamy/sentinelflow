---
service: svc-order-events
version: 1.9.0
effective_date: 2025-11-01
---

# Warehouse Pick Sla

## Summary
Pick events must publish within two seconds of scan.

## Details
This section defines operational expectations for warehouse-pick-sla.

Engineers MUST treat warehouse-pick-sla as release-blocking when criticality is tagged critical.

Changes touching warehouse-pick-sla require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to warehouse-pick-sla.

Security auditors MUST evaluate auth surface changes that intersect with warehouse-pick-sla.

Regression plans MUST include contract compatibility checks for warehouse-pick-sla API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for warehouse-pick-sla.

Policy overrides for verified secret exposure apply regardless of warehouse-pick-sla risk score.

Test strategists MUST map required tests to services impacted by warehouse-pick-sla modifications.

Documentation-only edits still require citation coverage when they alter warehouse-pick-sla semantics.

All warehouse-pick-sla artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link warehouse-pick-sla remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate warehouse-pick-sla acceptance criteria with measurable rollback triggers.

SREs MUST keep warehouse-pick-sla runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever warehouse-pick-sla stores payloads.

Performance tests for warehouse-pick-sla MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting warehouse-pick-sla MUST default to safe mode during staged rollouts.

Audit logs for warehouse-pick-sla MUST retain correlation identifiers for at least ninety days.

Change tickets referencing warehouse-pick-sla MUST list impacted services and required regression tests.

Architecture reviews for warehouse-pick-sla SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by warehouse-pick-sla incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for warehouse-pick-sla MUST assert contract compatibility on every deploy window.

Knowledge base entries for warehouse-pick-sla MUST include effective dates and document version stamps.
