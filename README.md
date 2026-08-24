# extractor


A small proof-of-concept for regulatory clause & entity extraction:
ingest a regulatory PDF or HTML document, segment it into clauses
(definitions, obligations, exemptions, penalties, effective dates, scope),
extract legally significant entities linked to those clauses, and attach
source evidence plus a review signal to every extraction.

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
   because a four-character entity matches almost any page. There is no
   confidence score — see the note below.
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
splitting (`tests/test_html_ingest.py`), the grounding/triage rules
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
- **There is deliberately no confidence score.** An earlier version blended
  hand-picked penalties into a float and produced values like `0.7695` — a
  number that reads as calibrated when nothing had been fitted, which in a
  compliance product is worse than no number, because downstream trusts the
  digits. Every decision is now a boolean fact, and `review_reasons` names the
  facts that fired. The cost is bluntness: with no calibrated score there is
  no principled basis for waving a penalty or a deadline through unreviewed,
  so **high-impact types never auto-publish**. Reintroducing a *fitted* score
  is the calibration work in `docs/SOLUTION_DESIGN.md` §4.
- `examples/sample_output.json` is a real, verified run against
  `docs/sample_doc.pdf` — 43 clauses, 18 entities, 56 auto-published, 3 for
  review, 2 rejected. Each flag is a different failure mode: a `scope` clause
  is `rejected` as `ungrounded_span` (it spans a page break where the document
  repeats a header block mid-sentence, so the span isn't contiguous); two
  clauses are held back solely by `high_impact_type`; and two
  `monetary_threshold` entities inherit `parent_clause_ungrounded` from that
  same broken clause. A naive quote-only check would have published all of
  them.
- `examples/sample_output_html.json` is a real, verified run against
  `docs/sample_regulation.html`, a fictional specimen written to exercise the
  HTML adapter (§2.3a) — 12 clauses, 14 entities, 17 auto-published, 9 for
  review. Seven of those nine are `high_impact_type` alone, which is the
  policy above doing exactly what it says: this document is mostly penalties,
  thresholds and dates, so most of it needs a human. The other two are
  entities attributed to clauses they don't appear in.
- **Known limitation:** on OCR'd pages grounding degrades to a *copy-fidelity*
  check. The page text came from Claude vision and the extraction model quoted
  from that same transcription, so both sides trace back to the model and a
  mis-transcribed figure will still verify as "grounded". Closing that needs a
  second, independent OCR pass; today those extractions are simply routed to
  review rather than trusted. On native-text pages there's no such
  circularity — the comparison is against PyMuPDF's independent extraction.
