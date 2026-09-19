"""Unit tests for structure-aware knowledge chunking."""

from pathlib import Path

from app.rag.chunking import chunk_knowledge_corpus, count_tokens


def test_healthcare_corpus_chunks_have_metadata() -> None:
    root = Path("domains/healthcare-claims/knowledge")
    chunks = chunk_knowledge_corpus(knowledge_root=root, domain_pack_id="healthcare-claims")
    assert len(chunks) >= 30
    sample = chunks[0]
    assert sample.metadata.document_id
    assert sample.metadata.section_id
    assert sample.metadata.domain_pack_id == "healthcare-claims"
    assert count_tokens(sample.content) > 0


def test_incident_sections_preserved_in_chunk_ids() -> None:
    root = Path("domains/healthcare-claims/knowledge")
    chunks = chunk_knowledge_corpus(knowledge_root=root, domain_pack_id="healthcare-claims")
    incident_chunks = [c for c in chunks if c.metadata.document_type.value == "incident"]
    assert incident_chunks
    assert any("symptom" in c.metadata.section_id for c in incident_chunks)
