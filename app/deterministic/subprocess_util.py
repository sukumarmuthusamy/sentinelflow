"""Shared subprocess helpers for external deterministic tools."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import PurePath


@dataclass(frozen=True)
class CommandResult:
    """Result of an external command invocation."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    # Set by run_command_safe: the command hit its timeout, or could not be started.
    timed_out: bool = False
    launch_failed: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.launch_failed


def command_available(name: str) -> bool:
    """Return True when an executable is discoverable on PATH."""
    return shutil.which(name) is not None


def run_command(
    argv: list[str],
    *,
    timeout_seconds: int = 120,
    cwd: str | None = None,
) -> CommandResult:
    """Run a subprocess and capture stdout/stderr.

    A non-zero exit is returned, not raised. ``subprocess.TimeoutExpired`` and
    ``OSError`` (executable missing, not runnable) do propagate; use
    ``run_command_safe`` where a timeout must become a result instead.
    """
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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


def run_command_safe(
    argv: list[str],
    *,
    timeout_seconds: int = 120,
    cwd: str | None = None,
) -> CommandResult:
    """Like ``run_command``, but a timeout or launch failure becomes a failed result.

    The result's stderr then holds a fixed message naming only the executable, so
    no argument (paths, tokens) is echoed.
    """
    program = PurePath(argv[0]).name if argv else "command"
    try:
        return run_command(argv, timeout_seconds=timeout_seconds, cwd=cwd)
    except subprocess.TimeoutExpired:
        return CommandResult(
            argv=argv,
            returncode=-1,
            stdout="",
            stderr=f"{program} timed out after {timeout_seconds}s",
            timed_out=True,
        )
    except OSError as exc:
        return CommandResult(
            argv=argv,
            returncode=-1,
            stdout="",
            stderr=f"{program} could not be started ({type(exc).__name__})",
            launch_failed=True,
        )


def docker_available() -> bool:
    """Return True when the Docker CLI responds successfully."""
    if not command_available("docker"):
        return False
    result = run_command_safe(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        timeout_seconds=15,
    )
    return result.ok


def load_json_file(path: str) -> object:
    """Load JSON from disk."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
