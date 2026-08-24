"""Clause segmentation + entity extraction in a single structured-output call.

For a document this size a single call is simplest and keeps clause/entity
linking trivial. At production scale this stage would first run cheap
heuristic segmentation (numbering patterns, heading styles) and only call the
LLM per-segment for classification + entity extraction — see
docs/SOLUTION_DESIGN.md. Keeping that heuristic pass out of the POC is a
deliberate scope cut, not an oversight.
"""

from __future__ import annotations

from .claude_client import ClaudeClient
from .ingest import IngestedDocument
from .schema import ModelExtraction

PROMPT_VERSION = "v2"

# Prompt is informed by the assessment spec
SYSTEM_PROMPT = """\
You are a regulatory analyst extracting structured clauses and legally \
significant entities from a regulation, directive, standard, or enforcement \
notice.

Rules:
1. Segment the document into clauses. Each clause gets one of these types: \
definition, obligation, exemption, penalty, effective_date, scope, other.
2. For each clause, `text` MUST be an exact verbatim quote copied character- \
for-character from the source pages — do not paraphrase, summarize, \
translate, or fix typos. Record the page number(s) the quote appears on in \
`evidence.page_start` / `evidence.page_end`, using the [PAGE n] markers in \
the source.
3. Extract legally significant entities referenced within each clause: \
organisation, person, location, date, monetary_threshold, product_category, \
regulation_reference (a reference to another regulation/standard/article). \
Each entity's `text` MUST also be an exact verbatim quote, with its own \
evidence page range, and `clause_id` set to the id of the clause it belongs \
to.
4. Assign every clause and entity an ID you invent (short strings like \
"c1", "e1" are fine).
5. Do not invent clauses or entities that are not supported by the text. \
Skip boilerplate (page headers/footers, tables of contents) unless it is \
itself a definition, obligation, exemption, penalty, or effective date.
6. Leave `normalized_value` null unless you can confidently normalize a date \
(ISO 8601) or a monetary amount (e.g. "50000 EUR").

Do not populate ocr_derived, grounded, review_flag, or review_reasons — \
leave them at their default values; a downstream process computes those.\
"""


def _user_prompt(document_text: str) -> str:
    return (
        "Extract clauses and entities from the following regulatory document. "
        "Page boundaries are marked with [PAGE n].\n\n" + document_text
    )


def extract(document: IngestedDocument, client: ClaudeClient) -> ModelExtraction:
    schema = ModelExtraction.model_json_schema()
    raw = client.structured_extract(
        system=SYSTEM_PROMPT,
        user_prompt=_user_prompt(document.as_prompt_text()),
        json_schema=schema,
    )
    return ModelExtraction.model_validate(raw)
