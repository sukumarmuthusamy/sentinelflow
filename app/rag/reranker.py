"""Reranker integrations: local cross-encoder and hosted Voyage comparison."""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache

from sentence_transformers import CrossEncoder

from app.core.settings import Settings, get_settings
from app.rag.models import RerankerMode, RetrievedChunk

# Local cross-encoders run on CPU only. GPU offload was investigated and rejected on
# the reference dev machine (Quadro M1000M, sm_50 < PyTorch sm_75 minimum). CPU
# inference works after paging-file adjustment but is impractical at eval scale
# (~14.5 min/query for Qwen3-Reranker-0.6B). Use --skip-local-reranker in evals
# and the Voyage hosted arm for ablation on constrained hardware.
LOCAL_RERANKER_DEVICE = "cpu"


class RerankerPort(ABC):
    """Rerank retrieved chunks for a query."""

    mode: RerankerMode

    @abstractmethod
    def rerank(
        self, query: str, chunks: list[RetrievedChunk], *, top_n: int
    ) -> list[RetrievedChunk]:
        """Return chunks reordered by reranker relevance."""


class NoOpReranker(RerankerPort):
    mode = RerankerMode.NONE

    def rerank(
        self, query: str, chunks: list[RetrievedChunk], *, top_n: int
    ) -> list[RetrievedChunk]:
        del query
        return chunks[:top_n]


@lru_cache
def _get_cross_encoder(model_name: str) -> CrossEncoder:
    return CrossEncoder(model_name, device=LOCAL_RERANKER_DEVICE)


class CrossEncoderReranker(RerankerPort):
    """Local cross-encoder reranker (default: Qwen/Qwen3-Reranker-0.6B, CPU-only)."""

    mode = RerankerMode.LOCAL

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = settings or get_settings()
        self._model = _get_cross_encoder(cfg.reranker_model)

    def rerank(
        self, query: str, chunks: list[RetrievedChunk], *, top_n: int
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        pairs = [(query, chunk.content) for chunk in chunks]
        scores = self._model.predict(pairs)
        ranked = sorted(
            zip(chunks, scores, strict=True),
            key=lambda item: float(item[1]),
            reverse=True,
        )
        results: list[RetrievedChunk] = []
        for rank, (chunk, score) in enumerate(ranked[:top_n], start=1):
            results.append(
                RetrievedChunk(
                    content=chunk.content,
                    metadata=chunk.metadata,
                    score=float(score),
                    rank=rank,
                )
            )
        return results


class VoyageReranker(RerankerPort):
    """Hosted Voyage rerank-2.5-lite comparison arm (requires VOYAGE_API_KEY)."""

    mode = RerankerMode.VOYAGE

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = settings or get_settings()
        api_key = cfg.voyage_api_key_or_none()
        if api_key is None:
            msg = (
                "VOYAGE_API_KEY is required for the Voyage reranker comparison arm. "
                "Sign up at https://www.voyageai.com/ for the 200M free-token grant."
            )
            raise RuntimeError(msg)
        import voyageai

        self._client = voyageai.Client(api_key=api_key)  # type: ignore[attr-defined]
        self._model = cfg.voyage_rerank_model

    def rerank(
        self, query: str, chunks: list[RetrievedChunk], *, top_n: int
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        import time

        from voyageai.error import RateLimitError

        documents = [chunk.content for chunk in chunks]
        top_k = min(top_n, len(chunks))
        for attempt in range(4):
            try:
                response = self._client.rerank(
                    query=query,
                    documents=documents,
                    model=self._model,
                    top_k=top_k,
                )
                break
            except RateLimitError:
                if attempt >= 3:
                    raise
                # Free-tier TPM/RPM windows are rolling; wait a full minute before retry.
                time.sleep(65)
        results: list[RetrievedChunk] = []
        for rank, item in enumerate(response.results, start=1):
            chunk = chunks[item.index]
            results.append(
                RetrievedChunk(
                    content=chunk.content,
                    metadata=chunk.metadata,
                    score=float(item.relevance_score),
                    rank=rank,
                )
            )
        return results


def build_reranker(mode: RerankerMode, settings: Settings | None = None) -> RerankerPort:
    if mode is RerankerMode.NONE:
        return NoOpReranker()
    if mode is RerankerMode.LOCAL:
        return CrossEncoderReranker(settings)
    return VoyageReranker(settings)
