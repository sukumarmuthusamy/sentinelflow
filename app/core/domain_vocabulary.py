"""Central registry of domain-specific terms forbidden in application code."""

from __future__ import annotations

# Terms that must appear only under domains/, never in app/ source.
# Extend this list when new domain packs introduce vocabulary.
FORBIDDEN_DOMAIN_TERMS: frozenset[str] = frozenset(
    {
        # healthcare-claims pack
        "claim",
        "claims",
        "payer",
        "payers",
        "remittance",
        "remittances",
        "diagnosis",
        "diagnoses",
        "hipaa",
        "eob",
        # ecommerce-payments pack
        "checkout",
        "cart",
        "sku",
        "skus",
        "merchant",
        "merchants",
        "chargeback",
        "chargebacks",
        "fulfillment",
    }
)
