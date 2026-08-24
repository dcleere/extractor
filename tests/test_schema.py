"""The checked-in example outputs should always validate against the current
schema -- this is what would catch a schema change silently breaking them."""

from __future__ import annotations

import json

import pytest

from extractor.schema import ExtractionResult
from helpers import REPO_ROOT

EXAMPLES = [
    REPO_ROOT / "examples" / "sample_output.json",
    REPO_ROOT / "examples" / "sample_output_html.json",
]


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_checked_in_example_validates_against_current_schema(path):
    data = json.loads(path.read_text())
    result = ExtractionResult.model_validate(data)

    assert result.clauses
    assert result.entities

    clause_ids = {c.id for c in result.clauses}
    assert all(e.clause_id in clause_ids for e in result.entities)
