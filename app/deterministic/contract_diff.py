"""Contract compatibility wrappers for OpenAPI and Protobuf."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.deterministic.docker_sandbox import Mount, image_present, run_sandboxed
from app.deterministic.models import ContractChange, ContractDiffResult
from app.deterministic.subprocess_util import command_available, run_command

# Pinned by registry digest (OCI image index): tufin/oasdiff, build 059301e. To
# install: docker pull tufin/oasdiff@sha256:e6afae33... (the full reference below).
OASDIFF_IMAGE = (
    "tufin/oasdiff@sha256:e6afae33552b0c2cbd1d00883cdbf03b8b9b05ebb4d2f8d8cb8392ddb48837d5"
)


class OasDiffOptions(BaseModel):
    """Configuration for oasdiff execution."""

    docker_image: str = OASDIFF_IMAGE
    timeout_seconds: int = 120


class OasDiffOutputError(ValueError):
    """oasdiff output that cannot be trusted as a verdict (never read as "clean")."""


_OASDIFF_NUMERIC_LEVELS = {"3": "ERR", "2": "WARN", "1": "INFO"}
_OASDIFF_LEVELS = {"ERR", "WARN", "INFO"}


def parse_oasdiff_output(stdout: str) -> ContractDiffResult:
    """Parse ``oasdiff breaking --format json`` output strictly.

    Accepted: a JSON list of change records (``[]`` means no differences), or an
    object whose ``changes`` is such a list. Each record must be an object with a
    recognised ``level`` (``severity``): 3/2/1 or ERR/WARN/INFO. Anything else,
    including empty, truncated or non-JSON output, raises ``OasDiffOutputError``.
    """
    payload = stdout.strip()
    if not payload:
        msg = "oasdiff produced no output"
        raise OasDiffOutputError(msg)
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        msg = "oasdiff output is not valid JSON"
        raise OasDiffOutputError(msg) from exc

    if isinstance(parsed, list):
        items: list[object] = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("changes"), list):
        items = parsed["changes"]
    else:
        msg = "oasdiff output has an unrecognised shape"
        raise OasDiffOutputError(msg)
    return _summarize_changes([_parse_change(item) for item in items], tool="oasdiff")


def _parse_change(item: object) -> ContractChange:
    if not isinstance(item, dict):
        msg = "oasdiff change record is not an object"
        raise OasDiffOutputError(msg)
    raw_level = item.get("level", item.get("severity"))
    if isinstance(raw_level, bool) or not isinstance(raw_level, (int, str)):
        msg = "oasdiff change record has no valid level"
        raise OasDiffOutputError(msg)
    level = str(raw_level).strip().upper()
    # oasdiff's JSON encodes level as an integer (3=ERR, 2=WARN, 1=INFO).
    level = _OASDIFF_NUMERIC_LEVELS.get(level, level)
    if level not in _OASDIFF_LEVELS:
        msg = "oasdiff change record has an unknown level"
        raise OasDiffOutputError(msg)
    message = item.get("text", item.get("comment", item.get("message", "")))
    path = item.get("path")
    return ContractChange(
        level=level,  # type: ignore[arg-type]
        message=message if isinstance(message, str) else str(message),
        path=path if isinstance(path, str) else None,
    )


def _summarize_changes(
    changes: list[ContractChange],
    *,
    tool: Literal["oasdiff", "buf", "fixture", "unavailable"],
) -> ContractDiffResult:
    err_count = sum(1 for change in changes if change.level == "ERR")
    warn_count = sum(1 for change in changes if change.level == "WARN")
    info_count = sum(1 for change in changes if change.level == "INFO")
    return ContractDiffResult(
        tool=tool,
        err_count=err_count,
        warn_count=warn_count,
        info_count=info_count,
        changes=changes,
        breaking=err_count > 0,
    )


def run_oasdiff(
    base_spec: Path,
    revision_spec: Path,
    *,
    options: OasDiffOptions | None = None,
) -> ContractDiffResult:
    """Run oasdiff in a locked-down container; any failure is ``unavailable``."""
    if not base_spec.is_file() or not revision_spec.is_file():
        msg = "OpenAPI spec paths must exist"
        raise FileNotFoundError(msg)

    opts = options or OasDiffOptions()
    if not image_present(opts.docker_image):
        return ContractDiffResult(tool="unavailable")
    # Each spec's directory is mounted on its own: fixtures keep base and revision
    # in sibling directories under the same file name.
    result = run_sandboxed(
        image=opts.docker_image,
        command=[
            "breaking",
            f"/specs/base/{base_spec.name}",
            f"/specs/revision/{revision_spec.name}",
            "--format",
            "json",
        ],
        mounts=[
            Mount(base_spec.parent, "/specs/base"),
            Mount(revision_spec.parent, "/specs/revision"),
        ],
        timeout_seconds=opts.timeout_seconds,
    )
    if not result.ok:
        return ContractDiffResult(tool="unavailable")
    try:
        return parse_oasdiff_output(result.stdout)
    except OasDiffOutputError:
        return ContractDiffResult(tool="unavailable")


def run_buf_breaking(
    module_dir: Path,
    *,
    against_ref: str = ".git#branch=main",
) -> ContractDiffResult:
    """Run `buf breaking` when available.

    TODO(M2+): wire real protobuf fixtures once domain packs ship .proto assets.
    """
    if not command_available("buf"):
        return ContractDiffResult(tool="unavailable")

    if not module_dir.is_dir():
        return ContractDiffResult(tool="buf")

    argv = ["buf", "breaking", "--against", against_ref, str(module_dir)]
    result = run_command(argv, timeout_seconds=120)
    if result.ok:
        return ContractDiffResult(tool="buf", breaking=False)

    changes = [
        ContractChange(level="ERR", message=line.strip())
        for line in result.stderr.splitlines()
        if line.strip()
    ]
    return _summarize_changes(changes, tool="buf")


def load_contract_diff_fixture(path: Path) -> ContractDiffResult:
    """Load pre-recorded contract diff output for offline fixture evaluation."""
    data = json.loads(path.read_text(encoding="utf-8"))
    changes = [
        ContractChange.model_validate(item)
        for item in data.get("changes", [])
        if isinstance(item, dict)
    ]
    return ContractDiffResult(
        tool="fixture",
        err_count=int(data.get("err_count", 0)),
        warn_count=int(data.get("warn_count", 0)),
        info_count=int(data.get("info_count", 0)),
        changes=changes,
        breaking=bool(data.get("breaking", False)),
    )


def resolve_contract_diff(
    *,
    base_spec: Path | None,
    revision_spec: Path | None,
    fixture_signals: Path | None = None,
) -> ContractDiffResult:
    """Prefer live oasdiff; fall back to fixture signals when Docker is unavailable."""
    if base_spec and revision_spec:
        live = run_oasdiff(base_spec, revision_spec)
        if live.tool != "unavailable":
            return live
    if fixture_signals and fixture_signals.is_file():
        return load_contract_diff_fixture(fixture_signals)
    return ContractDiffResult(tool="unavailable")
