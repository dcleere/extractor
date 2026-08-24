"""The JSON-schema sanitizer is pure dict manipulation -- no API involved."""

from __future__ import annotations

from typing import Any, Iterator

from extractor.claude_client import _sanitize_schema
from extractor.schema import ModelExtraction


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_sanitize_schema_strips_unsupported_keywords_and_forbids_extras():
    raw = ModelExtraction.model_json_schema()
    cleaned = _sanitize_schema(raw)

    forbidden = {"minimum", "maximum", "default"}
    for node in _walk(cleaned):
        assert forbidden.isdisjoint(node.keys())
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False
