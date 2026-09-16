"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the SentinelFlow API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "SentinelFlow"
    debug: bool = False
    domains_root: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[2] / "domains",
        description="Root directory containing domain packs",
    )
    default_domain_pack: str | None = Field(
        default=None,
        description=(
            "Default domain pack id; when unset, the first sorted pack under domains_root is used"
        ),
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
