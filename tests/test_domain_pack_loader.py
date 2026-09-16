"""Domain pack loading tests."""

from pathlib import Path

import pytest
from app.core.domain_pack import load_domain_pack, resolve_default_domain_pack_id
from app.core.settings import get_settings

DOMAINS_ROOT = Path(__file__).resolve().parents[1] / "domains"


@pytest.mark.parametrize(
    "pack_id",
    ["healthcare-claims", "ecommerce-payments"],
)
def test_domain_pack_loads_and_validates(pack_id: str) -> None:
    pack = load_domain_pack(pack_id, domains_root=DOMAINS_ROOT)
    assert pack.pack_id == pack_id
    assert pack.domain.pack_id == pack_id
    assert len(pack.domain.services) >= 1
    assert len(pack.dependencies.nodes) >= 1
    assert pack.policy.evidence_floor > 0
    assert len(pack.policy.default_plan) >= 1


def test_default_settings_domains_root_points_at_repo() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.domains_root == DOMAINS_ROOT
    default_pack_id = resolve_default_domain_pack_id(domains_root=DOMAINS_ROOT)
    assert load_domain_pack(default_pack_id, domains_root=settings.domains_root)
