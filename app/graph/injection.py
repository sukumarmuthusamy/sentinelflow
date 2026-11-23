"""Deterministic screen for instruction-like text in retrieved (untrusted) content.

This is a coarse first line, not a classifier: it catches the obvious "ignore your
instructions" class of payload so such chunks never reach an agent prompt and never
become citable evidence. A hit can only make the gate stricter (REVIEW floor); it
never blocks on its own and never lowers a decision. The adversarial benchmark
suite is M7 work.
"""

from __future__ import annotations

import re

_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+"
        r"(?:instructions|prompts|rules)",
        r"\bdisregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|system)\b",
        r"\byou\s+are\s+now\b",
        r"\bsystem\s+prompt\b",
        r"\bnew\s+instructions\b",
        r"</?\s*(?:system|assistant|instructions)\s*>",
        r"\b(?:mark|label|classify)\s+(?:this|it|all|every)\b[^.]{0,40}\bas\s+"
        r"(?:supported|pass(?:ed)?|approved|safe)\b",
        r"\b(?:respond|reply|answer|output)\s+(?:only\s+)?with\s+(?:pass|approved|supported)\b",
    )
)


def instruction_like_spans(text: str) -> list[str]:
    """Return the matched instruction-like fragments (empty when clean)."""
    return [match.group(0) for pattern in _PATTERNS for match in pattern.finditer(text)]


def looks_like_injection(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PATTERNS)
