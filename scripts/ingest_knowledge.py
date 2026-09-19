#!/usr/bin/env python3
"""Ingest domain pack knowledge corpora into pgvector and/or Qdrant."""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.settings import get_settings
from app.rag.embedding_cache import format_embedding_cache_stats, reset_embedding_cache_stats
from app.rag.ingest import ingest_domain_pack
from app.rag.models import VectorStoreBackend
from evals.retrieval.run_eval import check_prerequisites


async def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest SentinelFlow knowledge corpora")
    parser.add_argument(
        "--backend",
        choices=("pgvector", "qdrant", "both"),
        default="both",
    )
    parser.add_argument("--domain-pack", action="append", dest="domain_packs")
    args = parser.parse_args()

    check_prerequisites()
    reset_embedding_cache_stats()
    settings = get_settings()
    packs = args.domain_packs or [
        pack.name
        for pack in sorted(settings.domains_root.iterdir())
        if pack.is_dir() and (pack / "knowledge").is_dir()
    ]
    backends = (
        [VectorStoreBackend.PGVECTOR, VectorStoreBackend.QDRANT]
        if args.backend == "both"
        else [VectorStoreBackend(args.backend)]
    )

    for pack in packs:
        for backend in backends:
            report = await ingest_domain_pack(pack, backend=backend, reset=True)
            print(
                f"{report.domain_pack_id} [{report.backend.value}]: "
                f"{report.chunks_indexed} chunks / {report.source_documents} documents"
            )
    print(f"Embedding cache: {format_embedding_cache_stats()}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
