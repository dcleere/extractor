"""Shared test fixtures: sample-document paths and stub Claude clients.

These stubs implement the same surface as ClaudeClient (transcribe_image,
structured_extract, model) but never touch the network — every test in this
suite runs with no API key and costs nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_PDF = REPO_ROOT / "docs" / "sample_doc.pdf"
SAMPLE_HTML = REPO_ROOT / "docs" / "sample_regulation.html"


class PoisonPillClient:
    """Fails loudly if anything tries to call the real API."""

    model = "poison-pill"

    def transcribe_image(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("transcribe_image was called unexpectedly")

    def structured_extract(self, *args: Any, **kwargs: Any) -> dict:
        raise AssertionError("structured_extract was called unexpectedly")


class StubClaudeClient:
    """A ClaudeClient stand-in returning canned, caller-supplied responses."""

    model = "stub-model"

    def __init__(
        self,
        *,
        transcription: str = "",
        extraction: dict[str, Any] | None = None,
    ) -> None:
        self._transcription = transcription
        self._extraction = extraction if extraction is not None else {"clauses": [], "entities": []}

    def transcribe_image(self, image_b64: str, system: str) -> str:
        return self._transcription

    def structured_extract(
        self, *, system: str, user_prompt: str, json_schema: dict
    ) -> dict:
        return self._extraction
