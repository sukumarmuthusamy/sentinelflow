"""Ensure domain-specific vocabulary does not leak into application code."""

from __future__ import annotations

import re
from pathlib import Path

from app.core.domain_vocabulary import FORBIDDEN_DOMAIN_TERMS

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
SCAN_EXTENSIONS = {".py"}
EXCLUDED_RELATIVE_PATHS = {
    Path("core/domain_vocabulary.py"),
}


def _iter_app_source_files() -> list[Path]:
    return sorted(
        path for path in APP_ROOT.rglob("*") if path.suffix in SCAN_EXTENSIONS and path.is_file()
    )


def _find_forbidden_terms(content: str, term: str) -> list[int]:
    pattern = re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE)
    return [match.start() for match in pattern.finditer(content)]


def test_app_code_has_no_domain_vocabulary_leaks() -> None:
    violations: list[str] = []

    for path in _iter_app_source_files():
        relative = path.relative_to(APP_ROOT)
        if relative in EXCLUDED_RELATIVE_PATHS:
            continue
        text = path.read_text(encoding="utf-8")
        for term in FORBIDDEN_DOMAIN_TERMS:
            if _find_forbidden_terms(text, term):
                violations.append(f"{path.relative_to(APP_ROOT.parent)} contains '{term}'")

    assert not violations, "Domain vocabulary leaked into app/:\n" + "\n".join(violations)
