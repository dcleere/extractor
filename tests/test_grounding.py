"""Grounding & confidence: all pure logic, no API involved."""

from __future__ import annotations

from extractor.grounding import (
    OCR_CONFIDENCE_PENALTY,
    OVERBROAD_RANGE_PENALTY,
    ground_clause,
    ground_entity,
)
from extractor.ingest import IngestedDocument, Page
from extractor.schema import Clause, Entity, Evidence

CLAUSE_TEXT = "Manufacturers shall register the product."
PAGE_1 = f"Preamble. {CLAUSE_TEXT} End of section."
PAGE_2 = "Penalties may reach EUR 90,000 for repeat breaches."
PAGE_3 = "Annex I. Technical requirements are listed below."


def _doc(
    *,
    page1: str = PAGE_1,
    page2: str = PAGE_2,
    page3: str = PAGE_3,
    vision_pages: tuple[int, ...] = (),
) -> IngestedDocument:
    texts = {1: page1, 2: page2, 3: page3}
    return IngestedDocument(
        pages=[
            Page(
                number=n,
                text=texts[n],
                source="claude_vision" if n in vision_pages else "native",
            )
            for n in (1, 2, 3)
        ]
    )


def _clause(**overrides) -> Clause:
    defaults = dict(
        id="c1",
        type="obligation",
        text=CLAUSE_TEXT,
        evidence=Evidence(page_start=1, page_end=1, quote=CLAUSE_TEXT),
        model_confidence=0.9,
    )
    defaults.update(overrides)
    return Clause(**defaults)


def _entity(**overrides) -> Entity:
    defaults = dict(
        id="e1",
        clause_id="c1",
        type="organisation",
        text="Manufacturers",
        evidence=Evidence(page_start=1, page_end=1, quote="Manufacturers"),
        model_confidence=0.9,
    )
    defaults.update(overrides)
    return Entity(**defaults)


# --- clause grounding -------------------------------------------------------


def test_exact_match_auto_publishes():
    clause = ground_clause(_clause(), _doc())
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
    clause = ground_clause(_clause(type="penalty", model_confidence=0.7), _doc())
    # Grounded and above the default 0.6 bar, but "penalty" requires >= 0.85.
    assert clause.grounded is True
    assert clause.review_flag == "needs_review"
    assert "high_impact_type_strict_threshold" in clause.review_reasons


def test_ocr_derived_source_applies_a_confidence_penalty():
    clause = ground_clause(_clause(), _doc(vision_pages=(1,)))
    assert clause.ocr_derived is True
    assert clause.confidence == round(0.9 * OCR_CONFIDENCE_PENALTY, 4)
    assert "ocr_derived_source" in clause.review_reasons


def test_fuzzy_match_is_grounded_with_a_penalty():
    # The source says "product now." so "the product." is not a literal
    # substring -- this can only pass via the near-match fallback.
    doc = _doc(page1="Preamble. Manufacturers shall register the product now. End.")
    clause = ground_clause(_clause(), doc)
    assert clause.grounded is True
    assert "fuzzy_match" in clause.review_reasons
    assert clause.confidence < 0.9


def test_fabricated_text_alongside_a_faithful_quote_is_caught():
    """The quote is verbatim but `text` -- what downstream actually consumes --
    is invented. Previously this auto-published."""
    clause = ground_clause(
        _clause(text="Operators must pay a EUR 5,000,000 fine within 24 hours."),
        _doc(),
    )
    assert clause.grounded is False
    assert clause.review_flag == "needs_review"
    assert "text_not_grounded" in clause.review_reasons


def test_citation_pointing_at_the_wrong_page_is_distinguished_from_fabrication():
    quote = "Penalties may reach EUR 90,000"
    clause = ground_clause(
        _clause(
            text=quote,
            evidence=Evidence(page_start=1, page_end=1, quote=quote),  # really on page 2
        ),
        _doc(),
    )
    assert clause.grounded is False
    # The text exists -- the pointer is wrong. Different triage for a reviewer.
    assert "page_range_mismatch" in clause.review_reasons
    assert "ungrounded_span" not in clause.review_reasons


def test_overbroad_claimed_page_range_is_penalised():
    """The model picks its own citation range, so a gratuitously wide one --
    which makes its own grounding check easier to pass -- costs something."""
    quote = "Penalties may reach EUR 90,000"
    clause = ground_clause(
        _clause(
            text=quote,
            evidence=Evidence(page_start=1, page_end=3, quote=quote),  # only needs page 2
        ),
        _doc(),
    )
    assert clause.grounded is True
    assert "overbroad_page_range" in clause.review_reasons
    assert clause.confidence == round(0.9 * OVERBROAD_RANGE_PENALTY, 4)


def test_one_page_of_slack_is_tolerated():
    """A clause genuinely spanning a page break must not be punished for it --
    the tolerance is what keeps the overbroad rule from firing on real cases."""
    quote = "Penalties may reach EUR 90,000"
    clause = ground_clause(
        _clause(text=quote, evidence=Evidence(page_start=1, page_end=2, quote=quote)),
        _doc(),
    )
    assert clause.grounded is True
    assert "overbroad_page_range" not in clause.review_reasons


# --- entity grounding -------------------------------------------------------


def test_entity_is_grounded_within_its_parent_clause():
    doc = _doc()
    parent = ground_clause(_clause(), doc)
    entity = ground_entity(_entity(), doc, {parent.id: parent})
    assert entity.grounded is True
    assert entity.review_flag == "auto_publish"


def test_entity_absent_from_its_parent_clause_is_caught():
    """On the page, but not in the clause it claims to belong to -- the whole
    point of scoping entity grounding to the parent."""
    doc = _doc()
    parent = ground_clause(_clause(), doc)
    entity = ground_entity(
        _entity(text="Preamble", evidence=Evidence(page_start=1, page_end=1, quote="Preamble")),
        doc,
        {parent.id: parent},
    )
    assert entity.grounded is False
    assert "not_found_in_parent_clause" in entity.review_reasons


def test_short_quote_without_a_verified_parent_cannot_auto_publish():
    """A 3-char span matched against a whole page proves almost nothing."""
    doc = _doc()
    ungrounded_parent = ground_clause(_clause(text="fabricated clause text"), doc)
    entity = ground_entity(
        _entity(
            clause_id="c1",
            text="the",
            evidence=Evidence(page_start=1, page_end=1, quote="the"),
            model_confidence=0.99,
        ),
        doc,
        {ungrounded_parent.id: ungrounded_parent},
    )
    assert entity.review_flag == "needs_review"
    assert "quote_too_short_for_independent_evidence" in entity.review_reasons
    assert "parent_clause_ungrounded" in entity.review_reasons


def test_entity_with_an_unknown_clause_id_is_flagged():
    doc = _doc()
    entity = ground_entity(_entity(clause_id="does-not-exist"), doc, {})
    assert "orphan_clause_id" in entity.review_reasons


def test_high_impact_entity_still_honours_the_stricter_bar():
    doc = _doc()
    parent = ground_clause(
        _clause(text=PAGE_2, evidence=Evidence(page_start=2, page_end=2, quote=PAGE_2)),
        doc,
    )
    entity = ground_entity(
        _entity(
            type="monetary_threshold",
            text="EUR 90,000",
            evidence=Evidence(page_start=2, page_end=2, quote="EUR 90,000"),
            model_confidence=0.7,
        ),
        doc,
        {parent.id: parent},
    )
    assert entity.grounded is True
    assert entity.review_flag == "needs_review"
