"""Streaming-aware, fail-closed redaction of live fragments (ADR-0003, review M1).

Every secret here is synthetic and generated per test. Assertions check that no
window of a secret reaches the output; they never print a configured value.
"""

from __future__ import annotations

import base64
import json
import random
import string
from collections.abc import Iterable, Sequence

import pytest
from app.core.settings import Settings
from app.jobs.redaction import (
    MAX_OPEN_BLOCK,
    MAX_OPEN_RUN,
    REDACTED,
    FragmentRedactor,
    configured_secret_values,
    strip_controls,
)

WINDOW = 8  # no 8-character piece of a secret may be emitted
# Built by concatenation so secret scanners do not flag the synthetic test blocks.
KEY_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
KEY_END = "-----END " + "PRIVATE KEY-----"


def _token(
    rng: random.Random, size: int, alphabet: str = string.ascii_letters + string.digits
) -> str:
    return "".join(rng.choice(alphabet) for _ in range(size))


def _jwt(rng: random.Random, payload_chars: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    body = _token(rng, payload_chars, string.ascii_letters + string.digits + "-_")
    signature = _token(rng, 43, string.ascii_letters + string.digits + "-_")
    return f"{header}.{body}.{signature}"


def _chunks(text: str, sizes: Iterable[int]) -> list[str]:
    pieces: list[str] = []
    index = 0
    iterator = iter(sizes)
    while index < len(text):
        size = next(iterator)
        pieces.append(text[index : index + size])
        index += size
    return pieces


def _fixed(size: int) -> Iterable[int]:
    while True:
        yield size


def _random_sizes(rng: random.Random, low: int = 1, high: int = 12) -> Iterable[int]:
    while True:
        yield rng.randint(low, high)


def _stream(
    text: str,
    sizes: Iterable[int],
    secrets: Sequence[str] = (),
) -> tuple[list[str], int]:
    """Feed ``text`` in chunks; return the emitted pieces and the peak held size."""
    stream = FragmentRedactor(secrets).stream()
    emitted: list[str] = []
    peak = 0
    for chunk in _chunks(text, sizes):
        emitted.append(stream.feed(chunk))
        peak = max(peak, stream.held)
    emitted.append(stream.finish())
    return emitted, peak


def _leaks(output: str, secret: str) -> bool:
    """True if any WINDOW-character piece of ``secret`` appears in ``output``."""
    if len(secret) < WINDOW:
        return secret in output
    return any(secret[i : i + WINDOW] in output for i in range(len(secret) - WINDOW + 1))


# -- review reproductions ---------------------------------------------------------------


def test_a_241_character_jwt_in_7_character_chunks_never_leaks() -> None:
    """Review M1a: the 128-character holdback released the JWT's first 113 characters."""
    rng = random.Random(1)
    jwt = _jwt(rng, 160)
    assert len(jwt) > 200
    text = f'{{"note": "session {jwt} expired", "next": "retry"}}'
    emitted, _ = _stream(text, _fixed(7))
    output = "".join(emitted)
    assert not _leaks(output, jwt)
    assert REDACTED in output
    assert output.endswith('expired", "next": "retry"}')


def test_a_146_character_configured_secret_never_leaks() -> None:
    """Review M1b: a configured value longer than the window leaked its prefix."""
    rng = random.Random(2)
    secret = _token(rng, 146, string.ascii_letters + string.digits + " !#&*")
    text = f"The configured value is {secret} and nothing else."
    emitted, _ = _stream(text, _fixed(7), secrets=[secret])
    output = "".join(emitted)
    assert not _leaks(output, secret)
    assert output == f"The configured value is {REDACTED} and nothing else."


# -- adversarial chunking ---------------------------------------------------------------


def _credential_corpus(rng: random.Random) -> list[str]:
    alnum = string.ascii_letters + string.digits
    return [
        "AIza" + _token(rng, 35, alnum + "_-"),
        "sk-or-v1-" + _token(rng, 64, "0123456789abcdef"),
        "ghp_" + _token(rng, 36, alnum),
        "github_pat_" + _token(rng, 82, alnum + "_"),
        _jwt(rng, 40),
        _jwt(rng, 900),
        "AKIA" + _token(rng, 16, string.ascii_uppercase + string.digits),
        _token(rng, 64, "0123456789abcdef"),
    ]


@pytest.mark.parametrize("seed", range(12))
def test_known_shapes_never_leak_under_random_chunking(seed: int) -> None:
    rng = random.Random(seed)
    for credential in _credential_corpus(rng):
        text = f"Checked the config: {credential}, then moved on."
        emitted, _ = _stream(text, _random_sizes(rng))
        output = "".join(emitted)
        assert not _leaks(output, credential), credential[:6]
        assert output.startswith("Checked the config: ")
        assert output.endswith(", then moved on.")


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13, 64])
def test_single_and_fixed_size_chunks(size: int) -> None:
    rng = random.Random(size)
    credentials = _credential_corpus(rng)
    text = " | ".join(credentials)
    emitted, _ = _stream(text, _fixed(size))
    output = "".join(emitted)
    for credential in credentials:
        assert not _leaks(output, credential)


def test_adjacent_credentials_are_all_redacted() -> None:
    rng = random.Random(3)
    first = "ghp_" + _token(rng, 36)
    second = _jwt(rng, 300)
    third = "AIza" + _token(rng, 35)
    configured = _token(rng, 40, string.ascii_lowercase)
    for text in (
        f"{first}{second}{third}{configured}",
        f"{first},{second};{third} {configured}",
        f'["{first}","{second}","{third}","{configured}"]',
    ):
        emitted, _ = _stream(text, _random_sizes(rng, 1, 9), secrets=[configured])
        output = "".join(emitted)
        for credential in (first, second, third, configured):
            assert not _leaks(output, credential)


def test_configured_secret_with_spaces_and_quotes_is_matched_json_escaped() -> None:
    rng = random.Random(4)
    secret = 'pa ss"' + _token(rng, 20) + "\\end"
    escaped = json.dumps(secret)[1:-1]
    for text in (f"raw {secret} done", json.dumps({"value": secret})):
        emitted, _ = _stream(text, _random_sizes(rng, 1, 5), secrets=[secret])
        output = "".join(emitted)
        assert not _leaks(output, secret)
        assert not _leaks(output, escaped)


def test_configured_secret_longer_than_the_run_cap_is_matched_whole() -> None:
    rng = random.Random(5)
    secret = _token(rng, MAX_OPEN_RUN + 500, string.ascii_lowercase + " ")
    text = f"before {secret} after"
    emitted, _ = _stream(text, _fixed(11), secrets=[secret])
    output = "".join(emitted)
    assert not _leaks(output, secret)
    assert output.startswith("before ")
    assert output.endswith(" after")


def test_url_and_keyword_credentials_are_redacted() -> None:
    rng = random.Random(6)
    url_password = _token(rng, 14)
    key_value = _token(rng, 12, string.ascii_lowercase)
    text = (
        f"DATABASE_URL=postgresql://svc:{url_password}@db.internal:5432/app\n"
        f'config = {{"api_key": "{key_value}", "password": "{key_value}x"}}\n'
        f"export SECRET={key_value}y and done"
    )
    emitted, _ = _stream(text, _random_sizes(rng, 1, 6))
    output = "".join(emitted)
    assert url_password not in output
    assert key_value not in output
    assert "db.internal:5432/app" in output
    assert output.endswith(" and done")


# -- fail-closed bounds -----------------------------------------------------------------


def test_an_endless_run_is_suppressed_and_memory_stays_bounded() -> None:
    rng = random.Random(7)
    endless = _token(rng, 50_000, string.ascii_lowercase)  # unclassifiable, never closes
    text = f"start {endless} end of text"
    emitted, peak = _stream(text, _fixed(97))
    output = "".join(emitted)
    assert output == f"start {REDACTED} end of text"
    assert peak <= MAX_OPEN_RUN + 97


def test_an_unterminated_private_key_block_is_never_shown() -> None:
    rng = random.Random(8)
    body = "\n".join(_token(rng, 64) for _ in range(200))  # larger than MAX_OPEN_BLOCK
    assert len(body) > MAX_OPEN_BLOCK
    text = f"key:\n{KEY_BEGIN}\n{body}\n{KEY_END}\nafter"
    emitted, peak = _stream(text, _random_sizes(rng, 20, 80))
    output = "".join(emitted)
    assert all(line not in output for line in body.splitlines())
    assert output.endswith("\nafter")
    assert peak <= MAX_OPEN_BLOCK + 80

    emitted, _ = _stream(f"key: {KEY_BEGIN}\n{body[:500]}", _fixed(9))
    output = "".join(emitted)
    assert all(line not in output for line in body[:500].splitlines() if len(line) >= WINDOW)
    assert output.endswith(REDACTED)


def test_long_unknown_runs_are_redacted_but_ordinary_words_pass() -> None:
    long_run = "a" * 250
    machine_like = "Zq8" * 12
    text = f"word {long_run} {machine_like} configuration-management_v2.1 end"
    emitted, _ = _stream(text, _fixed(4))
    output = "".join(emitted)
    assert output == f"word {REDACTED} {REDACTED} configuration-management_v2.1 end"


# -- safe content -----------------------------------------------------------------------


def test_safe_prose_streams_incrementally() -> None:
    stream = FragmentRedactor(["a-configured-value"]).stream()
    assert stream.feed("Release risk is ") == "Release risk is "
    assert stream.feed("moderate because") == "moderate "  # "because" may still grow
    assert stream.feed(" the claims ") == "because the claims "
    assert stream.finish() == ""


def test_structured_output_without_secrets_is_unchanged() -> None:
    payload = json.dumps(
        {
            "change_category": "documentation",
            "summary": "Updates the claims adjudication runbook (section 4.2) for v2.3.1.",
            "risks": [{"id": "R-1", "evidence": ["docs/runbook.md#L10-L42"], "score": 0.35}],
            "url": "https://example.com/docs/release-notes",
            "notes": "Token budget: 1,000 input tokens; no secrets involved.",
        },
        indent=2,
    )
    rng = random.Random(9)
    emitted, _ = _stream(payload, _random_sizes(rng), secrets=["unrelated-configured-value"])
    assert "".join(emitted) == payload


def test_control_and_format_characters_are_stripped() -> None:
    assert strip_controls("a‮b​c\x07d\n\te") == "abcd\n\te"


# -- configured sources -----------------------------------------------------------------


def test_configured_secret_values_cover_every_source(monkeypatch: pytest.MonkeyPatch) -> None:
    rng = random.Random(10)
    values = {name: _token(rng, 24) for name in ("pg", "url", "api", "gh", "gemini")}
    monkeypatch.setenv("SENTINELFLOW_GITHUB_TOKEN", values["gh"])
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        postgres_password=values["pg"],
        database_url=f"postgresql+psycopg://svc:{values['url']}@localhost:5432/app",
        api_key=f"  {values['api']}  ",
        gemini_api_key=values["gemini"],
    )
    found = set(configured_secret_values(settings))
    missing = [name for name, value in values.items() if value not in found]
    assert missing == []


def test_public_default_password_and_short_values_are_not_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SENTINELFLOW_GITHUB_TOKEN", raising=False)
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        postgres_password="sentinelflow",
        database_url="postgresql+psycopg://sentinelflow:sentinelflow@localhost:5432/sentinelflow",
        api_key="short",
        gemini_api_key=None,
        openrouter_api_key=None,
        voyage_api_key=None,
        qdrant_api_key=None,
    )
    assert configured_secret_values(settings) == []
    emitted, _ = _stream("SentinelFlow (sentinelflow) works", _fixed(3))
    assert "".join(emitted) == "SentinelFlow (sentinelflow) works"


# -- M5-B cases, ported to the streaming API ---------------------------------------------

M5B_SECRETS = [
    "AIza" + "Sy" + "C" * 33,
    "sk-or-v1-" + "a" * 48,
    "ghp_" + "Z" * 36,
    "github_pat_" + "9" * 60,
    "Bearer " + "t" * 40,
    "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20,
]


@pytest.mark.parametrize("secret", M5B_SECRETS)
@pytest.mark.parametrize("seed", range(5))
def test_credentials_split_across_chunks_never_leave(secret: str, seed: int) -> None:
    rng = random.Random(seed)
    text = '{"summary":"' + "x" * rng.randint(0, 300) + secret + " tail" + "y" * 200 + '"}'
    emitted = "".join(_stream(text, _random_sizes(rng))[0])
    assert secret not in emitted
    assert secret[-6:] not in emitted  # no partial tail leaks either
    assert REDACTED in emitted
    assert emitted.endswith('"}')


def test_configured_secret_values_are_redacted_whole() -> None:
    value = "plain-config-value-42"
    emitted = "".join(_stream(f"a {value} b" + "z" * 200, _fixed(3), secrets=[value])[0])
    assert value not in emitted
    # The 201-character padding run ("b" + 200 "z") is now redacted too (long-run rule).
    assert emitted.startswith(f"a {REDACTED} ")


def test_private_key_block_is_held_until_it_ends() -> None:
    stream = FragmentRedactor().stream()
    opened = f"{KEY_BEGIN}\n" + "Q" * 400
    assert stream.feed(opened) == ""  # the whole open block is held back
    assert stream.held == len(opened)
    closed = stream.feed(f"\n{KEY_END}done") + stream.finish()
    assert "Q" not in closed and closed == f"{REDACTED}done"


def test_text_without_secrets_passes_through_unchanged() -> None:
    text = '{"rationale":"documentation-only","agents":["change_analyst"]}' * 5
    assert "".join(_stream(text, _fixed(7))[0]) == text
