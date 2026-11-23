"""oasdiff / gitleaks handling for the GitHub change source: fail closed, never leak."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest
from app.deterministic import docker_sandbox
from app.deterministic.contract_diff import (
    OASDIFF_IMAGE,
    OasDiffOutputError,
    parse_oasdiff_output,
)
from app.deterministic.subprocess_util import CommandResult, run_command, run_command_safe
from mcp_server.sentinelflow_context import github_source
from mcp_server.sentinelflow_context.github_source import (
    GITLEAKS_IMAGE,
    GitleaksReportError,
    LocalSignalTools,
    ScannerError,
    parse_gitleaks_report,
)

# ------------------------------------------------------------------ oasdiff parsing


def test_oasdiff_numeric_levels() -> None:
    output = json.dumps(
        [
            {"id": "a", "text": "removed required property", "level": 3, "path": "/x"},
            {"id": "b", "text": "response description changed", "level": 2},
            {"id": "c", "text": "added optional property", "level": 1},
        ],
    )
    result = parse_oasdiff_output(output)
    assert (result.err_count, result.warn_count, result.info_count) == (1, 1, 1)
    assert result.breaking
    assert result.tool == "oasdiff"


def test_oasdiff_string_levels_still_parse() -> None:
    result = parse_oasdiff_output(json.dumps([{"text": "x", "level": "ERR"}]))
    assert result.err_count == 1


def test_oasdiff_empty_list_is_a_legitimate_no_difference_verdict() -> None:
    result = parse_oasdiff_output("[]\n")
    assert result.tool == "oasdiff"
    assert (result.err_count, result.breaking, result.changes) == (0, False, [])


def test_oasdiff_changes_object_is_supported() -> None:
    result = parse_oasdiff_output(json.dumps({"changes": [{"level": 3, "text": "x"}]}))
    assert result.breaking


@pytest.mark.parametrize(
    "output",
    [
        "",  # no output at all
        '[{"level": 3, "text":',  # truncated JSON
        '{"error": "internal failure"}',  # unrecognised object
        '"breaking"',  # JSON scalar
        "ERR: removed property",  # text, never requested
        '[{"text": "no level"}]',  # missing level
        '[{"level": 7, "text": "x"}]',  # unknown numeric level
        '[{"level": "CRITICAL", "text": "x"}]',  # unknown string level
        '[{"level": true, "text": "x"}]',  # boolean level
        '[{"level": 3.0, "text": "x"}]',  # float level
        '[3, {"level": 3}]',  # non-object record
        '{"changes": "none"}',  # changes is not a list
    ],
)
def test_malformed_oasdiff_output_is_never_clean(output: str) -> None:
    with pytest.raises(OasDiffOutputError):
        parse_oasdiff_output(output)


# ------------------------------------------------------------------ gitleaks parsing


def test_gitleaks_report_is_redacted_and_relative(tmp_path: Path) -> None:
    secret = "AKIA" + "X" * 16
    report = [
        {
            "RuleID": "aws-access-token",
            "Description": "AWS access key",
            "File": str(tmp_path / "services" / "config.py"),
            "StartLine": 9,
            "Match": f'AWS_ACCESS_KEY_ID = "{secret}"',
            "Secret": secret,
        },
        {"RuleID": "private-key", "File": "/scan/keys/id_rsa", "StartLine": 1},
        {"RuleID": "generic-api-key", "File": "/scan/repo/app/settings.py"},
    ]
    result = parse_gitleaks_report(report, root=tmp_path)
    assert result.verified_secret_exposure
    assert result.max_severity == "critical"
    assert [finding.file_path for finding in result.findings] == [
        "services/config.py",
        "keys/id_rsa",
        "app/settings.py",
    ]
    assert secret not in result.model_dump_json()
    assert result.findings[0].detail == "AWS access key (line 9)"


def test_empty_gitleaks_report_is_clean(tmp_path: Path) -> None:
    result = parse_gitleaks_report([], root=tmp_path)
    assert result.findings == []
    assert not result.verified_secret_exposure


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "bad"},
        None,
        "[]",
        [["aws-access-token", "a.py"]],
        [{"File": "a.py"}],
        [{"RuleID": "aws-access-token"}],
        [{"RuleID": "", "File": "a.py"}],
        [{"RuleID": 7, "File": "a.py"}],
    ],
)
def test_malformed_gitleaks_report_is_never_clean(payload: object, tmp_path: Path) -> None:
    with pytest.raises(GitleaksReportError):
        parse_gitleaks_report(payload, root=tmp_path)


# ------------------------------------------------------------------ subprocess layer


def test_run_command_safe_turns_timeout_into_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(argv: list[str], **_: Any) -> None:
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(subprocess, "run", hang)
    result = run_command_safe(["docker", "run", "--secret-arg"], timeout_seconds=5)
    assert result.timed_out and not result.ok
    assert result.stderr == "docker timed out after 5s"
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(["docker"], timeout_seconds=5)


def test_run_command_safe_turns_launch_failure_into_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(argv: list[str], **_: Any) -> None:
        raise FileNotFoundError(2, "No such file", "/opt/private/path/gitleaks")

    monkeypatch.setattr(subprocess, "run", missing)
    result = run_command_safe(["/opt/private/path/gitleaks", "dir"], timeout_seconds=5)
    assert result.launch_failed and not result.ok
    assert "/opt/private" not in result.stderr


# ------------------------------------------------------------------ docker sandbox


class FakeDocker:
    """Records docker invocations. ``run`` decides what the started container does.

    ``run`` receives the ``docker create`` argv, so tests can inspect mounts and flags.
    """

    def __init__(self, run: Any = None) -> None:
        self.calls: list[list[str]] = []
        self._run = run or (lambda argv: CommandResult(argv, 0, "[]", ""))
        self.created: dict[str, list[str]] = {}
        self.create_result: CommandResult | None = None
        self.rm_result = CommandResult([], 0, "", "")

    def __call__(self, argv: list[str], **_: Any) -> CommandResult:
        self.calls.append(argv)
        if argv[:2] == ["docker", "rm"]:
            return self.rm_result
        if argv[:2] == ["docker", "create"]:
            if self.create_result is not None:
                return self.create_result
            self.created[argv[argv.index("--name") + 1]] = argv
            return CommandResult(argv, 0, "container-id", "")
        assert argv[:3] == ["docker", "start", "--attach"], argv
        result: CommandResult = self._run(self.created[argv[3]])
        return result

    @property
    def runs(self) -> list[list[str]]:
        return list(self.created.values())

    @property
    def removals(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["docker", "rm"]]


def _install(monkeypatch: pytest.MonkeyPatch, docker: FakeDocker) -> None:
    monkeypatch.setattr(docker_sandbox, "run_command_safe", docker)
    monkeypatch.setattr(github_source, "image_present", lambda image: True)
    monkeypatch.setattr(github_source, "_gitleaks_binary_matches", lambda: False)


def _specs(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "base.yaml").write_text("a", encoding="utf-8")
    (tmp_path / "revision.yaml").write_text("b", encoding="utf-8")
    return tmp_path / "base.yaml", tmp_path / "revision.yaml"


HARDENING = (
    "--network=none",
    "--pull=never",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--pids-limit=64",
    "--memory=512m",
    "--cpus=1",
)


def test_images_are_pinned_by_registry_digest() -> None:
    for image in (OASDIFF_IMAGE, GITLEAKS_IMAGE):
        name, _, digest = image.partition("@sha256:")
        assert name and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    assert GITLEAKS_IMAGE.startswith("zricethezav/gitleaks:v8.30.1@")


def test_oasdiff_runs_hardened_and_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker(lambda argv: CommandResult(argv, 0, '[{"text": "x", "level": 3}]', ""))
    _install(monkeypatch, docker)
    result = LocalSignalTools().diff_openapi(*_specs(tmp_path))
    assert result is not None and result.breaking
    argv = docker.runs[0]
    for flag in HARDENING:
        assert flag in argv, flag
    assert "--privileged" not in argv
    assert argv[argv.index("--name") + 1].startswith("sf-scan-")
    assert any(arg.endswith(":/specs:ro") for arg in argv)
    assert OASDIFF_IMAGE in argv
    name = argv[argv.index("--name") + 1]
    # Removed by exact name after every run (a no-op once --rm has removed it).
    assert docker.removals == [["docker", "rm", "--force", name]]


def test_container_that_cannot_be_created_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker()
    docker.create_result = CommandResult([], 125, "", "No such image")
    _install(monkeypatch, docker)
    with pytest.raises(ScannerError, match="^oasdiff failed$"):
        LocalSignalTools().diff_openapi(*_specs(tmp_path))
    assert len(docker.removals) == 1
    assert not any(call[:2] == ["docker", "start"] for call in docker.calls)


def test_container_names_are_unique(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker()
    _install(monkeypatch, docker)
    for _ in range(3):
        LocalSignalTools().diff_openapi(*_specs(tmp_path))
    names = {argv[argv.index("--name") + 1] for argv in docker.runs}
    assert len(names) == 3


def test_oasdiff_failure_raises_scanner_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeDocker(lambda argv: CommandResult(argv, 102, "", "bad spec")))
    with pytest.raises(ScannerError, match="^oasdiff failed$"):
        LocalSignalTools().diff_openapi(*_specs(tmp_path))


def test_oasdiff_invalid_output_raises_scanner_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeDocker(lambda argv: CommandResult(argv, 0, '[{"level": 3', "")))
    with pytest.raises(ScannerError, match="invalid output"):
        LocalSignalTools().diff_openapi(*_specs(tmp_path))


def test_timeout_removes_exactly_the_scanner_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(lambda argv: CommandResult(argv, -1, "", "timed out", timed_out=True))
    _install(monkeypatch, docker)
    with pytest.raises(ScannerError, match="^oasdiff timed out$"):
        LocalSignalTools().diff_openapi(*_specs(tmp_path))
    name = docker.runs[0][docker.runs[0].index("--name") + 1]
    assert docker.removals == [["docker", "rm", "--force", name]]


def test_cancellation_removes_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(argv: list[str]) -> CommandResult:
        raise KeyboardInterrupt

    docker = FakeDocker(interrupted)
    _install(monkeypatch, docker)
    with pytest.raises(KeyboardInterrupt):
        LocalSignalTools().diff_openapi(*_specs(tmp_path))
    assert len(docker.removals) == 1
    assert docker.removals[0][3].startswith("sf-scan-")


def test_failed_cleanup_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    docker = FakeDocker(lambda argv: CommandResult(argv, -1, "", "timed out", timed_out=True))
    docker.rm_result = CommandResult([], 1, "", "daemon unreachable")
    _install(monkeypatch, docker)
    with caplog.at_level(logging.WARNING), pytest.raises(ScannerError):
        LocalSignalTools().diff_openapi(*_specs(tmp_path))
    assert any("could not be removed" in record.getMessage() for record in caplog.records)


def test_missing_container_counts_as_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker()
    docker.rm_result = CommandResult([], 1, "", "Error: No such container: sf-scan-x")
    monkeypatch.setattr(docker_sandbox, "run_command_safe", docker)
    assert docker_sandbox.remove_container("sf-scan-x")


def test_cleanup_refuses_foreign_containers() -> None:
    with pytest.raises(ValueError, match="not a SentinelFlow scanner"):
        docker_sandbox.remove_container("postgres")


# ------------------------------------------------------------------ gitleaks runs


def _gitleaks_writes(payload: str, returncode: int) -> Any:
    """A started gitleaks container that writes ``payload`` to its report mount."""

    def run(argv: list[str]) -> CommandResult:
        mount = next(arg for arg in argv if arg.endswith(":/report"))
        host_dir = mount[: -len(":/report")]
        (Path(host_dir) / "report.json").write_text(payload, encoding="utf-8")
        return CommandResult(argv, returncode, "", "")

    return run


def test_gitleaks_configuration_cannot_come_from_the_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(_gitleaks_writes("[]", 0))
    _install(monkeypatch, docker)
    result = LocalSignalTools().scan_secrets(tmp_path)
    assert result is not None and result.findings == []
    argv = docker.runs[0]
    for flag in HARDENING:
        assert flag in argv, flag
    assert GITLEAKS_IMAGE in argv
    command = argv[argv.index(GITLEAKS_IMAGE) + 1 :]
    assert command[:2] == ["dir", "/scan"]
    assert command[command.index("--config") + 1] == "/config/gitleaks.toml"
    assert command[command.index("--gitleaks-ignore-path") + 1] == "/config"
    assert "--ignore-gitleaks-allow" in command and "--redact" in command
    assert command[command.index("--exit-code") + 1] == "2"
    # PR files are mounted below the target, read-only; only the report dir is writable.
    assert any(arg.endswith(":/scan/repo:ro") for arg in argv)
    writable = [arg for arg in argv if ":/" in arg and not arg.endswith(":ro")]
    assert len(writable) == 1 and writable[0].endswith(":/report")


def test_gitleaks_leaks_found_exit_code_parses_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = json.dumps([{"RuleID": "generic-api-key", "File": "/scan/repo/a.py"}])
    _install(monkeypatch, FakeDocker(_gitleaks_writes(report, 2)))
    result = LocalSignalTools().scan_secrets(tmp_path)
    assert result is not None and result.verified_secret_exposure
    assert result.findings[0].file_path == "a.py"


@pytest.mark.parametrize(
    ("payload", "returncode", "reason"),
    [
        ("[]", 1, "gitleaks failed"),
        ('{"error": "bad"}', 0, "no valid report"),
        ("", 0, "no valid report"),
        ('[{"RuleID": "aws', 2, "no valid report"),
        ("[]", 2, "listed none"),
    ],
)
def test_gitleaks_failures_are_not_a_clean_scan(
    payload: str, returncode: int, reason: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeDocker(_gitleaks_writes(payload, returncode)))
    with pytest.raises(ScannerError, match=reason):
        LocalSignalTools().scan_secrets(tmp_path)


def test_gitleaks_without_report_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeDocker(lambda argv: CommandResult(argv, 0, "", "")))
    with pytest.raises(ScannerError, match="no valid report"):
        LocalSignalTools().scan_secrets(tmp_path)


def test_gitleaks_timeout_removes_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(lambda argv: CommandResult(argv, -1, "", "", timed_out=True))
    _install(monkeypatch, docker)
    with pytest.raises(ScannerError, match="^gitleaks timed out$"):
        LocalSignalTools().scan_secrets(tmp_path)
    assert len(docker.removals) == 1


def test_unpinned_gitleaks_binary_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_source, "image_present", lambda image: False)
    monkeypatch.setattr(github_source, "command_available", lambda name: name == "gitleaks")
    monkeypatch.setattr(
        github_source,
        "run_command_safe",
        lambda argv, **_: CommandResult(argv, 0, "8.18.0\n", ""),
    )
    assert LocalSignalTools().scan_secrets(tmp_path) is None


def test_pinned_gitleaks_binary_uses_the_same_locked_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".gitleaksignore").write_text("anything\n", encoding="utf-8")
    seen: list[list[str]] = []

    def binary(argv: list[str], **_: Any) -> CommandResult:
        if argv[1] == "version":
            return CommandResult(argv, 0, "v8.30.1\n", "")
        seen.append(argv)
        target = Path(argv[2])
        # The PR's files sit one level below the target, never at its root.
        assert not (target / ".gitleaksignore").exists()
        assert (target / "repo" / ".gitleaksignore").exists()
        Path(argv[argv.index("--report-path") + 1]).write_text("[]", encoding="utf-8")
        return CommandResult(argv, 0, "", "")

    monkeypatch.setattr(github_source, "image_present", lambda image: False)
    monkeypatch.setattr(github_source, "command_available", lambda name: name == "gitleaks")
    monkeypatch.setattr(github_source, "run_command_safe", binary)
    result = LocalSignalTools().scan_secrets(tmp_path)
    assert result is not None and result.findings == []
    assert "--ignore-gitleaks-allow" in seen[0] and "--config" in seen[0]


def test_unavailable_tools_return_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_source, "image_present", lambda image: False)
    monkeypatch.setattr(github_source, "command_available", lambda name: False)
    tools = LocalSignalTools()
    assert tools.diff_openapi(*_specs(tmp_path)) is None
    assert tools.scan_secrets(tmp_path) is None


# ------------------------------------------------------------------ M1 run_oasdiff


def _m1_specs(tmp_path: Path) -> tuple[Path, Path]:
    # Fixture layout: base and revision share a file name in sibling directories.
    for side in ("base", "revision"):
        (tmp_path / side).mkdir()
        (tmp_path / side / "openapi.yaml").write_text(side, encoding="utf-8")
    return tmp_path / "base" / "openapi.yaml", tmp_path / "revision" / "openapi.yaml"


def test_run_oasdiff_mounts_each_spec_and_diffs_base_against_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deterministic import contract_diff

    docker = FakeDocker(lambda argv: CommandResult(argv, 0, '[{"level": 3, "text": "x"}]', ""))
    monkeypatch.setattr(docker_sandbox, "run_command_safe", docker)
    monkeypatch.setattr(contract_diff, "image_present", lambda image: True)
    result = contract_diff.run_oasdiff(*_m1_specs(tmp_path))
    assert result.tool == "oasdiff" and result.breaking
    argv = docker.runs[0]
    command = argv[argv.index(OASDIFF_IMAGE) + 1 :]
    assert command[:3] == ["breaking", "/specs/base/openapi.yaml", "/specs/revision/openapi.yaml"]
    assert any(arg.endswith(":/specs/base:ro") for arg in argv)
    assert any(arg.endswith(":/specs/revision:ro") for arg in argv)
    for flag in HARDENING:
        assert flag in argv, flag


@pytest.mark.parametrize(
    "outcome",
    [
        CommandResult([], 102, "", "invalid spec"),
        CommandResult([], 0, '{"error": "x"}', ""),
        CommandResult([], -1, "", "", timed_out=True),
    ],
)
def test_run_oasdiff_failures_are_unavailable_not_clean(
    outcome: CommandResult, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.deterministic import contract_diff

    monkeypatch.setattr(docker_sandbox, "run_command_safe", FakeDocker(lambda argv: outcome))
    monkeypatch.setattr(contract_diff, "image_present", lambda image: True)
    assert contract_diff.run_oasdiff(*_m1_specs(tmp_path)).tool == "unavailable"
