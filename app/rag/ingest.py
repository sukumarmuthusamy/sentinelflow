"""Ingest domain pack knowledge corpora into vector stores."""

from __future__ import annotations

from pathlib import Path

from app.core.settings import Settings, get_settings
from app.rag.chunking import chunk_knowledge_corpus, iter_knowledge_files
from app.rag.models import IngestReport, VectorStoreBackend
from app.rag.vector_store import VectorStorePort, build_vector_store


def knowledge_root(settings: Settings, domain_pack_id: str) -> Path:
    return settings.domains_root / domain_pack_id / "knowledge"


async def ingest_domain_pack(
    domain_pack_id: str,
    *,
    backend: VectorStoreBackend,
    settings: Settings | None = None,
    reset: bool = True,
) -> IngestReport:
    """Chunk and index all knowledge files for one domain pack."""
    cfg = settings or get_settings()
    root = knowledge_root(cfg, domain_pack_id)
    if not root.is_dir():
        msg = f"Knowledge corpus not found: {root}"
        raise FileNotFoundError(msg)

    chunks = chunk_knowledge_corpus(knowledge_root=root, domain_pack_id=domain_pack_id)
    collection_name = f"sentinelflow_{domain_pack_id.replace('-', '_')}"
    store: VectorStorePort = build_vector_store(backend, collection_name=collection_name)
    try:
        if reset:
            await store.reset()
        else:
            await store.initialize()
        indexed = await store.add_chunks(chunks)
    finally:
        await store.close()

    source_documents = len(list(iter_knowledge_files(root)))
    return IngestReport(
        domain_pack_id=domain_pack_id,
        source_documents=source_documents,
        chunks_indexed=indexed,
        backend=backend,
    )
