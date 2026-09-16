"""Subprocess wrappers for gitleaks, semgrep, and osv-scanner."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from app.deterministic.models import SecurityFindingRecord, SecurityScanResult, Severity
from app.deterministic.subprocess_util import command_available, run_command


class ScannerOptions(BaseModel):
    """Shared scanner execution options."""

    timeout_seconds: int = 120


def _max_severity(findings: list[SecurityFindingRecord]) -> str:
    if not findings:
        return "none"
    return max(
        (Severity.from_string(finding.severity) for finding in findings),
        key=Severity.rank,
    ).value


def _aggregate(findings: list[SecurityFindingRecord]) -> SecurityScanResult:
    max_severity = _max_severity(findings)
    verified_secret = any(
        finding.scanner == "gitleaks" and finding.severity in {"high", "critical"}
        for finding in findings
    )
    return SecurityScanResult(
        findings=findings,
        max_severity=max_severity,  # type: ignore[arg-type]
        verified_secret_exposure=verified_secret,
    )


def run_gitleaks(target: Path, *, options: ScannerOptions | None = None) -> SecurityScanResult:
    """Scan a path with gitleaks and return normalized findings."""
    opts = options or ScannerOptions()
    if not command_available("gitleaks"):
        return SecurityScanResult()

    argv = [
        "gitleaks",
        "detect",
        "--no-banner",
        "--source",
        str(target),
        "--report-format",
        "json",
        "--report-path",
        "-",
    ]
    result = run_command(argv, timeout_seconds=opts.timeout_seconds)
    if not result.stdout.strip():
        return SecurityScanResult()

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return SecurityScanResult()

    findings: list[SecurityFindingRecord] = []
    items = payload if isinstance(payload, list) else payload.get("findings", [])
    if isinstance(items, list):
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            findings.append(
                SecurityFindingRecord(
                    id=f"gitleaks-{index}",
                    scanner="gitleaks",
                    severity="critical",
                    title=str(item.get("RuleID", item.get("Description", "secret detected"))),
                    detail=str(item.get("Match", "")),
                    file_path=item.get("File"),
                    rule_id=item.get("RuleID"),
                )
            )
    return _aggregate(findings)


def run_semgrep(target: Path, *, options: ScannerOptions | None = None) -> SecurityScanResult:
    """Run semgrep with JSON output using bundled permissive rules only."""
    opts = options or ScannerOptions()
    if not command_available("semgrep"):
        return SecurityScanResult()

    argv = [
        "semgrep",
        "--config",
        "p/ci",
        "--json",
        "--quiet",
        str(target),
    ]
    result = run_command(argv, timeout_seconds=opts.timeout_seconds)
    if not result.stdout.strip():
        return SecurityScanResult()

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return SecurityScanResult()

    findings: list[SecurityFindingRecord] = []
    results = payload.get("results", []) if isinstance(payload, dict) else []
    if isinstance(results, list):
        for index, item in enumerate(results):
            if not isinstance(item, dict):
                continue
            extra = item.get("extra", {})
            severity_raw = extra.get("severity", "INFO") if isinstance(extra, dict) else "INFO"
            severity = str(severity_raw).lower()
            if severity not in {"critical", "high", "medium", "low", "info"}:
                severity = "medium"
            findings.append(
                SecurityFindingRecord(
                    id=f"semgrep-{index}",
                    scanner="semgrep",
                    severity=severity,  # type: ignore[arg-type]
                    title=str(extra.get("message", item.get("check_id", "semgrep finding"))),
                    detail=str(item.get("path", "")),
                    file_path=item.get("path"),
                    rule_id=item.get("check_id"),
                )
            )
    return _aggregate(findings)


def run_osv_scanner(lockfile: Path, *, options: ScannerOptions | None = None) -> SecurityScanResult:
    """Scan a lockfile with osv-scanner."""
    opts = options or ScannerOptions()
    if not command_available("osv-scanner") or not lockfile.is_file():
        return SecurityScanResult()

    argv = ["osv-scanner", "--format", "json", "--lockfile", str(lockfile)]
    result = run_command(argv, timeout_seconds=opts.timeout_seconds)
    if not result.stdout.strip():
        return SecurityScanResult()

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return SecurityScanResult()

    findings: list[SecurityFindingRecord] = []
    results = payload.get("results", []) if isinstance(payload, dict) else []
    if isinstance(results, list):
        index = 0
        for package_result in results:
            if not isinstance(package_result, dict):
                continue
            vulnerabilities = package_result.get("vulnerabilities", [])
            if not isinstance(vulnerabilities, list):
                continue
            for vuln in vulnerabilities:
                if not isinstance(vuln, dict):
                    continue
                severity = "high"
                severity_raw = vuln.get("severity")
                if isinstance(severity_raw, list) and severity_raw:
                    first = severity_raw[0]
                    if isinstance(first, dict) and "score" in first:
                        score = float(first["score"])
                        if score >= 9.0:
                            severity = "critical"
                        elif score >= 7.0:
                            severity = "high"
                        else:
                            severity = "medium"
                findings.append(
                    SecurityFindingRecord(
                        id=f"osv-{index}",
                        scanner="osv-scanner",
                        severity=severity,  # type: ignore[arg-type]
                        title=str(vuln.get("id", "dependency vulnerability")),
                        detail=str(vuln.get("summary", "")),
                        rule_id=str(vuln.get("id", "")),
                    )
                )
                index += 1
    return _aggregate(findings)


def load_security_fixture(path: Path) -> SecurityScanResult:
    """Load pre-recorded scanner output for offline fixture evaluation."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    findings = [
        SecurityFindingRecord.model_validate(item)
        for item in payload.get("findings", [])
        if isinstance(item, dict)
    ]
    raw_severity = payload.get("max_severity")
    max_severity = str(raw_severity) if raw_severity is not None else _max_severity(findings)
    return SecurityScanResult(
        findings=findings,
        max_severity=max_severity,  # type: ignore[arg-type]
        verified_secret_exposure=bool(payload.get("verified_secret_exposure", False)),
    )


def run_security_scans(
    *,
    repo_root: Path,
    lockfile: Path | None = None,
    fixture_signals: Path | None = None,
) -> SecurityScanResult:
    """Run all scanners and merge findings; fall back to fixture signals when needed."""
    merged: list[SecurityFindingRecord] = []
    tools_ran = False

    for runner, args in (
        (run_gitleaks, (repo_root,)),
        (run_semgrep, (repo_root,)),
    ):
        result = runner(*args)
        if result.findings:
            tools_ran = True
        merged.extend(result.findings)

    if lockfile is not None:
        osv_result = run_osv_scanner(lockfile)
        if osv_result.findings:
            tools_ran = True
        merged.extend(osv_result.findings)

    if tools_ran:
        return _aggregate(merged)

    if fixture_signals and fixture_signals.is_file():
        return load_security_fixture(fixture_signals)

    return SecurityScanResult()
