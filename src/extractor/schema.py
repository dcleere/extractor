"""Structured output schema for the extraction pipeline.

These models are the single source of truth: they define both the JSON shape
we persist and the JSON schema handed to Claude for structured output, so the
model's response and our on-disk records can never drift apart.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

ClauseType = Literal[
    "definition",
    "obligation",
    "exemption",
    "penalty",
    "effective_date",
    "scope",
    "other",
]

EntityType = Literal[
    "organisation",
    "person",
    "location",
    "date",
    "monetary_threshold",
    "product_category",
    "regulation_reference",
]

ReviewFlag = Literal["auto_publish", "needs_review", "rejected"]

# Clause/entity types the brief calls out as high-impact. These never
# auto-publish: without a calibrated score there is no principled basis for
# waving a penalty or a deadline through unreviewed.
HIGH_IMPACT_TYPES = {"penalty", "monetary_threshold", "effective_date"}


class Evidence(BaseModel):
    """The source text this extraction is grounded in."""

    page_start: int = Field(..., description="1-indexed first page the quote appears on")
    page_end: int = Field(..., description="1-indexed last page the quote appears on")
    quote: str = Field(..., description="Exact verbatim text from the source document")


class Entity(BaseModel):
    id: str
    clause_id: str = Field(..., description="id of the Clause this entity belongs to")
    type: EntityType
    text: str = Field(..., description="Exact verbatim entity text as it appears in the source")
    normalized_value: str | None = Field(
        default=None,
        description="Normalized form where applicable, e.g. ISO date or amount+currency",
    )
    evidence: Evidence

    # Populated by the grounding stage, not by the model.
    grounded: bool = False
    review_flag: ReviewFlag = "needs_review"
    review_reasons: list[str] = Field(default_factory=list)


class Clause(BaseModel):
    id: str
    type: ClauseType
    heading: str | None = None
    text: str = Field(..., description="Exact verbatim clause text as it appears in the source")
    evidence: Evidence

    # Populated by ingest/grounding, not by the model.
    ocr_derived: bool = False
    grounded: bool = False
    review_flag: ReviewFlag = "needs_review"
    review_reasons: list[str] = Field(default_factory=list)


class DocumentMeta(BaseModel):
    id: str
    filename: str
    page_count: int
    source_hash: str
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    model: str
    prompt_version: str


class ExtractionResult(BaseModel):
    document: DocumentMeta
    clauses: list[Clause]
    entities: list[Entity]


class ModelExtraction(BaseModel):
    """The shape we ask Claude to return directly: clauses + entities, before
    grounding/confidence post-processing is applied."""

    clauses: list[Clause]
    entities: list[Entity]
