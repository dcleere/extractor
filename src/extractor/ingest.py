"""Ingestion: native text layer first, Claude vision only as an OCR
fallback for pages that don't have a usable text layer (i.e. scans).

This hybrid is deliberate: a dedicated text layer costs nothing to read and is
exact, whereas every page routed through Claude vision costs money, latency,
and is a step removed from the literal source pixels. Reserving the LLM for
pages that actually need it is the cheap, obvious win before ever reaching
for a general-purpose OCR/document-AI service (see docs/SOLUTION_DESIGN.md).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import pymupdf

from .claude_client import ClaudeClient

# Below this many characters of extracted text, treat a page as "no usable
# text layer" and fall back to vision OCR rather than trust a near-empty page.
MIN_NATIVE_TEXT_CHARS = 20

RENDER_DPI = 175

OCR_SYSTEM_PROMPT = (
    "You are a strict OCR transcription engine. Transcribe the visible text of "
    "this document page exactly as it appears, preserving reading order, "
    "paragraph breaks, tables, and numbering. Do not summarize, interpret, translate, "
    "or add anything that is not printed on the page. Output plain text only."
)


@dataclass
class Page:
    number: int  # 1-indexed
    text: str
    source: str  # "native" | "claude_vision"


@dataclass
class IngestedDocument:
    pages: list[Page]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def text_for_range(self, page_start: int, page_end: int) -> str:
        return "\n".join(
            p.text for p in self.pages if page_start <= p.number <= page_end
        )

    def as_prompt_text(self) -> str:
        """Full document text with page boundary markers, for segmentation."""
        parts = []
        for p in self.pages:
            parts.append(f"[PAGE {p.number}]\n{p.text}")
        return "\n\n".join(parts)

    def any_vision_pages(self, page_start: int, page_end: int) -> bool:
        return any(
            p.source == "claude_vision"
            for p in self.pages
            if page_start <= p.number <= page_end
        )


def _render_page_png_b64(page: pymupdf.Page) -> str:
    pix = page.get_pixmap(dpi=RENDER_DPI)
    return base64.standard_b64encode(pix.tobytes("png")).decode("ascii")


def ingest_pdf(path: str, client: ClaudeClient) -> IngestedDocument:
    doc = pymupdf.open(path)
    pages: list[Page] = []

    for index in range(doc.page_count):
        pdf_page = doc[index]
        number = index + 1
        native_text = pdf_page.get_text("text").strip()

        if len(native_text) >= MIN_NATIVE_TEXT_CHARS:
            pages.append(Page(number=number, text=native_text, source="native"))
            continue

        image_b64 = _render_page_png_b64(pdf_page)
        transcribed = client.transcribe_image(image_b64, system=OCR_SYSTEM_PROMPT)
        pages.append(Page(number=number, text=transcribed, source="claude_vision"))

    doc.close()
    return IngestedDocument(pages=pages)
