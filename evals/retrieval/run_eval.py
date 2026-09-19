#!/usr/bin/env python3
"""Run M3 retrieval eval: store comparison + reranker ablation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from app.core.settings import get_settings
from app.rag.embedding_cache import format_embedding_cache_stats, reset_embedding_cache_stats
from app.rag.ingest import ingest_domain_pack
from app.rag.models import QueryBatch, RerankerMode, RetrievalMetrics, VectorStoreBackend
from evals.retrieval.harness import evaluate_arm
from sqlalchemy import create_engine, text

DOMAIN_PACKS = ("healthcare-claims", "ecommerce-payments")
QUERY_BATCHES = (QueryBatch.EASY, QueryBatch.HARD, None)


def check_prerequisites() -> None:
    """Fail fast with actionable errors when services or keys are missing."""
    settings = get_settings()
    errors: list[str] = []

    try:
        settings.require_gemini_api_key()
    except RuntimeError as exc:
        errors.append(str(exc))

    sync_url = settings.database_url.replace("+asyncpg", "+psycopg")
    try:
        engine = create_engine(sync_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        errors.append(
            "Postgres is not reachable at DATABASE_URL. "
            "Start it with: docker compose up -d postgres "
            f"(detail: {exc})"
        )

    try:
        response = httpx.get(f"{settings.qdrant_url.rstrip('/')}/collections", timeout=5.0)
        response.raise_for_status()
    except Exception as exc:
        host = urlparse(settings.qdrant_url).netloc or settings.qdrant_url
        errors.append(
            f"Qdrant is not reachable at {host}. "
            "Start it with: docker compose up -d qdrant "
            f"(detail: {exc})"
        )

    if errors:
        print("M3 retrieval prerequisites missing:\n", file=sys.stderr)
        for item in errors:
            print(f"  - {item}", file=sys.stderr)
        sys.exit(1)


async def ingest_all() -> None:
    """Ingest all packs into both backends, pacing calls to respect embedding RPM limits."""
    steps = [
        (pack, backend)
        for pack in DOMAIN_PACKS
        for backend in (VectorStoreBackend.PGVECTOR, VectorStoreBackend.QDRANT)
    ]
    for index, (pack, backend) in enumerate(steps):
        if index > 0:
            # Free tier allows ~100 embed requests/min; pause between full re-index passes.
            await asyncio.sleep(60)
        report = await ingest_domain_pack(pack, backend=backend, reset=True)
        print(
            f"Ingested {report.domain_pack_id} -> {report.backend.value}: "
            f"{report.chunks_indexed} chunks from {report.source_documents} documents"
        )


async def evaluate_pack_batches(
    *,
    domain_pack_id: str,
    backend: VectorStoreBackend,
    reranker_mode: RerankerMode,
    k: int,
) -> list[RetrievalMetrics]:
    metrics: list[RetrievalMetrics] = []
    for batch in QUERY_BATCHES:
        metric = await evaluate_arm(
            domain_pack_id=domain_pack_id,
            backend=backend,
            reranker_mode=reranker_mode,
            k=k,
            query_batch=batch,
        )
        metrics.append(metric)
        print(metric.model_dump_json())
    return metrics


def reranker_ablation_modes(*, skip_local_reranker: bool) -> tuple[RerankerMode, ...]:
    """Reranker arms for ablation; local cross-encoder is opt-in at eval time."""
    modes: list[RerankerMode] = [RerankerMode.NONE]
    if not skip_local_reranker:
        modes.append(RerankerMode.LOCAL)
    modes.append(RerankerMode.VOYAGE)
    return tuple(modes)


async def run_matrix(
    *,
    packs: tuple[str, ...] = DOMAIN_PACKS,
    run_store_comparison: bool = True,
    run_reranker_ablation: bool = True,
    skip_local_reranker: bool = False,
) -> dict[str, list[RetrievalMetrics]]:
    results: dict[str, list[RetrievalMetrics]] = {
        "store_comparison": [],
        "reranker_ablation": [],
    }
    for pack in packs:
        if run_store_comparison:
            for backend in (VectorStoreBackend.PGVECTOR, VectorStoreBackend.QDRANT):
                results["store_comparison"].extend(
                    await evaluate_pack_batches(
                        domain_pack_id=pack,
                        backend=backend,
                        reranker_mode=RerankerMode.NONE,
                        k=10,
                    )
                )

        if not run_reranker_ablation:
            continue

        settings = get_settings()
        if skip_local_reranker:
            print(
                "Skipping local reranker arm (--skip-local-reranker): "
                "CPU cross-encoder inference is documented but impractical on this hardware."
            )
        for reranker in reranker_ablation_modes(skip_local_reranker=skip_local_reranker):
            if reranker is RerankerMode.VOYAGE and settings.voyage_api_key_or_none() is None:
                print(
                    "Skipping Voyage reranker arm: VOYAGE_API_KEY not set "
                    "(optional; get one at https://www.voyageai.com/)"
                )
                continue
            results["reranker_ablation"].extend(
                await evaluate_pack_batches(
                    domain_pack_id=pack,
                    backend=VectorStoreBackend.PGVECTOR,
                    reranker_mode=reranker,
                    k=10,
                )
            )

    return results


def summarize(results: dict[str, list[RetrievalMetrics]]) -> None:
    print("\n=== M3 Retrieval Summary ===")
    for section, metrics in results.items():
        print(f"\n{section}:")
        for metric in metrics:
            print(
                f"  {metric.domain_pack_id} | batch={metric.query_batch} | {metric.arm} | "
                f"n={metric.query_count} recall@10={metric.recall_at_10} "
                f"MRR={metric.mrr} nDCG@10={metric.ndcg_at_10}"
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run M3 retrieval evaluation")
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="Skip re-indexing (use when corpora are already ingested)",
    )
    parser.add_argument(
        "--pack",
        action="append",
        dest="packs",
        help="Limit eval to one or more domain packs",
    )
    parser.add_argument(
        "--skip-store-comparison",
        action="store_true",
        help="Skip pgvector vs qdrant store comparison arms",
    )
    parser.add_argument(
        "--skip-reranker-ablation",
        action="store_true",
        help="Skip reranker ablation arms",
    )
    parser.add_argument(
        "--skip-local-reranker",
        action="store_true",
        help=(
            "Omit the local cross-encoder arm from reranker ablation "
            "(none + Voyage only; default for dev machines where CPU reranking is impractical)"
        ),
    )
    args = parser.parse_args()

    reset_embedding_cache_stats()
    check_prerequisites()
    if not args.skip_ingest:
        await ingest_all()
        print(f"Embedding cache after ingest: {format_embedding_cache_stats()}")
        reset_embedding_cache_stats()
    packs = tuple(args.packs) if args.packs else DOMAIN_PACKS
    results = await run_matrix(
        packs=packs,
        run_store_comparison=not args.skip_store_comparison,
        run_reranker_ablation=not args.skip_reranker_ablation,
        skip_local_reranker=args.skip_local_reranker,
    )
    summarize(results)
    print(f"\nEmbedding cache: {format_embedding_cache_stats()}")
    report_path = Path("evals/retrieval/reports/latest.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_payload: dict[str, object] = {}
    if report_path.exists():
        report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    for key, value in results.items():
        if value:
            report_payload[key] = [item.model_dump() for item in value]
    report_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")
    print(f"\nWrote {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
