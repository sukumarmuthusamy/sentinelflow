"""Structure-aware chunking for domain pack knowledge corpora."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import tiktoken
import yaml

from app.rag.models import ChunkMetadata, DocumentType, KnowledgeChunk

_TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
TARGET_MIN_TOKENS = 350
TARGET_MAX_TOKENS = 700


def count_tokens(text: str) -> int:
    """Approximate token count for chunk sizing."""
    return len(_TOKEN_ENCODER.encode(text))


def _merge_sections(sections: list[tuple[str, str]], *, min_tokens: int) -> list[tuple[str, str]]:
    """Merge adjacent small sections until each chunk meets the minimum token target."""
    if not sections:
        return []

    merged: list[tuple[str, str]] = []
    current_title = sections[0][0]
    current_body = sections[0][1]

    for title, body in sections[1:]:
        candidate = f"{current_body}\n\n{body}".strip()
        if count_tokens(candidate) <= TARGET_MAX_TOKENS:
            current_body = candidate
            current_title = f"{current_title}+{title}"
        else:
            if count_tokens(current_body) < min_tokens and merged:
                prev_title, prev_body = merged.pop()
                current_body = f"{prev_body}\n\n{current_body}".strip()
                current_title = f"{prev_title}+{current_title}"
            merged.append((current_title, current_body))
            current_title = title
            current_body = body

    if merged and count_tokens(current_body) < min_tokens:
        prev_title, prev_body = merged.pop()
        current_body = f"{prev_body}\n\n{current_body}".strip()
        current_title = f"{prev_title}+{current_title}"

    merged.append((current_title, current_body))
    return merged


def _split_markdown_by_heading(text: str) -> list[tuple[str, str]]:
    """Split markdown on ATX headings, preserving heading text in chunk content."""
    pattern = re.compile(r"^(#{1,4})\s+(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return [("body", text.strip())]

    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()
        title = match.group(2).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "section"
        sections.append((slug, section_text))
    return sections


def _split_incident_sections(text: str) -> list[tuple[str, str]]:
    """Preserve incident report sections: symptom, root-cause, impact, detection, remediation."""
    expected = ("symptom", "root-cause", "impact", "detection", "remediation")
    sections: list[tuple[str, str]] = []
    current_slug = "summary"
    current_lines: list[str] = []

    for line in text.splitlines():
        heading_match = re.match(r"^##\s+(.+)$", line.strip())
        if heading_match:
            if current_lines:
                sections.append((current_slug, "\n".join(current_lines).strip()))
            title = heading_match.group(1).strip().lower()
            slug = next((name for name in expected if name in title.replace(" ", "-")), title)
            slug = re.sub(r"[^a-z0-9]+", "-", slug.lower()).strip("-")
            current_slug = slug
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections.append((current_slug, "\n".join(current_lines).strip()))
    return sections or [("summary", text.strip())]


def _split_openapi(path: Path, payload: dict[str, object]) -> list[tuple[str, str]]:
    """Chunk OpenAPI specs by endpoint and schema object."""
    sections: list[tuple[str, str]] = []
    info = payload.get("info", {})
    title = str(info.get("title", path.stem)) if isinstance(info, dict) else path.stem
    version = str(info.get("version", "1.0.0")) if isinstance(info, dict) else "1.0.0"
    header = {"info": info, "servers": payload.get("servers", [])}
    sections.append(("info", yaml.safe_dump(header, sort_keys=False)))

    paths = payload.get("paths", {})
    if isinstance(paths, dict):
        for route, methods in paths.items():
            if not isinstance(methods, dict):
                continue
            for method, operation in methods.items():
                if method.startswith("x-"):
                    continue
                if not isinstance(operation, dict):
                    continue
                operation_id = str(operation.get("operationId", f"{method}-{route}"))
                slug = re.sub(r"[^a-z0-9]+", "-", operation_id.lower()).strip("-")
                body = yaml.safe_dump(
                    {"path": route, "method": method.upper(), "operation": operation},
                    sort_keys=False,
                )
                sections.append((slug, f"# {title} v{version}\n\n{body}"))

    components = payload.get("components", {})
    if isinstance(components, dict):
        schemas = components.get("schemas", {})
        if isinstance(schemas, dict):
            for schema_name, schema_body in schemas.items():
                slug = re.sub(r"[^a-z0-9]+", "-", str(schema_name).lower()).strip("-")
                body = yaml.safe_dump({schema_name: schema_body}, sort_keys=False)
                sections.append((f"schema-{slug}", f"# Schema: {schema_name}\n\n{body}"))

    return sections


def _infer_document_type(relative_path: Path) -> DocumentType:
    """Map corpus folder layout to document type."""
    parts = relative_path.parts
    if "requirements" in parts:
        return DocumentType.REQUIREMENT
    if "contracts" in parts:
        return DocumentType.CONTRACT
    if "adrs" in parts:
        return DocumentType.ADR
    if "test-plans" in parts:
        return DocumentType.TEST_PLAN
    if "policies" in parts:
        return DocumentType.RELEASE_POLICY
    if "runbooks" in parts:
        return DocumentType.RUNBOOK
    if "incidents" in parts:
        return DocumentType.INCIDENT
    if "ownership" in parts:
        return DocumentType.OWNERSHIP
    return DocumentType.REQUIREMENT


def _infer_service(relative_path: Path, front_matter: dict[str, object]) -> str | None:
    if "service" in front_matter:
        return str(front_matter["service"])
    stem = relative_path.stem
    if stem.startswith("svc-"):
        return stem
    return None


def _parse_front_matter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    front_matter_raw = text[4:end]
    body = text[end + 5 :]
    parsed = yaml.safe_load(front_matter_raw)
    return parsed if isinstance(parsed, dict) else {}, body


def chunk_markdown_file(
    *,
    path: Path,
    knowledge_root: Path,
    domain_pack_id: str,
    document_id: str,
    document_type: DocumentType,
    version: str,
    effective_date: str,
    service: str | None = None,
) -> list[KnowledgeChunk]:
    """Chunk a markdown knowledge document."""
    raw = path.read_text(encoding="utf-8")
    front_matter, body = _parse_front_matter(raw)
    version = str(front_matter.get("version", version))
    effective_date = str(front_matter.get("effective_date", effective_date))
    service = _infer_service(path.relative_to(knowledge_root), front_matter) or service

    if document_type is DocumentType.INCIDENT:
        sections = _split_incident_sections(body)
    else:
        sections = _split_markdown_by_heading(body)

    merged = _merge_sections(sections, min_tokens=TARGET_MIN_TOKENS)
    relative = path.relative_to(knowledge_root.parent.parent).as_posix()
    chunks: list[KnowledgeChunk] = []
    for section_id, content in merged:
        if not content.strip():
            continue
        chunks.append(
            KnowledgeChunk(
                content=content.strip(),
                metadata=ChunkMetadata(
                    document_id=document_id,
                    section_id=section_id,
                    document_type=document_type,
                    service=service,
                    version=version,
                    effective_date=effective_date,
                    source_path=relative,
                    domain_pack_id=domain_pack_id,
                ),
            )
        )
    return chunks


def chunk_openapi_file(
    *,
    path: Path,
    knowledge_root: Path,
    domain_pack_id: str,
    document_id: str,
    version: str,
    effective_date: str,
    service: str | None = None,
) -> list[KnowledgeChunk]:
    """Chunk an OpenAPI YAML/JSON contract."""
    text = path.read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        return []

    info = payload.get("info", {})
    if isinstance(info, dict):
        version = str(info.get("version", version))
        effective_date = str(info.get("x-effective-date", effective_date))

    sections = _split_openapi(path, payload)
    merged = _merge_sections(sections, min_tokens=TARGET_MIN_TOKENS)
    relative = path.relative_to(knowledge_root.parent.parent).as_posix()
    chunks: list[KnowledgeChunk] = []
    for section_id, content in merged:
        chunks.append(
            KnowledgeChunk(
                content=content.strip(),
                metadata=ChunkMetadata(
                    document_id=document_id,
                    section_id=section_id,
                    document_type=DocumentType.CONTRACT,
                    service=service,
                    version=version,
                    effective_date=effective_date,
                    source_path=relative,
                    domain_pack_id=domain_pack_id,
                ),
            )
        )
    return chunks


def iter_knowledge_files(knowledge_root: Path) -> Iterator[Path]:
    """Yield supported knowledge source files under a domain pack."""
    for pattern in ("**/*.md", "**/*.yaml", "**/*.yml", "**/*.json"):
        for path in sorted(knowledge_root.glob(pattern)):
            if path.is_file():
                yield path


def chunk_knowledge_corpus(
    *,
    knowledge_root: Path,
    domain_pack_id: str,
    default_version: str = "1.0.0",
    default_effective_date: str | None = None,
) -> list[KnowledgeChunk]:
    """Chunk all knowledge files for a domain pack."""
    effective = default_effective_date or date.today().isoformat()
    chunks: list[KnowledgeChunk] = []

    for path in iter_knowledge_files(knowledge_root):
        relative = path.relative_to(knowledge_root)
        document_type = _infer_document_type(relative)
        document_id = relative.as_posix().rsplit(".", 1)[0]
        service = None
        if document_type is DocumentType.CONTRACT:
            service = path.stem if path.stem.startswith("svc-") else None

        if path.suffix.lower() in {".yaml", ".yml", ".json"} and "contracts" in relative.parts:
            chunks.extend(
                chunk_openapi_file(
                    path=path,
                    knowledge_root=knowledge_root,
                    domain_pack_id=domain_pack_id,
                    document_id=document_id,
                    version=default_version,
                    effective_date=effective,
                    service=service,
                )
            )
            continue

        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            content = json.dumps(payload, indent=2)
            chunks.append(
                KnowledgeChunk(
                    content=content,
                    metadata=ChunkMetadata(
                        document_id=document_id,
                        section_id="body",
                        document_type=document_type,
                        service=service,
                        version=default_version,
                        effective_date=effective,
                        source_path=path.relative_to(knowledge_root.parent.parent).as_posix(),
                        domain_pack_id=domain_pack_id,
                    ),
                )
            )
            continue

        chunks.extend(
            chunk_markdown_file(
                path=path,
                knowledge_root=knowledge_root,
                domain_pack_id=domain_pack_id,
                document_id=document_id,
                document_type=document_type,
                version=default_version,
                effective_date=effective,
                service=service,
            )
        )

    return chunks
