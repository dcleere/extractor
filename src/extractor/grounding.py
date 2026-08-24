"""Hallucination guard + review triage.

Claude is asked to quote text verbatim, but LLMs still occasionally hallucinate.
Before anything is trusted downstream, we verify what the model returned against
the text ingestion independently extracted, and turn that (plus OCR provenance
and clause/entity "blast radius") into a review_flag with an explicit list of
reasons. This is deliberately rule-based rather than another model call: the
whole point is a signal to a compliance reviewer.

There is no confidence score, on purpose. An earlier version blended
hand-picked penalties into a float, which produced values like 0.7695 that read
as calibrated when nothing had been fitted. Every decision here is instead a
boolean fact about the extraction, and the reasons say which facts fired. The
cost is bluntness: with no calibrated score there is no principled basis for
waving a penalty or a deadline through unreviewed, so high-impact types never
auto-publish. That is the honest consequence, and §4 of the design doc covers
what would have to be measured to soften it.

The check is only as independent as its source. On native-text pages it is a
real check. The quote is compared against PyMuPDF's extraction, which the
model never saw. On OCR'd pages both sides trace back to Claude, so it degrades
to a copy-fidelity check and a mis-transcribed figure still verifies as
"grounded"; those are routed to review rather than trusted. Closing it properly
needs a second, independent OCR pass. See docs/SOLUTION_DESIGN.md §2.2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .ingest import IngestedDocument
from .schema import HIGH_IMPACT_TYPES, Clause, Entity

# how close a fuzzy match must be to count as found
FUZZY_MATCH_THRESHOLD = 0.9
# below this a span is too generic to be evidence on its own -- "EUR", "ESMA"
# will match almost any source text
MIN_EVIDENCE_QUOTE_CHARS = 12
# pages a citation may exceed the span the quote actually occupies before it
# reads as hedging rather than a real multi-page clause
OVERBROAD_RANGE_TOLERANCE = 1
# caps _locate_span's search width, to stay linear-ish on 300-page documents
MAX_SPAN_SEARCH_PAGES = 4

# reasons that on their own block auto-publication
BLOCKING_REASONS = frozenset(
    {
        "ungrounded_span",
        "page_range_mismatch",
        "text_not_grounded",
        "not_found_in_parent_clause",
        "quote_too_short_for_independent_evidence",
        "orphan_clause_id",
        "parent_clause_ungrounded",
        "ocr_derived_source",
        "overbroad_page_range",
        "high_impact_type",
    }
)


# the outcome of verifying one extraction against the source
@dataclass
class GroundingCheck:
    grounded: bool
    # set when the span appears nowhere in the document at all, i.e. probable
    # fabrication rather than a mis-stated citation
    fabricated: bool = False
    reasons: list[str] = field(default_factory=list)


# normalise text
def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


# locate a span in the source: exact substring first, then fuzzy
def _match(needle: str, haystack: str) -> tuple[bool, bool]:
    """Returns (found, exact)."""
    needle_norm = _normalize(needle)
    haystack_norm = _normalize(haystack)
    if not needle_norm or not haystack_norm:
        return False, False
    if needle_norm in haystack_norm:
        return True, True

    # fuzzy match via longest common substring ratio
    matcher = SequenceMatcher(None, haystack_norm, needle_norm)
    match = matcher.find_longest_match(0, len(haystack_norm), 0, len(needle_norm))
    return (match.size / len(needle_norm)) >= FUZZY_MATCH_THRESHOLD, False


# narrowest contiguous page span actually containing the quote, else None
def _locate_span(quote: str, document: IngestedDocument) -> tuple[int, int] | None:
    numbers = [p.number for p in document.pages]
    if not numbers:
        return None
    last = numbers[-1]
    # widen the window until it matches, capped by MAX_SPAN_SEARCH_PAGES
    for width in range(1, MAX_SPAN_SEARCH_PAGES + 1):
        for start in numbers:
            end = start + width - 1
            if end > last:
                break
            found, _ = _match(quote, document.text_for_range(start, end))
            if found:
                return start, end
    return None


# verify an extraction's quote AND its text against the pages it cites
def _check_against_source(
    *,
    quote: str,
    text: str,
    page_start: int,
    page_end: int,
    document: IngestedDocument,
) -> GroundingCheck:
    source = document.text_for_range(page_start, page_end)
    quote_found, _ = _match(quote, source)
    text_found, _ = _match(text, source)

    reasons: list[str] = []

    if not quote_found:
        # separate "this text doesn't exist" from "it exists, wrong pages
        # cited" -- different triage, and only the latter is salvageable
        if _locate_span(quote, document) is not None:
            reasons.append("page_range_mismatch")
            return GroundingCheck(grounded=False, reasons=reasons)
        reasons.append("ungrounded_span")
        return GroundingCheck(grounded=False, fabricated=True, reasons=reasons)

    if not text_found:
        # a verbatim quote beside a fabricated `text` is the failure that
        # matters: downstream consumes `text`, not the quote
        reasons.append("text_not_grounded")
        return GroundingCheck(grounded=False, reasons=reasons)

    # a short span matched against a whole page proves little, and there is no
    # verified parent clause narrowing scope here
    if len(_normalize(quote)) < MIN_EVIDENCE_QUOTE_CHARS:
        reasons.append("quote_too_short_for_independent_evidence")

    # the model picks its own citation range, and a wide one makes its own
    # check easier to pass
    actual_span = _locate_span(quote, document)
    if actual_span is not None:
        claimed_width = page_end - page_start + 1
        actual_width = actual_span[1] - actual_span[0] + 1
        if claimed_width > actual_width + OVERBROAD_RANGE_TOLERANCE:
            reasons.append("overbroad_page_range")

    return GroundingCheck(grounded=True, reasons=reasons)


# turn the accumulated facts into one flag -- no arithmetic, just a table
def _flag(check: GroundingCheck, *, high_impact: bool) -> tuple[str, list[str]]:
    reasons = list(check.reasons)

    if high_impact:
        reasons.append("high_impact_type")

    # nowhere in the document: don't publish it, don't spend a reviewer on it
    if check.fabricated:
        return "rejected", reasons

    if any(r in BLOCKING_REASONS for r in reasons):
        return "needs_review", reasons

    return "auto_publish", reasons


# ground a clause against the pages it cites
def ground_clause(clause: Clause, document: IngestedDocument) -> Clause:
    check = _check_against_source(
        quote=clause.evidence.quote,
        text=clause.text,
        page_start=clause.evidence.page_start,
        page_end=clause.evidence.page_end,
        document=document,
    )
    ocr_derived = document.any_vision_pages(
        clause.evidence.page_start, clause.evidence.page_end
    )
    if ocr_derived:
        check.reasons.append("ocr_derived_source")

    review_flag, reasons = _flag(check, high_impact=clause.type in HIGH_IMPACT_TYPES)

    clause.grounded = check.grounded
    clause.ocr_derived = ocr_derived
    clause.review_flag = review_flag
    clause.review_reasons = reasons
    return clause


# ground an entity inside its parent clause where possible
def ground_entity(
    entity: Entity,
    document: IngestedDocument,
    clauses_by_id: dict[str, Clause] | None = None,
) -> Entity:
    """Scoping to the clause rather than the page matters most for short
    entities: "ESMA" matches almost any page, but requiring it inside the
    clause it claims to belong to is a real constraint. Only usable if that
    clause was itself verified -- otherwise we would be checking model output
    against model output."""
    clause = (clauses_by_id or {}).get(entity.clause_id)
    reasons: list[str] = []

    if clause is None:
        reasons.append("orphan_clause_id")
    elif not clause.grounded:
        reasons.append("parent_clause_ungrounded")
        clause = None

    if clause is not None:
        # narrow scope: must appear within the verified parent clause, not
        # merely somewhere on the cited pages
        quote_found, _ = _match(entity.evidence.quote, clause.text)
        text_found, _ = _match(entity.text, clause.text)
        if quote_found and text_found:
            check = GroundingCheck(grounded=True, reasons=reasons)
        else:
            reasons.append("not_found_in_parent_clause")
            check = GroundingCheck(grounded=False, reasons=reasons)
    else:
        # no usable parent, fall back to the cited page range
        check = _check_against_source(
            quote=entity.evidence.quote,
            text=entity.text,
            page_start=entity.evidence.page_start,
            page_end=entity.evidence.page_end,
            document=document,
        )
        check.reasons = reasons + check.reasons

    if document.any_vision_pages(entity.evidence.page_start, entity.evidence.page_end):
        check.reasons.append("ocr_derived_source")

    review_flag, reasons = _flag(check, high_impact=entity.type in HIGH_IMPACT_TYPES)

    entity.grounded = check.grounded
    entity.review_flag = review_flag
    entity.review_reasons = reasons
    return entity
