"""Streamlit harness for the SentinelFlow HTTP API (M5-C).

The harness is an HTTP client only. It never imports ``app`` (graph, job store,
MCP or model gateway); everything it shows comes from the ``/v1`` endpoints and
the SSE stream. ``tests/ui`` pins that boundary.
"""
