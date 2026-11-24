"""Launch the harness on loopback: ``uv run --extra ui python -m ui.harness [--port N]``.

Streamlit listens on every interface unless told otherwise, so the address is
pinned to 127.0.0.1. Usage statistics are off and error details are hidden, so a
UI exception never shows a stack trace in the browser.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"


def streamlit_args(port: int) -> list[str]:
    return [
        "streamlit",
        "run",
        str(APP),
        "--server.address=127.0.0.1",
        f"--server.port={port}",
        "--server.headless=true",
        "--browser.gatherUsageStats=false",
        "--client.showErrorDetails=none",
        "--client.toolbarMode=minimal",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SentinelFlow Streamlit harness (loopback)")
    parser.add_argument("--port", type=int, default=8501)
    args = parser.parse_args(argv)
    from streamlit.web import cli

    sys.argv = streamlit_args(args.port)
    cli.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
