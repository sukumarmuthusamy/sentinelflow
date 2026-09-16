# SentinelFlow

Evidence-grounded multi-agent release-risk intelligence platform.

See [SENTINELFLOW_PLAN.md](SENTINELFLOW_PLAN.md) for the canonical architecture and roadmap.

## M0 — Local development

```bash
uv sync --extra dev
uv run ruff check app tests
uv run mypy app
uv run pytest
uv run uvicorn app.main:app --reload
```
