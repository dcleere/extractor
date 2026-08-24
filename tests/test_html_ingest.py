"""HTML ingestion: no API involved at any point, so no stub is even needed."""

from __future__ import annotations

from extractor.html_ingest import ingest_html
from helpers import SAMPLE_HTML


def test_sections_split_on_headings():
    doc = ingest_html(str(SAMPLE_HTML))
    assert doc.page_count == 7
    assert all(p.source == "native" for p in doc.pages)
    assert "Definitions" in doc.pages[1].text
    assert "Effective Date" in doc.pages[-1].text


def test_evidence_is_locatable_by_section():
    doc = ingest_html(str(SAMPLE_HTML))
    # Section 6 is "Penalties" in the fixture document.
    text = doc.text_for_range(6, 6)
    assert "500,000" in text
