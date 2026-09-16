"""Runtime settings for the sentinelflow-context MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.domain_pack import resolve_default_domain_pack_id
from app.core.settings import get_settings


@dataclass(frozen=True)
class McpServerSettings:
    """Configuration shared by MCP tools and resources."""

    domains_root: Path
    default_domain_pack_id: str
    audit_log_path: Path


@lru_cache
def get_mcp_server_settings() -> McpServerSettings:
    """Return cached MCP server settings."""
    app_settings = get_settings()
    project_root = app_settings.domains_root.parent
    audit_default = project_root / ".sentinelflow" / "audit" / "gate_decisions.jsonl"
    audit_override = os.getenv("SENTINELFLOW_AUDIT_LOG_PATH")
    audit_log_path = Path(audit_override) if audit_override else audit_default
    return McpServerSettings(
        domains_root=app_settings.domains_root,
        default_domain_pack_id=resolve_default_domain_pack_id(
            domains_root=app_settings.domains_root,
        ),
        audit_log_path=audit_log_path,
    )


def clear_mcp_server_settings_cache() -> None:
    """Clear cached MCP settings (for tests)."""
    get_mcp_server_settings.cache_clear()
