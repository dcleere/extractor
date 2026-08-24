"""Orchestrates ingest -> segment/extract -> grounding into one ExtractionResult."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from .claude_client import ClaudeClient
from .grounding import ground_clause, ground_entity
from .html_ingest import ingest_html
from .ingest import IngestedDocument, ingest_pdf
from .schema import DocumentMeta, ExtractionResult
from .segment_extract import PROMPT_VERSION, extract

HTML_SUFFIXES = {".html", ".htm"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _ingest(doc_path: Path, client: ClaudeClient) -> IngestedDocument:
    if doc_path.suffix.lower() in HTML_SUFFIXES:
        return ingest_html(str(doc_path))
    return ingest_pdf(str(doc_path), client)


def run(doc_path: str | Path, client: ClaudeClient) -> ExtractionResult:
    doc_path = Path(doc_path)
    if not doc_path.is_file():
        raise FileNotFoundError(
            f"Document not found at {doc_path}. Place a regulatory PDF or HTML "
            "file there, or pass a different path: `uv run extractor <path>`."
        )

    document = _ingest(doc_path, client)
    model_extraction = extract(document, client)

    # Clauses first: entity grounding is scoped to the parent clause, and can
    # only use it as scope once the clause itself has been verified.
    clauses = [ground_clause(c, document) for c in model_extraction.clauses]
    clauses_by_id = {c.id: c for c in clauses}
    entities = [
        ground_entity(e, document, clauses_by_id) for e in model_extraction.entities
    ]

    meta = DocumentMeta(
        id=str(uuid.uuid4()),
        filename=doc_path.name,
        page_count=document.page_count,
        source_hash=_sha256(doc_path),
        model=client.model,
        prompt_version=PROMPT_VERSION,
    )

    return ExtractionResult(document=meta, clauses=clauses, entities=entities)
