"""The harness is an HTTP client only: it never imports the backend (M5 exit criterion)."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
# Anything that would let the UI run the graph, read the job store or checkpoints,
# or talk to MCP directly instead of going through the API.
FORBIDDEN = (
    "app",
    "mcp_server",
    "langgraph",
    "langchain",
    "langchain_core",
    "sqlite3",
    "mcp",
    "tests",
    "evals",
    "scripts",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_no_ui_module_imports_the_backend() -> None:
    offenders = {
        str(path.relative_to(ROOT)): sorted(
            name for name in _imports(path) if name.split(".")[0] in FORBIDDEN
        )
        for path in UI.rglob("*.py")
    }
    assert {path: names for path, names in offenders.items() if names} == {}


def test_loading_the_page_loads_no_backend_module() -> None:
    pytest.importorskip("streamlit", reason="needs the ui extra")
    probe = (
        "import sys; import ui.harness.page, ui.harness.client, ui.harness.progress; "
        f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {FORBIDDEN!r}); "
        "print(','.join(bad))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_launcher_binds_loopback_and_hides_error_details() -> None:
    from ui.harness.__main__ import streamlit_args

    args = streamlit_args(8501)
    assert "--server.address=127.0.0.1" in args
    assert "--client.showErrorDetails=none" in args
    assert "--browser.gatherUsageStats=false" in args
    config = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert 'address = "127.0.0.1"' in config and 'showErrorDetails = "none"' in config
