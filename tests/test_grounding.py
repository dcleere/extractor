"""Grounding & confidence: all pure logic, no API involved."""

from __future__ import annotations

from extractor.grounding import ground_clause, ground_entity
from extractor.ingest import IngestedDocument, Page
from extractor.schema import Clause, Entity, Evidence


def _doc(*, page1: str = "", page2: str = "", vision_pages: tuple[int, ...] = ()) -> IngestedDocument:
    pages = [
        Page(number=1, text=page1, source="claude_vision" if 1 in vision_pages else "native"),
        Page(number=2, text=page2, source="claude_vision" if 2 in vision_pages else "native"),
    ]
    return IngestedDocument(pages=pages)


def _clause(**overrides) -> Clause:
    defaults = dict(
        id="c1",
        type="obligation",
        text="Manufacturers shall register the product.",
        evidence=Evidence(
            page_start=1, page_end=1, quote="Manufacturers shall register the product."
        ),
        model_confidence=0.9,
    )
    defaults.update(overrides)
    return Clause(**defaults)


def test_exact_match_auto_publishes():
    doc = _doc(page1="Preamble. Manufacturers shall register the product. End.")
    clause = ground_clause(_clause(), doc)
    assert clause.grounded is True
    assert clause.review_flag == "auto_publish"
    assert clause.confidence == 0.9


def test_ungrounded_span_forces_review():
    doc = _doc(page1="This page says nothing related to the quote at all.")
    clause = ground_clause(_clause(model_confidence=0.95), doc)
    assert clause.grounded is False
    assert clause.confidence <= 0.3
    assert clause.review_flag == "needs_review"
    assert "ungrounded_span" in clause.review_reasons


def test_very_low_confidence_ungrounded_is_rejected():
    doc = _doc(page1="Nothing matches here.")
    clause = ground_clause(_clause(model_confidence=0.1), doc)
    assert clause.review_flag == "rejected"


def test_high_impact_type_needs_a_stricter_bar():
    doc = _doc(page1="Preamble. Manufacturers shall register the product. End.")
    clause = ground_clause(_clause(type="penalty", model_confidence=0.7), doc)
    # Grounded, and above the default 0.6 bar, but "penalty" requires >= 0.85.
    assert clause.grounded is True
    assert clause.review_flag == "needs_review"
    assert "high_impact_type_strict_threshold" in clause.review_reasons


def test_ocr_derived_source_applies_a_confidence_penalty():
    doc = _doc(
        page1="Preamble. Manufacturers shall register the product. End.",
        vision_pages=(1,),
    )
    clause = ground_clause(_clause(model_confidence=0.9), doc)
    assert clause.ocr_derived is True
    assert clause.confidence == round(0.9 * 0.85, 4)


def test_fuzzy_match_is_grounded_with_a_penalty():
    # "the product." never appears verbatim -- the source says "product now." --
    # so this can only pass via the near-match fallback, not an exact substring.
    doc = _doc(page1="Preamble. Manufacturers shall register the product now. End.")
    clause = ground_clause(_clause(model_confidence=0.9), doc)
    assert clause.grounded is True
    assert "fuzzy_match" in clause.review_reasons
    assert clause.confidence < 0.9


def test_entity_grounding_mirrors_clause_grounding():
    doc = _doc(page1="The threshold is set at EUR 50,000 for this category.")
    entity = Entity(
        id="e1",
        clause_id="c1",
        type="monetary_threshold",
        text="EUR 50,000",
        evidence=Evidence(page_start=1, page_end=1, quote="EUR 50,000"),
        model_confidence=0.9,
    )
    grounded = ground_entity(entity, doc)
    assert grounded.grounded is True
    # monetary_threshold is high-impact: 0.9 clears the stricter 0.85 bar.
    assert grounded.review_flag == "auto_publish"
