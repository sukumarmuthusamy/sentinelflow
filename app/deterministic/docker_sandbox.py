"""Locked-down, disposable Docker containers for the external scanners.

Every scanner container:

- runs only an image that is already present (``--pull=never``), referenced by digest;
- has no network, a read-only root filesystem, no capabilities, no privilege
  escalation, and bounded processes, memory and CPU;
- runs as an unprivileged user, with every input mounted read-only;
- gets a unique name (``sf-scan-<uuid>``) and is force-removed by that exact name
  after every run, so a run that times out, is cancelled or crashes leaves nothing
  behind. Unrelated containers are never touched.

Cleanup is best effort. A failed removal is logged with the container name.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.deterministic.subprocess_util import CommandResult, docker_available, run_command_safe

logger = logging.getLogger(__name__)

SCANNER_DOCKER_FLAGS: tuple[str, ...] = (
    "--network=none",
    "--pull=never",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--pids-limit=64",
    "--memory=512m",
    "--cpus=1",
)
CONTAINER_NAME_PREFIX = "sf-scan-"
CONTAINER_LABEL = "org.sentinelflow.role=scanner"
# The fallback user when the host has no POSIX uid (Windows; Docker Desktop
# bind mounts do not enforce host permissions).
UNPRIVILEGED_USER = "65534:65534"
CLEANUP_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Mount:
    """A host directory bind-mounted into the scanner container."""

    source: Path
    target: str
    read_only: bool = True


def container_user() -> str:
    """The uid:gid the container runs as.

    On POSIX hosts the container takes the host user's ids, so that it can read the
    0700 temporary directories created for it. It still gets no capabilities.
    """
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return UNPRIVILEGED_USER
    return f"{getuid()}:{getgid()}"


def image_present(image: str) -> bool:
    """True when ``image`` (a pinned reference) exists locally. Never pulls."""
    if not docker_available():
        return False
    return run_command_safe(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        timeout_seconds=CLEANUP_TIMEOUT_SECONDS,
    ).ok


def new_container_name() -> str:
    return f"{CONTAINER_NAME_PREFIX}{uuid.uuid4().hex}"


def sandbox_argv(
    *,
    name: str,
    image: str,
    command: Sequence[str],
    mounts: Sequence[Mount],
    workdir: str | None = None,
) -> list[str]:
    """``docker create`` arguments for a locked-down scanner container."""
    argv = [
        "docker", "create", "--rm", "--name", name, "--label", CONTAINER_LABEL,
        *SCANNER_DOCKER_FLAGS, f"--user={container_user()}",
    ]  # fmt: skip
    for mount in mounts:
        suffix = ":ro" if mount.read_only else ""
        argv += ["-v", f"{mount.source.resolve()}:{mount.target}{suffix}"]
    if workdir is not None:
        argv += ["-w", workdir]
    return [*argv, image, *command]


_ALREADY_GONE = ("no such container", "is already in progress")


def remove_container(name: str) -> bool:
    """Force-remove the scanner container ``name``. True if it is gone."""
    if not name.startswith(CONTAINER_NAME_PREFIX):
        msg = "refusing to remove a container that is not a SentinelFlow scanner"
        raise ValueError(msg)
    result = run_command_safe(
        ["docker", "rm", "--force", name],
        timeout_seconds=CLEANUP_TIMEOUT_SECONDS,
    )
    if result.ok or any(marker in result.stderr.lower() for marker in _ALREADY_GONE):
        return True
    reason = "timed out" if result.timed_out else f"exit {result.returncode}"
    logger.warning("scanner container %s could not be removed (%s)", name, reason)
    return False


def run_sandboxed(
    *,
    image: str,
    command: Sequence[str],
    mounts: Sequence[Mount],
    timeout_seconds: int,
    workdir: str | None = None,
) -> CommandResult:
    """Run ``command`` in a fresh locked-down container and return its result.

    The container is created first (``docker create`` returns only once it exists)
    and then started attached, so it always exists under its name by the time the
    scanner can time out. A timeout comes back as ``timed_out=True``, not as an
    exception. The container is force-removed afterwards in every case: after a
    timeout, a failure or any interruption (including cancellation). On a normal
    exit ``--rm`` has usually removed it already, and removal is then a no-op.
    """
    name = new_container_name()
    create = sandbox_argv(name=name, image=image, command=command, mounts=mounts, workdir=workdir)
    try:
        created = run_command_safe(create, timeout_seconds=CLEANUP_TIMEOUT_SECONDS)
        if not created.ok:
            return created
        return run_command_safe(
            ["docker", "start", "--attach", name],
            timeout_seconds=timeout_seconds,
        )
    finally:
        remove_container(name)
