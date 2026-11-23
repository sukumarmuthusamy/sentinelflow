"""Streamlit entry point: ``uv run --extra ui python -m ui.harness`` (or ``streamlit run``).

Streamlit puts this file's directory on ``sys.path``; the repository root is added
so the harness is imported as ``ui.harness``, the same name the tests use.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ui.harness.page import main  # noqa: E402

main()
