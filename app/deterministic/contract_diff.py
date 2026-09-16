"""Contract compatibility wrappers for OpenAPI and Protobuf."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.deterministic.models import ContractChange, ContractDiffResult
from app.deterministic.subprocess_util import command_available, docker_available, run_command


class OasDiffOptions(BaseModel):
    """Configuration for oasdiff execution."""

    docker_image: str = "tufin/oasdiff"
    timeout_seconds: int = 120


def _parse_oasdiff_output(stdout: str, stderr: str) -> ContractDiffResult:
    payload = stdout.strip() or stderr.strip()
    if not payload:
        return ContractDiffResult(tool="oasdiff")

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return _parse_oasdiff_text(payload)

    changes: list[ContractChange] = []
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            level = str(item.get("level", item.get("severity", "INFO"))).upper()
            if level not in {"ERR", "WARN", "INFO"}:
                level = "INFO"
            changes.append(
                ContractChange(
                    level=level,  # type: ignore[arg-type]
                    message=str(item.get("text", item.get("comment", item.get("message", "")))),
                    path=item.get("path") if isinstance(item.get("path"), str) else None,
                )
            )
    elif isinstance(parsed, dict) and "changes" in parsed and isinstance(parsed["changes"], list):
        for item in parsed["changes"]:
            if isinstance(item, dict):
                level = str(item.get("level", "INFO")).upper()
                changes.append(
                    ContractChange(
                        level=level if level in {"ERR", "WARN", "INFO"} else "INFO",  # type: ignore[arg-type]
                        message=str(item.get("text", "")),
                        path=item.get("path") if isinstance(item.get("path"), str) else None,
                    )
                )

    return _summarize_changes(changes, tool="oasdiff")


def _parse_oasdiff_text(payload: str) -> ContractDiffResult:
    changes: list[ContractChange] = []
    for line in payload.splitlines():
        match = re.match(r"^(ERR|WARN|INFO)\s*:\s*(.+)$", line.strip(), flags=re.IGNORECASE)
        if match:
            changes.append(
                ContractChange(
                    level=match.group(1).upper(),  # type: ignore[arg-type]
                    message=match.group(2).strip(),
                )
            )
    return _summarize_changes(changes, tool="oasdiff")


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
    """Run oasdiff via the official Docker image and return a typed verdict."""
    if not base_spec.is_file() or not revision_spec.is_file():
        msg = "OpenAPI spec paths must exist"
        raise FileNotFoundError(msg)

    opts = options or OasDiffOptions()
    mount_dir = base_spec.parent.resolve()
    argv = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{mount_dir}:/specs:ro",
        "-w",
        "/specs",
        opts.docker_image,
        "breaking",
        base_spec.name,
        revision_spec.name,
        "--format",
        "json",
    ]
    if not docker_available():
        return ContractDiffResult(tool="unavailable")

    result = run_command(argv, timeout_seconds=opts.timeout_seconds)
    return _parse_oasdiff_output(result.stdout, result.stderr)


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
