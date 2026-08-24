"""End-to-end wiring (ingest -> extract -> ground -> assemble) with a stubbed
Claude client -- proves the pipeline is wired correctly without spending a
single real API call."""

from __future__ import annotations

from extractor.cli import _print_summary
from extractor.pipeline import run
from extractor.schema import ExtractionResult
from helpers import SAMPLE_PDF, StubClaudeClient

GROUNDED_QUOTE = "EMIRATES CONFORMITY ASSESSMENT SCHEME"  # verbatim, on page 1 of the sample PDF


def _extraction_payload() -> dict:
    return {
        "clauses": [
            {
                "id": "c1",
                "type": "scope",
                "heading": None,
                "text": GROUNDED_QUOTE,
                "evidence": {"page_start": 1, "page_end": 1, "quote": GROUNDED_QUOTE},
                "model_confidence": 0.9,
            },
            {
                "id": "c2",
                "type": "penalty",
                "heading": None,
                "text": "a clause that was never actually in the document",
                "evidence": {
                    "page_start": 1,
                    "page_end": 1,
                    "quote": "a clause that was never actually in the document",
                },
                "model_confidence": 0.95,
            },
        ],
        "entities": [
            {
                "id": "e1",
                "clause_id": "c1",
                "type": "organisation",
                "text": GROUNDED_QUOTE,
                "normalized_value": None,
                "evidence": {"page_start": 1, "page_end": 1, "quote": GROUNDED_QUOTE},
                "model_confidence": 0.9,
            }
        ],
    }


def test_pipeline_wires_ingest_extract_and_grounding_together():
    client = StubClaudeClient(extraction=_extraction_payload())
    result = run(SAMPLE_PDF, client)

    assert result.document.filename == "sample_doc.pdf"
    assert result.document.page_count == 8
    assert result.document.model == "stub-model"

    by_id = {c.id: c for c in result.clauses}
    assert by_id["c1"].review_flag == "auto_publish"
    assert by_id["c2"].review_flag == "needs_review"
    assert "ungrounded_span" in by_id["c2"].review_reasons

    assert result.entities[0].review_flag == "auto_publish"

    # This is exactly what cli.py does before writing to output/*.json.
    assert ExtractionResult.model_validate_json(result.model_dump_json()) == result


def test_cli_summary_prints_without_error(capsys):
    client = StubClaudeClient(extraction=_extraction_payload())
    result = run(SAMPLE_PDF, client)

    _print_summary(result)
    out = capsys.readouterr().out

    assert "sample_doc.pdf" in out
    assert "auto_publish" in out
    assert "needs_review" in out
