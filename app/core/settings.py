"""Application settings loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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

    # Postgres / pgvector
    database_url: str = Field(
        default="postgresql+psycopg://sentinelflow:sentinelflow@localhost:5433/sentinelflow",
        description="SQLAlchemy connection string for pgvector store",
    )
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_user: str = "sentinelflow"
    postgres_password: SecretStr = SecretStr("sentinelflow")
    postgres_db: str = "sentinelflow"

    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None

    # Embeddings
    gemini_api_key: SecretStr | None = None
    embedding_model: str = "gemini-embedding-2"
    embedding_dimensions: int = 768
    embedding_cache_path: Path = Field(
        default_factory=lambda: Path(".sentinelflow/embedding_cache.sqlite"),
        description="Local SQLite cache for embedding vectors (gitignored)",
    )

    # Rerankers
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    voyage_api_key: SecretStr | None = None
    voyage_rerank_model: str = "rerank-2.5-lite"

    # Retrieval
    vector_store_backend: Literal["pgvector", "qdrant"] = "pgvector"
    retrieval_top_k: int = 10
    rerank_top_n: int = 5

    @field_validator("domains_root", "embedding_cache_path", mode="before")
    @classmethod
    def resolve_repo_relative_path(cls, value: Path | str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        repo_root = Path(__file__).resolve().parents[2]
        return (repo_root / path).resolve()

    def require_gemini_api_key(self) -> str:
        """Return the Gemini API key or raise if missing."""
        if self.gemini_api_key is None or not self.gemini_api_key.get_secret_value().strip():
            msg = (
                "GEMINI_API_KEY is required for M3 retrieval (embeddings). "
                "Copy .env.example to .env and set your key."
            )
            raise RuntimeError(msg)
        return self.gemini_api_key.get_secret_value()

    def voyage_api_key_or_none(self) -> str | None:
        """Return Voyage API key when configured."""
        if self.voyage_api_key is None:
            return None
        value = self.voyage_api_key.get_secret_value().strip()
        return value or None


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
