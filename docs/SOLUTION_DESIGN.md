# Solution Design — Regulatory Clause & Entity Extraction

## 1. Problem & assumptions

Ingest regulatory documents (regulations, directives, standards, enforcement
notices) and reliably produce, for each document:

- **Clauses** — definitions, obligations, exemptions, penalties, effective
  dates, scope — as structured segments.
- **Entities** — organisations, individuals, locations, dates, monetary
  thresholds, product categories, and cross-references to other
  regulations/standards — each linked to the clause it belongs to.
- Every clause and entity carries **source evidence** — a verbatim quote and
  a locator into the source — and a **review signal** derived from verifying
  that evidence, so downstream risk/compliance/workflow systems can check
  what they're consuming rather than take it on trust.

Assumptions:

| Area | Assumption |
|---|---|
| Volume | Thousands of documents per jurisdiction; continuous new/changed arrivals plus occasional bulk backfills. |
| Document profile | 5–300+ pages; mixed PDF/HTML; inconsistent structure, tables, footnotes, cross-references; some scanned pages. |
| Languages | English first. Component choices must not preclude non-Latin scripts later, but multilingual is out of scope for v1 (§6). |
| Accuracy | Precision over latency. Nothing publishes unless its evidence verifies against the source; high-impact extractions (penalties, monetary thresholds, effective dates) go to human review regardless. |
| **Observability (added)** | This is a compliance product. Therefore, an extraction that is later found wrong (or reprocessed with a better model) must not silently overwrite history. Every record needs versioning: which model, which prompt, when. |
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
│                    │  for scanned pages → frontier model vision as last-resort
│                    │  fallback for pages the primary OCR can't handle
└─────────┬──────────┘
          ▼
┌───────────────────┐
│ 3. Entity          │  LLM structured extraction per clause, entities
│    extraction      │  linked to clause_id
└─────────┬──────────┘
          ▼
┌───────────────────┐
│ 4. Grounding &     │  verbatim-span validation (hallucination guard) +
│    review triage   │  decision table → auto_publish / needs_review /
│                    │  rejected
└─────────┬──────────┘
          ▼
┌───────────────────┐        ┌─────────────────────┐
│ 5. Storage &       │──────▶│ Human review queue    │
│    versioned output│        │ (needs_review items)  │
└─────────┬──────────┘        └──────────┬───────────┘
          │                              │ decisions feed back
          ▼                              ▼
   Downstream risk / compliance /   Golden set / eval corpus
   workflow systems (API, events)
```

### 2.0 Runtime shape

This is an **event-driven, serverless
pipeline with one state machine per document** — it fits the
workload: bursty arrivals, per-document isolation and long-running asynchronous
OCR.

| Concern | Choice |
|---|---|
| Artifacts | Object store, content-hash keyed (S3 / Blob Storage / GCS). |
| Trigger | Object event → queue → one workflow execution per document version. |
| Orchestration | Durable state machine (Step Functions / Logic Apps / Workflows). Stages are stateless functions; the machine holds state, retries and the DLQ. |
| Document state | Key-value store (DynamoDB / Cosmos / Firestore) tracking status, current stage, config version, timings, failure reason. Answers *"where is document X?"*, drives the review queue, makes re-ingest idempotent. Distinct from §2.6's published output store. | 


**Asynchronous.** Multi-page OCR is a submit/callback API at
every major provider, not a function call. The state machine waits on a job
token rather than blocking a worker — which is why this is a state machine
and not a chain of queue consumers.

**The binding constraint is provider tokens-per-minute, not compute.**
Serverless compute scales past the model quota trivially, so the limiter must
be token-aware: concurrency derived from the token budget, backpressure via
queue depth, retry with jitter on 429, DLQ for repeat failures. Sizing the
infrastructure without sizing the quota is the standard way this class of
pipeline falls over.

**Two lanes, same stages.** New arrivals take the asynchronous lane (minutes).
Backfills and reprocessing after a prompt change take a **batch-inference
lane** (Anthropic Message Batches / Bedrock batch / equivalent): roughly half
the token price for ~24h turnaround.

### 2.1 Ingestion & normalization
Documents land in object storage keyed by a content hash, with metadata
(jurisdiction, source, retrieved_at, doc type). A new or changed document
publishes an event onto a queue, triggering a workflow execution for that
document version. Re-ingesting an unchanged document is a no-op by hash; a
changed one creates a new version rather than mutating the old.

### 2.2 Parsing / OCR

**What I'd build for production:** native text-layer extraction first (free,
exact, instant) via a layout-aware parser (e.g. `pymupdf`) that also gives
reading order and table detection. For pages with **no usable text layer**
(scans), route to a **dedicated OCR/Document-AI service** (e.g. `AWS Textract`
/ `Bedrock Data Automation`, `Azure Document Intelligence`, or
`Google Document AI`).

**Output contract of this stage:** text, plus — for anything *recognised*
rather than read from an existing text layer — per-word/per-region
recognition confidence **and bounding boxes**. Every mainstream Document-AI
service exposes both. These aren't incidental extras: §2.5a scopes review
triage to the region behind each citation, §2.7 puts the box in front of the
reviewer, and §3 carries both on the evidence record — so a parse backend
that can't supply them forces the conservative default (route to review) for
everything it touches.

**The arithmetic behind the choice.** Take 5,000 documents × ~40 pages ≈ 200k
pages, ~85% arriving with a usable text layer. Indicative list prices, worth
re-checking before committing:

| Path | Pages | Indicative cost |
|---|---|---|
| Native text layer | 170k | ~$0 |
| Dedicated OCR on the residual scans | 30k | ~$45 plain text; ~$450 with table/form analysis |
| Frontier vision on the same residual | 30k | ~$500 (≈2k image tokens + ~700 output per page) |
| Frontier vision on **every** page | 200k | ~$3,300 |

Two conclusions, pointing different ways. **Native-text-first is the large
win** — it removes ~85% of the recognition bill outright, and is exact and
instant besides. But on the residual scans the engine choice is a few hundred
dollars either way, so it is *not* won on cost:

- **Determinism & auditability.** A regulated product benefits from a step
  that returns the same output for the same input, with per-token confidence
  and pixel-accurate bounding boxes it can point a reviewer at. LLM vision is
  non-deterministic and gives, at best, approximate location and directional
  confidence.
- **Failure mode shape.** Dedicated OCR fails by garbling characters —
  detectable via low per-token confidence. An LLM asked to "read this page"
  can instead *smooth over* a bad scan and produce fluent, plausible, wrong
  text. That is the far more dangerous failure for anything computing
  penalties or deadlines.

**Where frontier vision earns its place:** as a **fallback for pages the
primary engine flags low-confidence or fails outright** — degraded scans,
handwritten annotations, rotated or multi-column layouts, complex tables.
It is good at those (to be verified against the golden set), and used
narrowly there the cost is immaterial.

### 2.3 Entity extraction
Structured (schema-constrained) LLM output per clause extracting typed
entities, each linked to its `clause_id`. Deterministic normalization (date
parsing, currency/amount parsing, an org-name alias table) happens as a
separate post-processing step, not inside the LLM call — normalization is a
solved, testable problem that doesn't need a language model's judgment.

### 2.5 Grounding & review triage
Every clause/entity's quoted span is checked against the actual source text
it claims to come from. This is the single highest-leverage reliability
mechanism in the whole design and captures
plausible-sounding fabrication. See §3 for the full rule set.

Its limit is worth stating plainly: grounding verifies **presence, not
correctness**. A correctly quoted span attached to the wrong clause type — a
real sentence extracted as an obligation when it is an exemption — passes
every check here. That class of error is caught by the golden set (§4), not
by grounding, which is why §4 is not optional.

### 2.5a OCR-engine confidence as a review-triage signal
A dedicated OCR/Document-AI engine (§2.2) returns a **per-word or per-line
confidence score** for its own character recognition (e.g. Azure Document
Intelligence's per-word `confidence`). This is a materially different signal
from a model's self-rated confidence in the PoC: it comes from a model purpose-built to
estimate *recognition* uncertainty — ambiguous glyph, low contrast, skew,
noise.

**Keep it evidence-scoped, not page-scoped.** A blanket "this page was OCR'd"
flag is a rough proxy. Engine confidence is available per word/region, so it
attaches to the span backing a citation, not the page. Two clauses on the same
imperfectly scanned page carry very different real risk — one quoting a crisp
paragraph, one a smudged table cell. This is also why the evidence record
stays in the loop rather than being replaced by a score: the score is only
actionable if a reviewer can see *which* text it rates.

**Consume it through a threshold.** "Closer to validated" still isn't
"validated here," so it earns a threshold, not a number that travels
downstream; §4 calibrates where the threshold sits. Its role in the rule set —
discriminating fabrication from mis-recognition, and thereby earning the right
to `reject` — is set out in §3.

**On logprobs.** Not a substitute. Token logprob reflects how *expected* a
token was given the image and preceding text, not recognition accuracy: a
model can be highly confident in a fluent, plausible, wrong continuation. It
is also the wrong granularity — per-subword-token, where OCR evidence needs
word- or line-level uncertainty to be useful to a reviewer.

### 2.6 Storage & downstream output
Versioned structured records (document → clauses → entities, each with
evidence + review flag), stored so that re-processing (better model, fixed
prompt) creates a new version rather than overwriting — full audit trail of
what was extracted, when, by which model and config version. That matters for
a product whose job is to *prove* compliance.

### 2.7 Human review loop

`needs_review` items queue for adjudication. Three things make this a
component rather than a backlog:

- **What the reviewer sees.** The evidence quote, the clause around it, the
  reasons that fired, and — for recognised sources — the page image cropped to
  the bounding box (§2.2, §3). Judging an extraction should not require
  opening the source PDF. This is what the bounding boxes are collected *for*.
- **Review rate.** If 40% of extractions need
  review the system isn't economically viable, so review rate is budgeted and
  tracked. §4's reason-level precision is the instrument for tuning it: a
  reason with a high false-alarm rate spends reviewer time for nothing. A
  rising review rate is also a degradation alarm (§5.1).
- **Decisions write a new version, never an overwrite** (§2.6), and become
  raw material for the golden set (§4) and future prompt/model iteration.

## 3. Evidence & review signal design

Every clause and entity carries:

```
text:      str                                     # what downstream consumes
evidence:  {                                       # the citation backing it
  locator:  { page_start, page_end } | anchor,     #   format-specific position
  bbox:     [x, y, w, h] | null,                   #   region on the page, where known
  quote:    str,                                   #   the verbatim span
  recognition_confidence: float | null             #   per §2.5a, null where N/A
}
grounded:  bool                                    # text AND quote verified in source
source_kind: native_text | recognised | markup     # how the source text was obtained
review_flag: auto_publish | needs_review | rejected
review_reasons: [...]                              # which facts fired, and why
```

`source_kind` replaces a page-level "was this OCR'd" boolean: it records how
the text a citation rests on was obtained, which determines whether grounding
is an independent check or merely copy-fidelity (§2.5). `bbox` and
`recognition_confidence` are evidence-scoped, not page-scoped — the first
sends a reviewer straight to the pixels, the second conditions the rules below.

**A worked record**, a penalty clause from the HTML sample run:

```jsonc
{
  "id": "c9",
  "type": "penalty",
  "heading": "5. Penalties",
  "text": "A Competent Authority may impose an administrative fine of up to
           €500,000 or 2% of the undertaking's total annual turnover, ...",
  "evidence": {
    "locator": { "anchor": "5. Penalties" },
    "quote":   "A Competent Authority may impose an administrative fine of
                up to €500,000 ...",
    "recognition_confidence": null
  },
  "source_kind": "markup",
  "grounded": true,
  "review_flag": "needs_review",
  "review_reasons": ["high_impact_type"]
}
```

The quote verifies against the source, so `grounded` is true and no
source-dependent reason fires. It still doesn't publish: `penalty` is a
high-impact type, and the single entry in `review_reasons` tells the reviewer
exactly why it reached their queue. That is the whole contract — a verdict a
human can interrogate, rather than a number they have to trust.

**Triage is a decision table, not a score.** No number reaches the output.
Each rule is a fact about the extraction and `review_reasons` names the facts
that fired, because a reviewer or auditor must be able to see *why* something
was held back — a float can't be interrogated, and one assembled from
unvalidated constants is worse than none, since downstream trusts the digits.
`recognition_confidence` is the pipeline's only number, admissible because it
comes from a purpose-built recogniser and is consumed solely through a
threshold (§4 sets it), never blended.

**Source-dependent rules** ask *"is this text present in the source?"*, so
their answer is only as good as the source. Recognition confidence conditions
them: on a recognised source a missing quote is ambiguous between fabrication
and mis-recognition (§2.5a).

| `review_reason` | Condition | Verdict |
|---|---|---|
| `ungrounded_span` | quote found nowhere; source `native_text`/`markup`, or `recognised` above threshold | `rejected` |
| `ungrounded_span` | quote found nowhere; source `recognised` below threshold or unavailable | `needs_review` |
| `locator_mismatch` | quote exists, but not at the cited locator | `needs_review` |
| `text_not_grounded` | quote verifies, `text` does not | `needs_review` |
| `entity_outside_parent_clause` | entity absent from the clause it claims | `needs_review` |
| `fuzzy_match` | near-match; source `native_text`/`markup`, or `recognised` not below threshold | `auto_publish` |
| `low_recognition_confidence` | near-match on a `recognised` region below threshold | `needs_review` |
| `unverifiable_source` | `source_kind` is `recognised`, no confidence signal available | `needs_review` |


**Source-independent rules** inspect the extraction alone:

- `quote_too_short` — a few characters ("EUR", "ESMA") match almost any page.
  Acceptable only inside an already-verified parent clause.
- `overbroad_locator` — the model picks its own citation range, and a wide
  one makes its own check easier to pass. One unit of slack is tolerated so
  genuine span-crossing clauses aren't punished.
- `orphan_clause_id` — an entity citing a clause that doesn't exist.
- `high_impact_type` — `penalty`, `monetary_threshold`, `effective_date`
  never auto-publish, implementing the brief's requirement that high-impact
  extractions get human review.

**Precedence:** most conservative verdict wins, with one deliberate
asymmetry — `rejected` is reachable only where the source was established as
trustworthy, so a low-confidence region never rejects however many rules
fire on it.

## 4. Evaluation & quality bar

Evaluation is a **runnable artifact in the repo, not a document**: a CLI that
replays a pinned corpus against a pinned config version (§5.2) and writes
results keyed by that version, so any two runs are diffable. It runs in CI on
every prompt, schema or model change and gates release (§5.2).

- **Golden set**: a manually annotated sample spanning multiple
  jurisdictions, document types and formats (including scanned documents) —
  precision/recall/F1 **sliced per clause type, entity type, document type
  and jurisdiction**. 
- **Adversarial negatives**: the design rests on the grounding check, so the
  check itself is tested. The corpus carries deliberately corrupted
  extractions — fabricated spans, quotes moved to the wrong locator, entities
  reassigned to the wrong clause, characters transposed as a recogniser would
  — and the gate asserts each is caught with the expected reason. Without
  this, a normalization bug that made everything match would leave every other
  metric here looking healthy.
- **Review agreement rate**: how often reviewers accept vs. overturn
  `needs_review` items, and how often they *catch* something that was
  `auto_publish`. The second number is the one that should worry you if it
  ever rises.
- **Cost and latency per document**, gated alongside quality and sliced by
  document type. A prompt change that lifts F1 a point and triples token spend
  is a regression.

## 5. Delivery plan

Each milestone is defined by the evidence that closes it, not the code in it.

| Milestone | Scope | Closed by |
|---|---|---|
| **M0 — thin slice** | One jurisdiction, one format (digital PDF); LLM segmentation, grounded extraction, decision-table triage, JSON output — **plus the eval harness and golden set from day one**. | Golden-set F1 baseline established; adversarial negatives all caught; grounding rate measured. |
| **M1 — recognition** | Dedicated Document-AI for scanned pages; `source_kind`, `recognition_confidence` and `bbox` populated end to end; frontier vision as narrow fallback. | Threshold calibrated from reason-level precision; scanned documents in the golden set meeting the same bar. |
| **M2 — the loop** | Review queue and UI, decision write-back, feedback into the golden set; auto-publish flag plus shadow/canary machinery. | Review agreement rate measured; one segment promoted to `auto_publish` on evidence. |
| **M3 — scale** | Batch backfill lane, multi-jurisdiction rollout, HTML adapter hardened for real-world markup. | Full corpus backfilled within cost budget; per-jurisdiction metrics held. |

**Deferred beyond M3**: multilingual support; cross-document reference
resolution (a `regulation_reference` entity is extracted as text, not resolved
to a document in the corpus); active learning or fine-tuning from review
decisions; script-rendered HTML.

The order is deliberate. The eval harness sits in M0 rather than later because
every subsequent milestone is gated on it — a system that cannot measure
itself cannot be safely changed.

## 7. Risks & trade-offs

| Risk | Mitigation |
|---|---|
| LLM hallucinates a clause/entity or its span | Grounding check (§2.5, §3) — the core defense. |
| **Grounding verifies presence, not correctness** — a real quote misclassified as the wrong clause type passes every automated check | The deepest limitation in the design. |
| Poor scan quality → bad recognition → bad extraction | Per-region `recognition_confidence` conditions triage (§3); very low-confidence pages route to human transcription rather than through the LLM at all. |
| **Model deprecated or silently changed underneath you** | Config-pinned model version, regression gate before any change ships (§5.2), eval results retained per version so drift is provable rather than suspected. |
| **Review capacity saturates** — quality gates are worthless if the queue is unworkable | Review rate budgeted as an SLO (§2.7), tuned via reason-level precision (§4); a reason that cannot be made precise is removed rather than tolerated. |
| **Upstream source changes format or blocks the crawler** | Ingestion failures alarm separately from processing failures; content-hash versioning makes a silent format change visible as a mass re-extraction. |
| Cost/latency at 300+-page documents, thousands of docs | Native-text-first (§2.2), fan-out per page/clause, batch lane for backfill, caching of parse output across re-runs. |
| Residency or provider lock-in | Stage boundaries are provider-agnostic contracts (§2.2's output contract especially), so the recognition and LLM stages can be swapped per region (§5.3). |
| Compliance product needs an audit trail | Versioned records by design (§2.6), not bolted on later. |


## 9. References

Claude helped with this design. Here are references I used to help with this design also.
- https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws
- https://hypersense-software.com/blog/2025/04/02/intelligent-document-processing-aws-workflow-automation/
