---
service: svc-payment-api
version: 1.0.0
effective_date: 2025-09-01
---

# ADR-104-3DS-STEPUP

## Decision
3DS step-up required for high-risk carts.

## Context
This section defines operational expectations for adr-104-3ds-stepup.

Engineers MUST treat adr-104-3ds-stepup as release-blocking when criticality is tagged critical.

Changes touching adr-104-3ds-stepup require evidence from requirements, contracts, and test plans.

Release reviewers SHOULD verify blast radius against the dependency graph before approval.

The platform team owns runbooks and incident history related to adr-104-3ds-stepup.

Security auditors MUST evaluate auth surface changes that intersect with adr-104-3ds-stepup.

Regression plans MUST include contract compatibility checks for adr-104-3ds-stepup API changes.

Evidence investigators SHOULD retrieve ADRs that constrain design choices for adr-104-3ds-stepup.

Policy overrides for verified secret exposure apply regardless of adr-104-3ds-stepup risk score.

Test strategists MUST map required tests to services impacted by adr-104-3ds-stepup modifications.

Documentation-only edits still require citation coverage when they alter adr-104-3ds-stepup semantics.

All adr-104-3ds-stepup artifacts carry version metadata and effective dates for auditability.

On-call engineers MUST link adr-104-3ds-stepup remediation steps to prior incidents with matching signatures.

Product managers SHOULD annotate adr-104-3ds-stepup acceptance criteria with measurable rollback triggers.

SREs MUST keep adr-104-3ds-stepup runbooks synchronized with dependency graph changes each sprint.

Compliance reviewers REQUIRE explicit data-classification tags wherever adr-104-3ds-stepup stores payloads.

Performance tests for adr-104-3ds-stepup MUST include cold-start and peak-traffic scenarios in CI.

Feature flags affecting adr-104-3ds-stepup MUST default to safe mode during staged rollouts.

Audit logs for adr-104-3ds-stepup MUST retain correlation identifiers for at least ninety days.

Change tickets referencing adr-104-3ds-stepup MUST list impacted services and required regression tests.

Architecture reviews for adr-104-3ds-stepup SHOULD cite ADRs before approving cross-service coupling.

Error budgets consumed by adr-104-3ds-stepup incidents trigger automatic REVIEW on subsequent releases.

Synthetic monitors for adr-104-3ds-stepup MUST assert contract compatibility on every deploy window.

Knowledge base entries for adr-104-3ds-stepup MUST include effective dates and document version stamps.
