# extractor


A small proof-of-concept for regulatory clause & entity extraction:
ingest a regulatory PDF or HTML document, segment it into clauses
(definitions, obligations, exemptions, penalties, effective dates, scope),
extract legally significant entities linked to those clauses, and attach
source evidence plus a confidence/review signal to every extraction.

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
   right default at production scale — Claude vision is used here to keep
   the POC self-contained on one provider.) For HTML
   (`src/extractor/html_ingest.py`), there's no OCR problem — the document's
   own headings stand in for PDF page numbers as the section/evidence
   anchor. Both produce the same in-memory document shape, so segmentation
   and extraction below don't know or care which one ran.
2. **Segment & extract** (`src/extractor/segment_extract.py`) — a single
   schema-constrained Claude call segments the document into clauses and
   extracts entities linked to them, quoting verbatim source text for every
   extraction.
3. **Ground & score** (`src/extractor/grounding.py`) — every quoted span is
   verified against the actual source text it claims to come from
   (hallucination guard), and a confidence + `auto_publish` /
   `needs_review` / `rejected` signal is computed from grounding, OCR
   provenance, and clause/entity type.
4. **Output** — a structured JSON file per document
   (`schema.py:ExtractionResult`) written to `output/`.

## Run it

Requires [`uv`](https://docs.astral.sh/uv/).

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
uv run extractor docs/sample_regulation.html   # a synthetic HTML regulation, bundled for verification
```

Pre-generated examples are checked in at `examples/sample_output.json` (PDF)
and `examples/sample_output_html.json` (HTML) so the shape of the output is
inspectable without running anything.

## Tests

```bash
uv run pytest
```

No API key needed — every Claude call is behind a stub or a poison-pill
client that fails loudly if it's ever hit. Covers: the PDF native-text/
vision-OCR fork actually forks (`tests/test_ingest.py`), HTML section
splitting (`tests/test_html_ingest.py`), the grounding/confidence rules
(`tests/test_grounding.py`), the JSON-schema sanitizer
(`tests/test_claude_client.py`), the checked-in examples still validating
against the current schema (`tests/test_schema.py`), and the full
ingest → extract → ground pipeline wired together with a stub client
(`tests/test_pipeline.py`). `uv run extractor` itself still hits the real,
paid API on purpose — that's a separate, deliberate step.

## Notes

- Requires an Anthropic API key (`ANTHROPIC_API_KEY`). No key is included in
  this repo — see `.env.example`.
- The bundled sample document happens to be fully digital-native (every page
  has a text layer), so the OCR fallback path isn't exercised against it —
  it's implemented and can be exercised with a scanned PDF.
- This is intentionally narrow: one document, one pass, no queue/storage/
  review-UI. See `docs/SOLUTION_DESIGN.md` §6 for what's deferred and why.
- `examples/sample_output.json` is a real, verified run against
  `docs/sample_doc.pdf`. One clause in it is flagged `needs_review` with
  reason `ungrounded_span` even though the extracted text is accurate — it
  spans a page break where the source document repeats a header block
  ("Identification no.: CARL-01...") between the two halves of the sentence,
  so the verbatim-quote check can't confirm it as contiguous. Left in
  deliberately: it's a real example of the grounding check being
  conservative rather than silently trusting a plausible-looking span, which
  is the trade-off the design favors (see `docs/SOLUTION_DESIGN.md` §2.5/§3).
- `examples/sample_output_html.json` is a real, verified run against
  `docs/sample_regulation.html`, a fictional specimen document written to
  exercise the HTML adapter (§2.3a). Every extraction in it comes back
  `auto_publish` — with heading-delimited sections lining up exactly with
  clause boundaries, there's no cross-section artifact like the PDF's page
  break above, which is itself a useful data point on where grounding
  failures tend to come from.
