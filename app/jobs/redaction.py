"""Streaming-aware, fail-closed redaction of live model-output fragments (ADR-0003).

A credential can arrive split across any number of chunks, so a fragment is never
judged on its own. Each model response gets a ``StreamRedactor`` that buffers just
enough text to decide, and emits only what can no longer become part of a secret:

* **Open token runs.** A run of token characters (letters, digits, ``_-+/=.~%``) can
  still grow into a key or JWT until a delimiter (space, quote, comma, colon, ...)
  ends it. The open run at the end of the buffer is always held back. A closed
  run is then classified whole: known credential shapes are redacted, and so is any
  run that is long or looks machine-generated (mixed-case alphanumerics, long hex).
* **Configured secrets.** The longest buffer suffix that is a prefix of a
  configured secret (raw or JSON-escaped) is held back, whatever characters the
  secret contains, so a configured value is never emitted in part.
* **Credential keywords.** ``password``, ``api_key``, ``bearer``, ... at the end of
  the buffer are held with their separator until the value after them is complete,
  so ``"api_key": "<value>"`` is matched as one unit.
* **URL credentials.** ``//user:`` is held until the authority ends, so
  ``scheme://user:password@host`` is matched whole.
* **Private-key blocks** are held from ``-----BEGIN`` until their END line.

Bounded and fail-closed: when a held run would exceed ``MAX_OPEN_RUN`` characters
(or an open block ``MAX_OPEN_BLOCK``), the held text is replaced by one
``[REDACTED]`` marker and the rest of that run or block is discarded as it arrives,
instead of being released. Memory per response is therefore bounded by those caps
plus the longest configured secret.

Limits: the classification is pattern- and shape-based. It cannot recognise a
short secret with an unknown format, and configured values shorter than
``MIN_SECRET_CHARS`` are not matched (they would redact ordinary words).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

from app.core.settings import Settings

REDACTED = "[REDACTED]"
MIN_SECRET_CHARS = 8
MAX_OPEN_RUN = 1_024
MAX_SAFE_RUN = 200
MAX_OPEN_BLOCK = 8_192
# The documented local-development default (docker-compose, .env.example): not a
# secret, and matching it would redact the product name.
_PUBLIC_DEFAULT_PASSWORDS = frozenset({"sentinelflow"})

_RUN_CHARS = "A-Za-z0-9_\\-+/=.~%"
_RUN_CHAR = re.compile(f"[{_RUN_CHARS}]")
_OPEN_RUN = re.compile(f"[{_RUN_CHARS}]+\\Z")
_RUN = re.compile(f"[{_RUN_CHARS}]+")

_KEYWORDS = (
    r"password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key"
    r"|client[_-]?secret|private[_-]?key|authorization|bearer"
)
_KEYWORD_PENDING = re.compile(
    rf"(?i)(?<![A-Za-z0-9_])(?:{_KEYWORDS})[\"']?\s*(?:[:=]\s*[\"']?)?\Z",
)
# "//user:pa" at the end of the buffer may be the start of URL credentials.
_URL_PENDING = re.compile(r"//[^\s/@\"']*\Z")
# The Bearer shape has no left boundary ("xBearer <token>" still matches over
# complete text), so its hold has none either.
_BEARER_PENDING = re.compile(r"(?i)bearer\s*\Z")
_PENDING = (_KEYWORD_PENDING, _BEARER_PENDING, _URL_PENDING)
_BLOCK_START = "-----BEGIN"
_BLOCK_END = re.compile(r"-----END[ A-Z]*-----")

# Known credential shapes and keyword-value pairs, matched over complete text.
_SHAPES: tuple[str, ...] = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"AIza[0-9A-Za-z_\-]{30,}",  # Google API key
    r"sk-[A-Za-z0-9_\-]{16,}",  # OpenAI / OpenRouter style
    r"gh[pousr]_[A-Za-z0-9]{20,}",  # GitHub tokens
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"xox[abposr]-[A-Za-z0-9\-]{10,}",  # Slack
    r"AKIA[0-9A-Z]{16}",  # AWS access key id
    r"(?i:bearer)\s+[A-Za-z0-9._~+/\-]{8,}=*",
    r"eyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-.]*",  # JWT (any length, even unsigned)
    r"(?:[a-z][a-z0-9+.\-]*:)?//[^\s:/@\"']+:[^\s@/\"']+@",  # credentials in a URL
)
_KEYWORD_VALUE = re.compile(
    rf"(?i)((?<![A-Za-z0-9_])(?:{_KEYWORDS})[\"']?\s*[:=]\s*[\"']?)([^\s\"',;{{}}\[\]]{{6,}})",
)
_HEX = re.compile(r"[0-9a-fA-F]{32,}")


def strip_controls(text: str) -> str:
    """Drop control and format characters (bidi overrides, zero-width) but keep whitespace."""
    return "".join(
        char for char in text if char in "\n\r\t" or not unicodedata.category(char).startswith("C")
    )


def _sensitive_run(run: str) -> bool:
    """A complete token run that must not be shown: too long, or machine-like."""
    if len(run) > MAX_SAFE_RUN:
        return True
    core = run.strip("-_.=~%+/")
    if len(core) < 32:
        return False
    has_digit = any(char.isdigit() for char in core)
    has_upper = any(char.isupper() for char in core)
    has_lower = any(char.islower() for char in core)
    if has_digit and has_upper and has_lower:
        return True
    return bool(_HEX.fullmatch(core) and has_digit and any(char.isalpha() for char in core))


def _secret_variants(values: Iterable[str]) -> list[str]:
    """Each secret as it could appear in model output: raw and JSON-escaped."""
    variants: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if len(cleaned) < MIN_SECRET_CHARS:
            continue
        variants.add(cleaned)
        variants.add(json.dumps(cleaned)[1:-1])
    # Longest first, so the alternation never stops at a shorter overlapping value.
    return sorted(variants, key=len, reverse=True)


def configured_secret_values(settings: Settings) -> list[str]:
    """Secret values this process can reach, for exact-match redaction.

    Values are returned for matching only and are never logged. Sources: provider
    and service keys, the API key, the Postgres password, the password inside
    ``database_url``, and ``SENTINELFLOW_GITHUB_TOKEN`` (the same environment
    variable the MCP server's GitHub source reads).
    """
    values: list[str] = []
    for name in (
        "gemini_api_key",
        "openrouter_api_key",
        "voyage_api_key",
        "qdrant_api_key",
        "api_key",
        "postgres_password",
    ):
        secret = getattr(settings, name, None)
        if secret is not None:
            values.append(secret.get_secret_value())
    try:
        url_password = urlsplit(settings.database_url).password
    except ValueError:
        url_password = None
    if url_password:
        values.append(url_password)
    github_token = os.getenv("SENTINELFLOW_GITHUB_TOKEN")
    if github_token:
        values.append(github_token)
    return [
        value.strip()
        for value in values
        if len(value.strip()) >= MIN_SECRET_CHARS and value.strip() not in _PUBLIC_DEFAULT_PASSWORDS
    ]


class FragmentRedactor:
    """Compiled policy, shared by every response of one run."""

    def __init__(self, secret_values: Sequence[str] = ()) -> None:
        self.secrets = _secret_variants(secret_values)
        alternatives = [re.escape(value) for value in self.secrets] + list(_SHAPES)
        self.pattern = re.compile("|".join(f"(?:{item})" for item in alternatives))

    def stream(self) -> StreamRedactor:
        return StreamRedactor(self)

    def redact_complete(self, text: str) -> str:
        """Redact text that is known to be complete (no more chunks will follow)."""
        text = self.pattern.sub(REDACTED, text)
        text = _KEYWORD_VALUE.sub(lambda match: match.group(1) + REDACTED, text)
        return _RUN.sub(
            lambda match: REDACTED if _sensitive_run(match.group(0)) else match.group(0),
            text,
        )


class StreamRedactor:
    """Redaction state for one model response: ``feed`` chunks, then ``finish``."""

    def __init__(self, policy: FragmentRedactor) -> None:
        self._policy = policy
        self._buffer = ""
        # None, "run" (drop until a delimiter) or "block" (drop until an END line).
        self._discarding: str | None = None
        self._discard_tail = ""

    @property
    def held(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: str) -> str:
        chunk = self._skip_discarded(chunk)
        if not chunk:
            return ""
        self._buffer += chunk
        return self._drain()

    def finish(self) -> str:
        if self._discarding is not None:
            self._buffer = ""
            return ""
        text, self._buffer = self._buffer, ""
        block = _open_block_start(text)
        if block is not None:
            # An unterminated key block at the end of a response: never shown.
            return self._policy.redact_complete(text[:block]) + REDACTED
        return self._policy.redact_complete(text)

    # -- internals --------------------------------------------------------------------

    def _skip_discarded(self, chunk: str) -> str:
        if self._discarding == "run":
            index = 0
            while index < len(chunk) and _RUN_CHAR.match(chunk[index]):
                index += 1
            if index == len(chunk):
                return ""
            self._discarding = None
            return chunk[index:]
        if self._discarding == "block":
            window = self._discard_tail + chunk
            end = _BLOCK_END.search(window)
            if end is None:
                self._discard_tail = window[-64:]
                return ""
            self._discarding = None
            self._discard_tail = ""
            return window[end.end() :]
        return chunk

    def _cut(self, text: str) -> tuple[int, str | None]:
        """Where safe text ends, and which hold (if any) produced the cut."""
        cut = len(text)
        reason: str | None = None
        open_run = _OPEN_RUN.search(text)
        if open_run is not None:
            cut, reason = open_run.start(), "run"
        for secret in self._policy.secrets:
            for size in range(min(len(secret), len(text)), 0, -1):
                if text.endswith(secret[:size]):
                    if len(text) - size < cut:
                        cut, reason = len(text) - size, "secret"
                    break
        block = _open_block_start(text)
        if block is not None and block < cut:
            cut, reason = block, "block"
        # Repeat until stable: moving the cut can expose another pending keyword,
        # and the cut must never split a complete match or a token run.
        moved = True
        while moved:
            moved = False
            for pending in _PENDING:
                found = pending.search(text, 0, cut)
                if found is not None and found.start() < cut:
                    cut, moved = found.start(), True
                    reason = reason or "run"
            for match in self._policy.pattern.finditer(text):
                if match.start() < cut < match.end():
                    cut, moved = match.start(), True
            while (
                0 < cut < len(text)
                and _RUN_CHAR.match(text[cut])
                and _RUN_CHAR.match(text[cut - 1])
            ):
                cut, moved = cut - 1, True
        return cut, reason

    def _drain(self) -> str:
        text = self._buffer
        cut, reason = self._cut(text)
        held = len(text) - cut
        limit = MAX_OPEN_BLOCK if reason == "block" else MAX_OPEN_RUN + self._longest_secret()
        if held > limit:
            # Fail closed: an unclassifiable, ever-growing sequence is suppressed.
            self._buffer = ""
            self._discarding = "block" if reason == "block" else "run"
            self._discard_tail = ""
            safe = self._policy.redact_complete(text[:cut])
            return safe + REDACTED
        self._buffer = text[cut:]
        return self._policy.redact_complete(text[:cut])

    def _longest_secret(self) -> int:
        return len(self._policy.secrets[0]) if self._policy.secrets else 0


def _open_block_start(text: str) -> int | None:
    start = text.rfind(_BLOCK_START)
    if start < 0:
        # A partial marker at the very end ("-----BEG") is held as an open run.
        return None
    if _BLOCK_END.search(text, start) is not None:
        return None
    return start
