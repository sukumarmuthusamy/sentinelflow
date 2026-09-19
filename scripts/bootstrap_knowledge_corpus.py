#!/usr/bin/env python3
"""Generate synthetic knowledge corpora for both domain packs (M3)."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def md_doc(
    *,
    title: str,
    service: str,
    version: str,
    effective_date: str,
    body: str,
) -> str:
    return f"""---
service: {service}
version: {version}
effective_date: {effective_date}
---

# {title}

{body}
"""


def incident_doc(
    *,
    incident_id: str,
    service: str,
    version: str,
    effective_date: str,
    symptom: str,
    root_cause: str,
    impact: str,
    detection: str,
    remediation: str,
) -> str:
    return md_doc(
        title=f"Incident {incident_id}",
        service=service,
        version=version,
        effective_date=effective_date,
        body=f"""## Symptom
{symptom}

## Root cause
{root_cause}

## Impact
{impact}

## Detection
{detection}

## Remediation
{remediation}
""",
    )


def openapi_contract(*, service: str, title: str, version: str, effective_date: str) -> dict:
    return {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": version,
            "x-effective-date": effective_date,
            "description": f"Synthetic OpenAPI contract for {service}.",
        },
        "paths": {
            f"/v1/{service.replace('svc-', '')}/health": {
                "get": {
                    "operationId": f"{service}_health",
                    "summary": "Health check",
                    "responses": {"200": {"description": "OK"}},
                }
            },
            f"/v1/{service.replace('svc-', '')}/submit": {
                "post": {
                    "operationId": f"{service}_submit",
                    "summary": "Submit primary workflow payload",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SubmitRequest"}
                            }
                        },
                    },
                    "responses": {
                        "202": {"description": "Accepted"},
                        "409": {"description": "Conflict"},
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "SubmitRequest": {
                    "type": "object",
                    "required": ["correlation_id", "payload"],
                    "properties": {
                        "correlation_id": {"type": "string"},
                        "payload": {"type": "object"},
                        "priority": {"type": "string", "enum": ["normal", "urgent"]},
                    },
                },
                "ErrorResponse": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                    },
                },
            }
        },
    }


def paragraph_block(topic: str, *, sentences: int = 24) -> str:
    templates = [
        "This section defines operational expectations for {t}.",
        "Engineers MUST treat {t} as release-blocking when criticality is tagged critical.",
        "Changes touching {t} require evidence from requirements, contracts, and test plans.",
        "Release reviewers SHOULD verify blast radius against the dependency graph before approval.",
        "The platform team owns runbooks and incident history related to {t}.",
        "Security auditors MUST evaluate auth surface changes that intersect with {t}.",
        "Regression plans MUST include contract compatibility checks for {t} API changes.",
        "Evidence investigators SHOULD retrieve ADRs that constrain design choices for {t}.",
        "Policy overrides for verified secret exposure apply regardless of {t} risk score.",
        "Test strategists MUST map required tests to services impacted by {t} modifications.",
        "Documentation-only edits still require citation coverage when they alter {t} semantics.",
        "All {t} artifacts carry version metadata and effective dates for auditability.",
        "On-call engineers MUST link {t} remediation steps to prior incidents with matching signatures.",
        "Product managers SHOULD annotate {t} acceptance criteria with measurable rollback triggers.",
        "SREs MUST keep {t} runbooks synchronized with dependency graph changes each sprint.",
        "Compliance reviewers REQUIRE explicit data-classification tags wherever {t} stores payloads.",
        "Performance tests for {t} MUST include cold-start and peak-traffic scenarios in CI.",
        "Feature flags affecting {t} MUST default to safe mode during staged rollouts.",
        "Audit logs for {t} MUST retain correlation identifiers for at least ninety days.",
        "Change tickets referencing {t} MUST list impacted services and required regression tests.",
        "Architecture reviews for {t} SHOULD cite ADRs before approving cross-service coupling.",
        "Error budgets consumed by {t} incidents trigger automatic REVIEW on subsequent releases.",
        "Synthetic monitors for {t} MUST assert contract compatibility on every deploy window.",
        "Knowledge base entries for {t} MUST include effective dates and document version stamps.",
    ]
    lines = [template.format(t=topic) for template in templates[:sentences]]
    return "\n\n".join(lines)


def build_healthcare(root: Path) -> None:
    pack = root / "domains/healthcare-claims/knowledge"
    services = ["svc-core-api", "svc-notification", "svc-analytics"]
    req_topics = [
        ("claim-intake-idempotency", "svc-core-api", "Duplicate claim submissions must be rejected."),
        ("payer-remittance-window", "svc-core-api", "Remittance files must post within SLA windows."),
        ("notification-retry-policy", "svc-notification", "Patient notifications require exponential backoff."),
        ("analytics-phi-boundary", "svc-analytics", "Analytics exports must strip regulated-adjacent fields."),
        ("release-evidence-floor", "svc-core-api", "Evidence coverage below 0.75 forces REVIEW."),
        ("contract-breaking-block", "svc-core-api", "oasdiff ERR on critical services forces BLOCK."),
        ("adjudication-timeout", "svc-core-api", "Adjudication workflows must fail closed after SLA timeout."),
        ("prior-auth-bridge", "svc-core-api", "Prior authorization codes must map to payer-specific catalogs."),
        ("member-eligibility-cache", "svc-core-api", "Eligibility lookups must respect cache invalidation events."),
        ("clinical-code-versioning", "svc-analytics", "Clinical code sets require versioned migration plans."),
    ]
    for idx, (slug, service, summary) in enumerate(req_topics, start=1):
        write(
            pack / f"requirements/{slug}.md",
            md_doc(
                title=slug.replace("-", " ").title(),
                service=service,
                version=f"1.{idx}.0",
                effective_date="2025-11-01",
                body=f"## Summary\n{summary}\n\n## Details\n{paragraph_block(slug)}",
            ),
        )

    for service in services:
        contract = openapi_contract(
            service=service,
            title=f"{service} contract",
            version="2.1.0",
            effective_date="2025-10-15",
        )
        write(
            pack / f"contracts/{service}.yaml",
            yaml.safe_dump(contract, sort_keys=False),
        )

    adrs = [
        ("adr-001-async-notification", "svc-notification", "Use async fan-out for notification delivery."),
        ("adr-002-graph-provenance", "svc-core-api", "Tag dependency edges with grimp vs tree-sitter provenance."),
        ("adr-003-evidence-ids", "svc-core-api", "Stable document and section IDs for citation validation."),
        ("adr-004-sandbox-tests", "svc-core-api", "Sandboxed test execution with dry-run default."),
        ("adr-005-hybrid-retrieval", "svc-analytics", "Hybrid retrieval with reranking for incident similarity."),
    ]
    for slug, service, decision in adrs:
        write(
            pack / f"adrs/{slug}.md",
            md_doc(
                title=slug.upper(),
                service=service,
                version="1.0.0",
                effective_date="2025-09-01",
                body=f"## Decision\n{decision}\n\n## Context\n{paragraph_block(slug)}",
            ),
        )

    for idx, service in enumerate(services, start=1):
        write(
            pack / f"test-plans/regression-{service}.md",
            md_doc(
                title=f"Regression plan for {service}",
                service=service,
                version=f"3.{idx}.0",
                effective_date="2025-12-01",
                body=f"## Required tests\n{paragraph_block('regression coverage')}",
            ),
        )

    policies = [
        ("release-thresholds", "PASS below 40, REVIEW below 70, BLOCK otherwise."),
        ("override-matrix", "Verified secret exposure and critical contract breaks force BLOCK."),
        ("evidence-coverage", "Coverage floor 0.75 applies before PASS verdict."),
    ]
    for slug, summary in policies:
        write(
            pack / f"policies/{slug}.md",
            md_doc(
                title=slug.replace("-", " ").title(),
                service="svc-core-api",
                version="1.0.0",
                effective_date="2025-08-01",
                body=f"## Policy\n{summary}\n\n## Rationale\n{paragraph_block(slug)}",
            ),
        )

    runbooks = [
        ("runbook-core-api-failover", "svc-core-api"),
        ("runbook-notification-backlog", "svc-notification"),
        ("runbook-analytics-lag", "svc-analytics"),
        ("runbook-claim-replay", "svc-core-api"),
    ]
    for slug, service in runbooks:
        write(
            pack / f"runbooks/{slug}.md",
            md_doc(
                title=slug.replace("-", " ").title(),
                service=service,
                version="2.0.0",
                effective_date="2025-07-01",
                body=f"## Steps\n{paragraph_block(slug)}",
            ),
        )

    incidents = [
        (
            "INC-HC-2024-011",
            "svc-core-api",
            "Duplicate claim IDs accepted during peak load.",
            "Missing idempotency key validation on submit endpoint.",
            "12% duplicate adjudication attempts for 45 minutes.",
            "PagerDuty alert on error rate SLO burn.",
            "Deploy hotfix enforcing correlation_id uniqueness.",
        ),
        (
            "INC-HC-2024-019",
            "svc-notification",
            "Notification retries exhausted for payer updates.",
            "Downstream queue consumer stalled after config drift.",
            "Delayed remittance notices for two payer groups.",
            "Synthetic canary stopped receiving webhook callbacks.",
            "Replay backlog with capped concurrency and patched consumer.",
        ),
        (
            "INC-HC-2025-003",
            "svc-analytics",
            "Analytics export included regulated-adjacent fields.",
            "Feature flag enabled new column without redaction filter.",
            "Internal audit flagged one export batch.",
            "Data governance scanner matched PHI-adjacent pattern.",
            "Disable flag, purge batch, add regression test for redaction.",
        ),
        (
            "INC-HC-2025-008",
            "svc-core-api",
            "Contract break deployed without oasdiff gate.",
            "Required field removed from SubmitRequest schema.",
            "Mobile clients received 409 responses.",
            "Client error budget burn alert.",
            "Rollback revision and add contract diff to release checklist.",
        ),
        (
            "INC-HC-2025-012",
            "svc-core-api",
            "Verified secret exposure in configuration module.",
            "API key committed in config.py during hotfix branch.",
            "Secret rotated; no external exfiltration detected.",
            "gitleaks CI gate on pull request.",
            "Block release, rotate secret, add pre-commit hook.",
        ),
    ]
    for incident_id, service, symptom, root, impact, detection, remediation in incidents:
        write(
            pack / f"incidents/{incident_id}.md",
            incident_doc(
                incident_id=incident_id,
                service=service,
                version="1.0.0",
                effective_date="2025-06-15",
                symptom=symptom,
                root_cause=root,
                impact=impact,
                detection=detection,
                remediation=remediation,
            ),
        )

    for service in services:
        write(
            pack / f"ownership/{service}.md",
            md_doc(
                title=f"Service ownership — {service}",
                service=service,
                version="1.0.0",
                effective_date="2025-05-01",
                body=f"## Ownership\n{paragraph_block(service + ' ownership')}",
            ),
        )


def build_ecommerce(root: Path) -> None:
    pack = root / "domains/ecommerce-payments/knowledge"
    services = ["svc-payment-api", "svc-inventory", "svc-order-events"]
    req_topics = [
        ("checkout-idempotency", "svc-payment-api", "Checkout sessions must survive client retries."),
        ("pci-logging-ban", "svc-payment-api", "Never log primary account numbers or CVV data."),
        ("inventory-reservation", "svc-inventory", "Cart reservations expire after 15 minutes."),
        ("order-event-ordering", "svc-order-events", "Order events require partition-key ordering."),
        ("refund-double-charge", "svc-payment-api", "Refund API must be idempotent per payment intent."),
        ("fraud-signal-ingest", "svc-payment-api", "Fraud signals require signed webhook payloads."),
        ("settlement-batch-window", "svc-payment-api", "Settlement batches must close before reconciliation."),
        ("gift-card-liability", "svc-payment-api", "Gift card balances require double-entry ledger entries."),
        ("warehouse-pick-sla", "svc-order-events", "Pick events must publish within two seconds of scan."),
        ("catalog-price-drift", "svc-inventory", "Price changes require versioned catalog snapshots."),
    ]
    for idx, (slug, service, summary) in enumerate(req_topics, start=1):
        write(
            pack / f"requirements/{slug}.md",
            md_doc(
                title=slug.replace("-", " ").title(),
                service=service,
                version=f"1.{idx}.0",
                effective_date="2025-11-01",
                body=f"## Summary\n{summary}\n\n## Details\n{paragraph_block(slug)}",
            ),
        )

    for service in services:
        contract = openapi_contract(
            service=service,
            title=f"{service} contract",
            version="3.0.0",
            effective_date="2025-10-20",
        )
        write(
            pack / f"contracts/{service}.yaml",
            yaml.safe_dump(contract, sort_keys=False),
        )

    adrs = [
        ("adr-101-tokenization-boundary", "svc-payment-api", "Tokenization stays in payment API boundary."),
        ("adr-102-cart-saga", "svc-order-events", "Order fulfillment uses compensating saga steps."),
        ("adr-103-inventory-lock", "svc-inventory", "Optimistic locking for inventory counts."),
        ("adr-104-3ds-stepup", "svc-payment-api", "3DS step-up required for high-risk carts."),
        ("adr-105-release-gate", "svc-payment-api", "Deterministic risk engine owns PASS/REVIEW/BLOCK."),
    ]
    for slug, service, decision in adrs:
        write(
            pack / f"adrs/{slug}.md",
            md_doc(
                title=slug.upper(),
                service=service,
                version="1.0.0",
                effective_date="2025-09-01",
                body=f"## Decision\n{decision}\n\n## Context\n{paragraph_block(slug)}",
            ),
        )

    for idx, service in enumerate(services, start=1):
        write(
            pack / f"test-plans/regression-{service}.md",
            md_doc(
                title=f"Regression plan for {service}",
                service=service,
                version=f"4.{idx}.0",
                effective_date="2025-12-01",
                body=f"## Required tests\n{paragraph_block('payment regression')}",
            ),
        )

    policies = [
        ("payments-release-policy", "Critical payment contract breaks force BLOCK."),
        ("pci-adjacent-handling", "PCI-adjacent data never enters prompts or logs."),
        ("refund-approval", "Large refunds require human approval interrupt."),
    ]
    for slug, summary in policies:
        write(
            pack / f"policies/{slug}.md",
            md_doc(
                title=slug.replace("-", " ").title(),
                service="svc-payment-api",
                version="1.0.0",
                effective_date="2025-08-01",
                body=f"## Policy\n{summary}\n\n## Rationale\n{paragraph_block(slug)}",
            ),
        )

    runbooks = [
        ("runbook-payment-outage", "svc-payment-api"),
        ("runbook-inventory-oversell", "svc-inventory"),
        ("runbook-order-replay", "svc-order-events"),
        ("runbook-webhook-storm", "svc-payment-api"),
    ]
    for slug, service in runbooks:
        write(
            pack / f"runbooks/{slug}.md",
            md_doc(
                title=slug.replace("-", " ").title(),
                service=service,
                version="2.0.0",
                effective_date="2025-07-01",
                body=f"## Steps\n{paragraph_block(slug)}",
            ),
        )

    incidents = [
        (
            "INC-EC-2024-044",
            "svc-payment-api",
            "Double capture reported for same payment intent.",
            "Idempotency key ignored on capture endpoint regression.",
            "37 duplicate captures before rollback.",
            "Finance reconciliation alert on capture mismatch.",
            "Rollback deploy and add capture idempotency integration test.",
        ),
        (
            "INC-EC-2024-051",
            "svc-inventory",
            "Oversell during flash sale event.",
            "Reservation TTL not enforced on hot SKU path.",
            "142 orders cancelled post-checkout.",
            "Support ticket spike and inventory negative counts.",
            "Enable reservation expiry worker and add load test.",
        ),
        (
            "INC-EC-2025-006",
            "svc-order-events",
            "Out-of-order fulfillment messages.",
            "Partition key changed on order update event.",
            "Warehouse picked wrong items for 18 orders.",
            "Consumer lag dashboard and DLQ inspection.",
            "Revert schema, replay events with stable partition key.",
        ),
        (
            "INC-EC-2025-011",
            "svc-payment-api",
            "3DS step-up skipped for high-risk carts.",
            "Feature flag misconfigured in risk routing table.",
            "Elevated chargeback rate for 72 hours.",
            "Fraud ops dashboard threshold crossed.",
            "Force step-up flag on, add policy test for routing table.",
        ),
        (
            "INC-EC-2025-015",
            "svc-payment-api",
            "Webhook signature validation disabled in staging leak.",
            "Copy-paste config promoted to prod canary.",
            "No fraudulent orders; audit finding only.",
            "Security scanner flagged unsigned webhook acceptance.",
            "Block promotion, rotate signing keys, add config guard test.",
        ),
    ]
    for incident_id, service, symptom, root, impact, detection, remediation in incidents:
        write(
            pack / f"incidents/{incident_id}.md",
            incident_doc(
                incident_id=incident_id,
                service=service,
                version="1.0.0",
                effective_date="2025-06-15",
                symptom=symptom,
                root_cause=root,
                impact=impact,
                detection=detection,
                remediation=remediation,
            ),
        )

    for service in services:
        write(
            pack / f"ownership/{service}.md",
            md_doc(
                title=f"Service ownership — {service}",
                service=service,
                version="1.0.0",
                effective_date="2025-05-01",
                body=f"## Ownership\n{paragraph_block(service + ' ownership')}",
            ),
        )


def main() -> None:
    build_healthcare(ROOT)
    build_ecommerce(ROOT)
    print("Knowledge corpora generated under domains/*/knowledge/")


if __name__ == "__main__":
    main()
