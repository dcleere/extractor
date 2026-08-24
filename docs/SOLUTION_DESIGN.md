# Solution Design — Regulatory Clause & Entity Extraction

## 1. Problem & assumptions

Ingest regulatory documents (regulations, directives, standards, enforcement
notices) and reliably produce, for each document:

- **Clauses** — definitions, obligations, exemptions, penalties, effective
  dates, scope — as structured segments.
- **Entities** — organisations, individuals, locations, dates, monetary
  thresholds, product categories, and cross-references to other
  regulations/standards — each linked to the clause it belongs to.
- Every clause and entity carries **source evidence** (page(s), verbatim
  quote) and a **review signal**, so downstream risk/compliance/workflow
  systems can trust — or know to distrust — what they're consuming.

Assumptions (from the brief, plus a few I'm adding):

| Area | Assumption |
|---|---|
| Volume | Thousands of documents per jurisdiction; continuous new/changed arrivals plus occasional bulk backfills. |
| Document profile | 5–300+ pages; mixed PDF/HTML; inconsistent structure, tables, footnotes, cross-references; some scanned pages. |
| Languages | English first; design must not preclude multilingual later. |
| Accuracy | Precision over latency. High-impact or low-confidence extractions route to human review before publication. |
| **Audit trail (added)** | This is a compliance product — an extraction that is later found wrong (or reprocessed with a better model) must not silently overwrite history. Every record needs versioning: which model, which prompt, when. |
| **Idempotency (added)** | The same document will be re-ingested (edited source, re-crawled, reprocessed after a prompt fix). Ingestion must dedupe/version by content hash, not just filename. |

## 2. End-to-end architecture

```
Source docs (PDF/HTML)
        │
        ▼
┌───────────────────┐
│ 1. Ingestion &     │  object storage, content-hash dedup,
│    normalization   │  event-driven trigger per new/changed doc
└─────────┬──────────┘
          ▼
┌───────────────────┐
│ 2. Parse / OCR     │  native text layer → layout-aware parser (headings,
│                    │  tables, reading order) → dedicated OCR/Document-AI
│                    │  for scanned pages → Claude vision as last-resort
│                    │  fallback for pages the primary OCR can't handle
└─────────┬──────────┘
          ▼
┌───────────────────┐
│ 3. Structural      │  heuristics (numbering, heading style) build the
│    segmentation    │  hierarchy first; LLM classifies ambiguous segments
│                    │  into clause types
└─────────┬──────────┘
          ▼
┌───────────────────┐
│ 4. Entity          │  LLM structured extraction per clause, entities
│    extraction      │  linked to clause_id
└─────────┬──────────┘
          ▼
┌───────────────────┐
│ 5. Grounding &     │  verbatim-span validation (hallucination guard) +
│    review triage   │  boolean rules → auto_publish / needs_review /
│                    │  rejected
└─────────┬──────────┘
          ▼
┌───────────────────┐        ┌─────────────────────┐
│ 6. Storage &       │──────▶│ Human review queue    │
│    versioned output│        │ (needs_review items)  │
└─────────┬──────────┘        └──────────┬───────────┘
          │                              │ decisions feed back
          ▼                              ▼
   Downstream risk / compliance /   Golden set / eval corpus
   workflow systems (API, events)
```

### 2.1 Ingestion & normalization
Documents land in object storage (S3/GCS) keyed by a content hash, with
metadata (jurisdiction, source, retrieved_at, doc type). A new or changed
document publishes an event onto a queue, which triggers the pipeline for
that document — this is what lets "thousands of documents, continuous
arrivals, occasional bulk backfill" scale horizontally without a bespoke
batch scheduler: backfills are just a burst of the same events.

### 2.2 Parsing / OCR — and why not "Claude as OCR" by default
This is the step the take-home specifically asked me to reason about.

**What I'd build for production:** native text-layer extraction first (free,
exact, instant) via a layout-aware parser (e.g. `pymupdf`/`pdfplumber`, or a
document-structure library like Docling/`unstructured.io`) that also gives
reading order and table detection. For pages with **no usable text layer**
(scans), route to a **dedicated OCR/Document-AI service** — AWS Textract,
Azure Document Intelligence, or Google Document AI, or an open-source
alternative (PaddleOCR, docTR, Tesseract) if cost or data-residency rules out
a managed API.

**Why not send every scanned page to a general-purpose multimodal LLM
instead:**
- **Cost/latency at scale.** Thousands of documents × up to 300 pages is a
  lot of image tokens; a purpose-built OCR engine is an order of magnitude
  cheaper and faster per page, and this system is explicitly precision-and-
  throughput sensitive, not latency-sensitive for any single document but
  very much cost-sensitive in aggregate.
- **Determinism & auditability.** A regulated compliance product benefits
  from an OCR step that returns the same output for the same input, with
  per-token confidence and pixel-accurate bounding boxes it can point a
  human reviewer at. LLM vision transcription is non-deterministic and
  gives, at best, an approximate location.
- **Failure mode shape.** Dedicated OCR fails by garbling characters
  (detectable via low per-token confidence). An LLM asked to "read this
  page" can instead *smooth over* a bad scan and produce fluent, plausible,
  wrong text — a much more dangerous failure mode for anything computing
  penalties or deadlines.

**Where Claude vision earns its place:** as a **fallback for the pages the
primary OCR engine flags low-confidence or fails on outright** — degraded
scans, handwritten annotations, unusual multi-column/rotated layouts, or
complex tables. It's genuinely good at those, and used narrowly there, the
cost is bounded to a small fraction of pages rather than the whole corpus.

**What the POC actually does:** a **hybrid** of the above, scoped to fit a
small proof-of-concept — native text layer first; Claude vision (not a
dedicated OCR API) as the fallback for pages without one. This demonstrates
the cost-awareness of the design (don't OCR what's already text) using only
the one AI provider needed for a self-contained, clone-and-run POC, while the
production recommendation above is what I'd actually operate at the stated
volume.

### 2.3 Structural segmentation
Deterministic heuristics (numbering patterns like "Article 5", "Section
3.2(a)", heading/font-size signals) build the document's hierarchy tree
first — this handles most well-formed regulations for free and gives stable
anchors even before any LLM call. The LLM is reserved for what heuristics
can't do reliably: classifying each segment into a clause type, and handling
documents whose structure doesn't follow a clean numbering convention.
*(The POC simplifies this to a single LLM call over the whole document —
see §6 and the POC README for the scope cut.)*

### 2.3a HTML documents
HTML skips stage 2 (parsing/OCR) entirely — it's already structured text, so
there's no scanning problem to solve — and plugs straight into structural
segmentation above, using the DOM itself (`<h1>`–`<h6>`, `<table>`, `<p>`,
`id`/`class` hints) as the heuristic segmentation signal instead of the PDF's
numbering/font-size heuristics. Everything downstream — entity extraction,
grounding, review triage — is format-agnostic once you have clause text and a
location to cite as evidence, so HTML is mostly a second **ingest adapter**,
not a second pipeline. `src/extractor/html_ingest.py` prototypes exactly
this — heading-delimited sections stand in for PDF page numbers as the
evidence anchor — and has been run end-to-end against a synthetic regulatory
HTML document (`docs/sample_regulation.html`,
`examples/sample_output_html.json`) to verify the extension point actually
holds, not just in theory.

### 2.4 Entity extraction
Structured (schema-constrained) LLM output per clause — or a windowed batch
of clauses for longer documents — extracting typed entities, each linked to
its `clause_id`. Deterministic normalization (date parsing, currency/amount
parsing, an org-name alias table) happens as a separate post-processing step,
not inside the LLM call — normalization is a solved, testable problem that
doesn't need a language model's judgment.

### 2.5 Grounding & review triage
Every clause/entity's quoted span is checked against the actual source text
it claims to come from. This is the single highest-leverage reliability
mechanism in the whole design: it converts "the model says so" into "the
model says so, *and* the text is verifiably present in the source," which
catches the failure mode that matters most for a compliance product —
plausible-sounding fabrication. See §3 for the full rule set.

### 2.6 Storage & downstream output
Versioned structured records (document → clauses → entities, each with
evidence + review flag), stored so that re-processing (better model, fixed
prompt) creates a new version rather than overwriting — full audit trail of
what was extracted, when, by which model/prompt version, matters for a
product whose job is to *prove* compliance.

### 2.7 Human review loop
`needs_review` items queue for human adjudication. Decisions aren't just a
publish/reject gate — they're the raw material for the golden set (§4) and,
over time, for prompt/model iteration and any future fine-tuning.

## 3. Evidence & review signal design

Every clause and entity carries:

```
text:      str                                     # what downstream consumes
evidence:  { page_start, page_end, quote }         # the citation backing it
grounded:  bool                                    # text AND quote verified in source
ocr_derived: bool                                  # any source page was OCR'd
review_flag: auto_publish | needs_review | rejected
review_reasons: [...]                              # which facts fired, and why
```

**There is deliberately no confidence score.** An earlier version blended
hand-picked penalties into a float and produced values like `0.7695` — a
number that reads as calibrated when nothing had been fitted, which in a
compliance product is worse than no number at all, because downstream trusts
the digits. Every decision is now a boolean fact about the extraction, and
`review_reasons` names the facts that fired. See "the cost of dropping the
score" below.

Scoring is deliberately **rule-based, not another model call** — the point
of this signal is that a compliance reviewer (or an auditor) can see exactly
why something was flagged:

- **Ungrounded span** (quote found nowhere in the document) → `rejected`.
  This is probable fabrication: don't publish it, and don't spend a
  reviewer's time on it either. The hallucination guard.
- **Page-range mismatch** (the text exists, but not on the pages cited) →
  `needs_review`. Also ungrounded, but reported separately: a broken citation
  and a fabricated span need very different triage, and only the former is
  salvageable by a human.
- **`text` not grounded** → the model returns both a `text` and an
  `evidence.quote`, and *downstream consumes `text`*. Validating only the
  quote lets a faithful citation sit beside a paraphrased or invented `text`,
  so both are checked.
- **Overbroad page range** → the model chooses its own citation range, and a
  wide one makes its own grounding check easier to pass. A claimed range
  materially wider than the span the quote actually occupies goes to review.
  One page of slack is tolerated so genuine page-spanning clauses aren't
  punished.
- **Quote too short to be independent evidence** → a span of a few characters
  ("EUR", "ESMA") matches almost any page. Short spans are acceptable *inside
  a verified parent clause*, which is a real constraint; outside that scope
  they cannot auto-publish.
- **Entities are grounded within their parent clause**, not the whole page —
  and only when that clause was itself verified, otherwise the check would be
  comparing model output against model output.
- **Fuzzy-matched span** (near-match rather than exact substring) → still
  auto-publishes. The tolerance exists precisely for whitespace and
  punctuation noise, and treating that as suspicious would clog the queue
  with non-problems.
- **OCR-derived source** → `needs_review`, since the check is only a
  copy-fidelity check on those pages. See the limitation below.
- **High-impact types** (`penalty`, `monetary_threshold`, `effective_date`)
  → never auto-publish, directly implementing the brief's "high-impact or
  low-confidence extractions may require human review."

**The cost of dropping the score.** Removing the float removes the ability to
express "probably fine" — every extraction is either clean or it isn't. The
consequence lands hardest on high-impact types: with no calibrated score,
there is no principled basis for waving a penalty, a monetary threshold or an
effective date through unreviewed, so **those never auto-publish**. On the
bundled HTML sample that moves the review queue from 1 item to 9 out of 26.

That is a real trade, and it is the right one at this stage. A blunt rule
that is honest about what it doesn't know beats a precise-looking number
nobody has validated — particularly where the failure mode is a downstream
system trusting a penalty amount it shouldn't. The route back to graded
decisions is the calibration work in §4: once a golden set exists,
bucket extractions, measure actual accuracy per bucket, and reintroduce a
score fitted to that evidence rather than to intuition. Until then the review
queue absorbs the uncertainty, which is exactly what it is for.

**What this check does not cover.** On native-text pages it is a genuine
independent check: the model's quote is compared against PyMuPDF's
extraction, which the model never saw. On **OCR'd pages it degrades to a
copy-fidelity check** — the page text came from Claude vision and the
extraction model quoted from that same transcription, so both sides trace
back to the model, and a mis-transcribed figure verifies as "grounded".
Closing that properly requires a second, independent OCR pass to
cross-check against (one more reason the production design in §2.2 puts a
dedicated OCR engine on the primary path); today OCR-derived extractions
are simply routed to review rather than trusted. Worth stating plainly rather
than letting "hallucination guard" imply more coverage than it has.

## 4. Evaluation & quality bar

- **Golden set**: a manually annotated sample spanning multiple
  jurisdictions/document types/formats (including at least a few scanned
  documents) — precision/recall/F1 per clause type and per entity type.
  This is the ground-truth metric and the regression gate before any
  prompt/model change ships.
- **Grounding rate**: % of extractions with a verified span match — a
  label-free proxy metric computable on *every* production batch, not just
  the golden set, so quality can be monitored continuously without new
  annotation.
- **Review agreement rate**: how often human reviewers accept vs. overturn
  `needs_review` items and how often they *catch* something that was
  `auto_publish` — the second number is the one that should worry you if it
  ever rises.
- **Reason-level precision**: for each `review_reason`, how often did it
  fire on an extraction a human then judged correct? A reason with a high
  false-alarm rate is costing reviewer time for nothing and should be
  loosened; one that never fires is dead weight. This is also the evidence
  needed to reintroduce a *fitted* confidence score in place of today's
  deliberately blunt flags (§3).

## 5. Production operability

- **Observability**: per-stage latency/cost/failure rate; drift in the
  review-flag and reason mix over time (a sudden shift often means a new
  document format broke segmentation, or the model changed underneath you); review-
  queue depth and age.
- **Reliability**: each pipeline stage is idempotent and retryable, with a
  dead-letter queue for documents that fail repeatedly — a bad PDF shouldn't
  stall the pipeline for everything behind it.
- **Versioning**: prompts, schemas, and extraction runs are all versioned;
  re-running a document with a new prompt version creates a new record, it
  doesn't silently mutate history.
- **Cost control**: cache parse/OCR output so only the LLM stages re-run
  when a prompt changes, not the whole pipeline; batch clause-level entity
  extraction calls where the document is large.
- **Rollout**: start on a single narrow jurisdiction/document type, shadow-
  run against a human baseline, compare grounding rate + golden-set metrics
  before trusting `auto_publish` for that segment, then expand jurisdiction
  by jurisdiction rather than flipping the whole system on at once.

## 6. What I'd build first vs. defer

**Build first** (= the POC's scope): digital-PDF parsing, an HTML adapter,
LLM clause segmentation + classification, grounded entity extraction, the
review-flag signal, structured JSON output.

**Defer**:
- Dedicated OCR/Document-AI integration for scanned pages at scale (the POC
  uses Claude vision as a stand-in fallback, per §2.2 — good enough to prove
  the pattern, not the production choice).
- Heuristic pre-segmentation ahead of the LLM call (POC does single-pass LLM
  segmentation given the document's size).
- Production-grade HTML handling — the POC's adapter (§2.3a) proves the
  extension point on well-formed markup; real-world sanitization,
  script-rendered pages, and malformed HTML are out of scope. Multilingual
  support is likewise deferred.
- Human review UI (the `needs_review` signal exists; the queue/UI to act on
  it doesn't, in this POC).
- Cross-document reference resolution (an entity of type
  `regulation_reference` is extracted as text, not resolved to an actual
  document in the corpus).
- Active learning / fine-tuning from review decisions.

## 7. Risks & trade-offs

| Risk | Mitigation |
|---|---|
| LLM hallucinates a clause/entity or its span | Grounding check (§2.5, §3) — the core defense. |
| Heuristic segmentation breaks on an unfamiliar jurisdiction's formatting | Golden set must include format diversity; LLM fallback for ambiguous structure; monitor grounding rate by jurisdiction to catch silent degradation early. |
| Poor scan quality → bad OCR → bad extraction | Dedicated OCR's per-token confidence feeds directly into the `ocr_derived` signal; very low-confidence OCR pages could route straight to human transcription rather than through the LLM at all. |
| Cost/latency at 300+-page documents, thousands of docs | Native-text-first, OCR only where needed, clause-level (not whole-document) LLM calls, caching of parse/OCR output across re-runs. |
| Compliance product needs an audit trail | Versioned records by design (§2.6), not bolted on later. |

## 8. POC mapping

The POC (`src/extractor/`) implements ingestion → segmentation → entity
extraction → grounding → review triage exactly as described in §2.2–2.5, using
Claude for both the vision-OCR fallback and the structured clause/entity
extraction. It's the "focused, high-risk part" of this design made concrete:
grounded extraction with evidence and a review signal is the piece most
likely to fail silently in production if you don't build the verification
step in from the start.

Ingestion has two adapters sharing everything downstream: `ingest.py` (PDF,
native text + Claude-vision fallback) and `html_ingest.py` (§2.3a). Each was
run end-to-end against a real document — `docs/sample_doc.pdf` and
`docs/sample_regulation.html` — with verified output checked in at
`examples/sample_output.json` and `examples/sample_output_html.json`.

See the repo `README.md` for how to run it and where the output lands.
