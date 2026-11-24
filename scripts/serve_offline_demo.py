"""Serve the real API with offline test doubles, for the M5-C harness demo.

    uv run --extra dev python scripts/serve_offline_demo.py [--port 8000] [--stream-tokens]

What is real: the FastAPI app, routes, job store, SSE stream, LangGraph graph,
deterministic risk engine and policy gate, the in-process ``sentinelflow-context``
MCP server reading the bundled fixtures, approval and the audit-log write.

What is fake: the model. Every agent returns the same canned, schema-valid
answer (``tests/graph/conftest.py``), so the agents' text is not an analysis of
the change. Retrieval is disabled. ``--stream-tokens`` swaps in a fake chat
model that streams that canned JSON in chunks, to show ``agent.token`` events.

Nothing calls a provider, GitHub or the network. Job, checkpoint and audit files
go to a fresh temporary directory, printed at start. Binds 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SentinelFlow offline demo API (fake model)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--stream-tokens", action="store_true")
    args = parser.parse_args(argv)

    data = Path(tempfile.mkdtemp(prefix="sentinelflow-demo-"))
    # The MCP server reads its audit-log path from the environment.
    os.environ["SENTINELFLOW_AUDIT_LOG_PATH"] = str(data / "gate_decisions.jsonl")

    import uvicorn
    from app.core.settings import Settings
    from app.main import create_app
    from mcp_server.sentinelflow_context.settings import clear_mcp_server_settings_cache
    from tests.api.conftest import FakeRuntime
    from tests.graph.conftest import static_llm_responses
    from tests.graph.streaming_fakes import FakeStreamingGatewayLLM

    clear_mcp_server_settings_cache()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,  # never read provider keys from .env for the demo
        api_job_db_path=data / "jobs.sqlite",
        graph_checkpoint_path=data / "checkpoints.sqlite",
        gemini_api_key=None,
        openrouter_api_key=None,
        api_key=None,
        api_stream_tokens=args.stream_tokens,
        analysis_max_llm_calls=50,
    )
    llm = (
        FakeStreamingGatewayLLM(static_llm_responses(), settings=settings)
        if args.stream_tokens
        else None
    )
    runtime = FakeRuntime(llm, stream_tokens=args.stream_tokens)
    print(
        "SentinelFlow OFFLINE DEMO API: canned model answers, no provider calls.\n"
        f"  data dir: {data}\n  listening on http://127.0.0.1:{args.port}",
        flush=True,
    )
    uvicorn.run(create_app(settings, runtime=runtime), host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
