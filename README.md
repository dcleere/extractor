# extractor

A small proof-of-concept for regulatory clause & entity extraction:
ingest a regulatory PDF or HTML document, segment it into clauses
(definitions, obligations, exemptions, penalties, effective dates, scope),
extract legally significant entities linked to those clauses, and attach
source evidence plus a review signal to every extraction.

I considered the OCR and validation steps as highest risk since these will flow to downstream systems. Ensuring that these steps operate effectively, can scale efficiently and monitored closely is extremely important in building a trustworthy system.

The full end-to-end system design (production architecture, OCR trade-offs,
evaluation plan, rollout strategy) is in
[`docs/SOLUTION_DESIGN.md`](docs/SOLUTION_DESIGN.md). This README covers only
the POC.

## How it works

1. **Ingest** — two adapters share everything downstream. For PDFs
   (`src/extractor/ingest.py`), each page's native text layer is used
   directly where present; where it isn't (a scan), the page is rendered to
   an image and transcribed by Claude vision as an OCR fallback. (The design
   doc explains why a dedicated OCR/Document-AI service, not an LLM, is the
   right default at production scale). For HTML
   (`src/extractor/html_ingest.py`), there's no OCR problem — the document's
   own headings stand in for PDF page numbers as the section/evidence
   anchor. Both produce the same in-memory document shape, so segmentation
   and extraction below don't know or care which one ran.
2. **Segment & extract** (`src/extractor/segment_extract.py`) — a single
   schema-constrained Claude call segments the document into clauses and
   extracts entities linked to them, quoting verbatim source text for every
   extraction.
3. **Ground & triage** (`src/extractor/grounding.py`) — every extraction is
   verified against the text ingestion independently produced, and a
   `auto_publish` / `needs_review` / `rejected` flag is derived from that,
   OCR provenance, and clause/entity type. Specifically:
   both `text` and `evidence.quote` must check out (a faithful quote beside a
   fabricated `text` is exactly the failure that matters, since downstream
   consumes `text`); a citation that points at the wrong pages is reported as
   `page_range_mismatch` rather than lumped in with fabrication; a
   gratuitously wide claimed page range goes to review, since the model picks
   its own range and a wide one makes its own check easier to pass; and
   entities are grounded inside their parent clause rather than a whole page,
   because a four-character entity matches almost any page. This is not optimal as a model is typically overconfident and not designed to provide this type of output.
4. **Output** — a structured JSON file per document
   (`schema.py:ExtractionResult`) written to `output/`.

## Run it

Requires [`uv`](https://docs.astral.sh/uv/) and python.

```bash
uv sync
cp .env.example .env   # then add your ANTHROPIC_API_KEY
uv run extractor
```

This runs against the bundled sample document (`docs/sample_doc.pdf`) and
writes `output/sample_doc.extraction.json`, printing a summary to the
terminal. Run against a different PDF or HTML file with:

```bash
uv run extractor path/to/other.pdf
uv run extractor docs/sample_regulation.html   # a synthetic HTML regulation
uv run extractor docs/screenshot_eg.png   # a png screenshot of a page in sample doc
```

Pre-generated examples are checked in at `examples/sample_output.json` (PDF)
and `examples/sample_output_html.json` (HTML) and `examples/sample_png_output.json` (PNG) so the shape of the output is inspectable without running anything.

## Tests

```bash
uv run pytest
```

No API key needed — every Claude call is behind a stub. Covers: the PDF native-text/
vision-OCR fork actually forks (`tests/test_ingest.py`), HTML section
splitting (`tests/test_html_ingest.py`), the grounding/triage rules
(`tests/test_grounding.py`), the JSON-schema sanitizer
(`tests/test_claude_client.py`), the checked-in examples still validating
against the current schema (`tests/test_schema.py`), and the full
ingest → extract → ground pipeline wired together with a stub client
(`tests/test_pipeline.py`). `uv run extractor` itself still hits the real,
paid API on purpose — that's a separate, deliberate step.

## Notes

- Requires an Anthropic API key (`ANTHROPIC_API_KEY`) — see `.env.example`.
- This is intentionally narrow: one document, one pass, no queue/storage/
  review-UI. See `docs/SOLUTION_DESIGN.md` §5 for what's deferred and why.
- **There is deliberately no confidence score.** An earlier version blended
  hand-picked penalties into a float and produced values like `0.7695` — a
  number that reads as calibrated when nothing had been fitted, which in a
  compliance product is worse than no number, because downstream trusts the
  digits. Claude does not emit logprobs from its API. Every decision is now a boolean fact, and `review_reasons` names the
  facts that fired. It is a blunt flag that leans toward risk. 
  Reintroducing a *fitted* score is the calibration work in `docs/SOLUTION_DESIGN.md` §4.
- **Known limitation:** on OCR'd pages grounding degrades to a *copy-fidelity*
  check. The page text came from Claude vision and the extraction model quoted
  from that same transcription, so both sides trace back to the model and a
  mis-transcribed figure will still verify as "grounded". Closing that needs a
  second, independent OCR pass, from a more capable model; today those extractions are simply routed to
  review rather than trusted. On native-text pages there's no such
  circularity — the comparison is against PyMuPDF's independent extraction.
