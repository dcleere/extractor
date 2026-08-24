"""PDF ingestion: proves the native-text/vision-OCR fork actually forks."""

from __future__ import annotations

import pymupdf

from extractor.ingest import ingest_pdf
from helpers import SAMPLE_PDF, PoisonPillClient, StubClaudeClient


def test_native_text_pages_never_call_the_api():
    doc = ingest_pdf(str(SAMPLE_PDF), PoisonPillClient())
    assert doc.page_count == 8
    assert {p.source for p in doc.pages} == {"native"}


def test_pages_without_a_text_layer_fall_back_to_vision(tmp_path):
    blank_pdf = tmp_path / "blank.pdf"
    pdf = pymupdf.open()
    pdf.new_page()
    pdf.save(str(blank_pdf))
    pdf.close()

    client = StubClaudeClient(transcription="transcribed by the stub")
    doc = ingest_pdf(str(blank_pdf), client)

    assert doc.page_count == 1
    assert doc.pages[0].source == "claude_vision"
    assert doc.pages[0].text == "transcribed by the stub"
