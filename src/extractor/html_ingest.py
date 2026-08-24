"""HTML ingestion: a second adapter onto the same IngestedDocument shape the
PDF path (ingest.py) produces.

HTML has no OCR problem — it's already structured text — so this skips
straight to using the document's own headings as the coarse structural
signal that stands in for PDF page numbers, giving each clause-sized section
its own evidence anchor. This exists to prove the extension point described
in docs/SOLUTION_DESIGN.md §2.3: everything downstream of ingestion
(segmentation, extraction, grounding) is format-agnostic once you have text
plus a location to cite as evidence.

Deliberately minimal — built to demonstrate the pattern on well-formed
regulatory HTML, not to handle arbitrary real-world markup (sanitization,
script-rendered content, malformed HTML are out of scope for this POC).
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from .ingest import IngestedDocument, Page

# Heading levels treated as section boundaries. A real regulatory page is
# usually a flat run of h1 (title) + h2 (clause headings); deeper heuristics
# (h3+, <section> boundaries) are the kind of robustness left for production.
SECTION_TAGS = {"h1", "h2"}


def ingest_html(path: str) -> IngestedDocument:
    with open(path, encoding="utf-8") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    root = soup.body or soup
    sections: list[list[str]] = [[]]

    for element in root.descendants:
        name = getattr(element, "name", None)
        if name in SECTION_TAGS and sections[-1]:
            sections.append([])
            continue
        if name is None:
            text = str(element).strip()
            if text:
                sections[-1].append(text)

    pages = [
        Page(number=i + 1, text=" ".join(chunks), source="native")
        for i, chunks in enumerate(sections)
        if chunks
    ]
    if not pages:
        pages = [Page(number=1, text=root.get_text(" ", strip=True), source="native")]

    return IngestedDocument(pages=pages)
