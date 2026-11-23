"""Run the local harness: ``uv run python -m app.api.serve``.

Binds 127.0.0.1 by default. It refuses a non-loopback ``--host`` unless ``API_KEY``
is configured, because ``/v1`` launches analyses that spend provider quota. A single
worker is enforced: job execution, startup recovery and (M5-B) live event fan-out
all assume one process per job database (ADR-0003).
"""

from __future__ import annotations

import argparse
import ipaddress
import sys

import uvicorn

from app.core.settings import get_settings


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SentinelFlow local API harness")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    settings = get_settings()
    has_key = bool(settings.api_key and settings.api_key.get_secret_value().strip())
    if not _loopback(args.host) and not has_key:
        print(
            f"refusing to bind {args.host}: set API_KEY to serve beyond loopback",
            file=sys.stderr,
        )
        return 2
    uvicorn.run("app.main:app", host=args.host, port=args.port, workers=1, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
