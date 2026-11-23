"""oasdiff / gitleaks handling for the GitHub change source: fail closed, never leak."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.deterministic.contract_diff import parse_oasdiff_output
from app.deterministic.subprocess_util import CommandResult
from mcp_server.sentinelflow_context import github_source
from mcp_server.sentinelflow_context.github_source import (
    LocalSignalTools,
    parse_gitleaks_report,
)


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
    ]
    result = parse_gitleaks_report(report, root=tmp_path)
    assert result.verified_secret_exposure
    assert result.max_severity == "critical"
    assert [finding.file_path for finding in result.findings] == [
        "services/config.py",
        "keys/id_rsa",
    ]
    assert secret not in result.model_dump_json()
    assert result.findings[0].detail == "AWS access key (line 9)"


def test_empty_gitleaks_report_is_clean(tmp_path: Path) -> None:
    result = parse_gitleaks_report([], root=tmp_path)
    assert result.findings == []
    assert not result.verified_secret_exposure


def _no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_source, "command_available", lambda name: False)
    monkeypatch.setattr(github_source, "docker_available", lambda: False)


def test_unavailable_tools_return_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_tools(monkeypatch)
    (tmp_path / "base.yaml").write_text("openapi: 3.1.0\n", encoding="utf-8")
    (tmp_path / "revision.yaml").write_text("openapi: 3.1.0\n", encoding="utf-8")
    tools = LocalSignalTools()
    assert tools.diff_openapi(tmp_path / "base.yaml", tmp_path / "revision.yaml") is None
    assert tools.scan_secrets(tmp_path) is None


def test_gitleaks_tool_error_is_not_a_clean_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_source, "command_available", lambda name: name == "gitleaks")
    seen: list[list[str]] = []

    def fail(argv: list[str], **_: Any) -> CommandResult:
        seen.append(argv)
        return CommandResult(argv=argv, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(github_source, "run_command", fail)
    assert LocalSignalTools().scan_secrets(tmp_path) is None
    argv = seen[0]
    assert argv[:2] == ["gitleaks", "dir"]
    assert "--redact" in argv
    assert argv[argv.index("--exit-code") + 1] == "2"


def test_gitleaks_leaks_found_exit_code_parses_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_source, "command_available", lambda name: name == "gitleaks")

    def leaks(argv: list[str], **_: Any) -> CommandResult:
        report = Path(argv[argv.index("--report-path") + 1])
        report.write_text(json.dumps([{"RuleID": "generic-api-key", "File": "a.py"}]))
        return CommandResult(argv=argv, returncode=2, stdout="", stderr="")

    monkeypatch.setattr(github_source, "run_command", leaks)
    result = LocalSignalTools().scan_secrets(tmp_path)
    assert result is not None
    assert result.verified_secret_exposure


def test_oasdiff_runs_offline_without_pulling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_source, "_docker_image_present", lambda image: True)
    seen: list[list[str]] = []

    def ok(argv: list[str], **_: Any) -> CommandResult:
        seen.append(argv)
        return CommandResult(
            argv=argv, returncode=0, stdout='[{"text": "x", "level": 3}]', stderr=""
        )

    monkeypatch.setattr(github_source, "run_command", ok)
    (tmp_path / "base.yaml").write_text("a", encoding="utf-8")
    (tmp_path / "revision.yaml").write_text("b", encoding="utf-8")
    result = LocalSignalTools().diff_openapi(tmp_path / "base.yaml", tmp_path / "revision.yaml")
    assert result is not None and result.breaking
    assert "--pull=never" in seen[0] and "--network=none" in seen[0]


def test_oasdiff_failure_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_source, "_docker_image_present", lambda image: True)
    monkeypatch.setattr(
        github_source,
        "run_command",
        lambda argv, **_: CommandResult(argv=argv, returncode=125, stdout="", stderr="no image"),
    )
    (tmp_path / "base.yaml").write_text("a", encoding="utf-8")
    (tmp_path / "revision.yaml").write_text("b", encoding="utf-8")
    assert (
        LocalSignalTools().diff_openapi(tmp_path / "base.yaml", tmp_path / "revision.yaml") is None
    )
