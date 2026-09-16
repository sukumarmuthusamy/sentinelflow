"""Shared subprocess helpers for external deterministic tools."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    """Result of an external command invocation."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def command_available(name: str) -> bool:
    """Return True when an executable is discoverable on PATH."""
    return shutil.which(name) is not None


def run_command(
    argv: list[str],
    *,
    timeout_seconds: int = 120,
    cwd: str | None = None,
) -> CommandResult:
    """Run a subprocess and capture stdout/stderr without raising."""
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        cwd=cwd,
        check=False,
    )
    return CommandResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def docker_available() -> bool:
    """Return True when the Docker CLI responds successfully."""
    if not command_available("docker"):
        return False
    result = run_command(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        timeout_seconds=15,
    )
    return result.ok


def load_json_file(path: str) -> object:
    """Load JSON from disk."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
