from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from .claude_client import DEFAULT_MODEL, ClaudeClient
from .pipeline import run

DEFAULT_SAMPLE_PATH = Path("docs/sample_doc.pdf")
OUTPUT_DIR = Path("output")


def _print_summary(result) -> None:
    clause_types = Counter(c.type for c in result.clauses)
    entity_types = Counter(e.type for e in result.entities)
    review_flags = Counter(c.review_flag for c in result.clauses) + Counter(
        e.review_flag for e in result.entities
    )

    print(f"\nDocument: {result.document.filename} ({result.document.page_count} pages)")
    print(f"Model: {result.document.model}  Prompt: {result.document.prompt_version}\n")

    print(f"Clauses ({len(result.clauses)}):")
    for clause_type, count in clause_types.most_common():
        print(f"  {clause_type:<16} {count}")

    print(f"\nEntities ({len(result.entities)}):")
    for entity_type, count in entity_types.most_common():
        print(f"  {entity_type:<20} {count}")

    print("\nReview signal (clauses + entities combined):")
    for flag, count in review_flags.most_common():
        print(f"  {flag:<14} {count}")


def main() -> None:
    load_dotenv()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your "
            "key, or export it in your shell.",
            file=sys.stderr,
        )
        sys.exit(1)

    doc_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SAMPLE_PATH
    model = os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
    client = ClaudeClient(api_key=api_key, model=model)

    result = run(doc_path, client)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_path = OUTPUT_DIR / f"{doc_path.stem}.extraction.json"
    out_path.write_text(result.model_dump_json(indent=2))

    _print_summary(result)
    print(f"\nFull structured output written to {out_path}")


if __name__ == "__main__":
    main()
