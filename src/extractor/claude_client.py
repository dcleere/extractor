"""Thin wrapper around the Anthropic SDK for the two calls this POC needs:
vision transcription (OCR fallback) and schema-constrained structured
extraction.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic

DEFAULT_MODEL = "claude-sonnet-5"
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


# unsupported JSON Schema Pydantic keywords that the SDK does not support
_UNSUPPORTED_SCHEMA_KEYWORDS = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
    "default",
}


def _sanitize_schema(schema: Any) -> Any:
    """Adapt a pydantic-generated JSON Schema to the subset Anthropic's
    structured-output mode accepts: every object needs `additionalProperties:
    false` explicit, and constraint keywords like minimum/maximum aren't
    supported."""
    if isinstance(schema, dict):
        for key in list(schema.keys()):
            if key in _UNSUPPORTED_SCHEMA_KEYWORDS:
                del schema[key]
        if schema.get("type") == "object" or "properties" in schema:
            schema.setdefault("additionalProperties", False)
        for value in schema.values():
            _sanitize_schema(value)
    elif isinstance(schema, list):
        for item in schema:
            _sanitize_schema(item)
    return schema


class ClaudeClient:
    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def _with_retries(self, fn):
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return fn()
            except (anthropic.RateLimitError, anthropic.APIConnectionError) as exc:
                last_error = exc
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise RuntimeError(f"Claude call failed after {MAX_RETRIES} attempts") from last_error

    def transcribe_image(self, image_b64: str, system: str) -> str:
        def call():
            return self._client.messages.create(
                model=self.model,
                max_tokens=4096,
                thinking={"type": "disabled"},
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": image_b64,
                                },
                            },
                            {"type": "text", "text": "Transcribe this page."},
                        ],
                    }
                ],
            )

        response = self._with_retries(call)
        text_blocks = [b.text for b in response.content if b.type == "text"]
        return "\n".join(text_blocks).strip()

    def structured_extract(
        self, *, system: str, user_prompt: str, json_schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Call Claude with a native JSON-schema-constrained output and return
        the parsed dict. Raises if the response isn't valid JSON, so callers
        see a hard failure rather than silently proceeding on garbage."""

        schema = _sanitize_schema(json.loads(json.dumps(json_schema)))

        def call():
            return self._client.messages.create(
                model=self.model,
                max_tokens=16000, # raise error if the document is too long for Claude to handle
                thinking={"type": "disabled"},
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
                output_config={
                    "format": {"type": "json_schema", "schema": schema},
                },
            )

        response = self._with_retries(call)
        text_blocks = [b.text for b in response.content if b.type == "text"]
        if not text_blocks:
            raise RuntimeError("Claude returned no text content for structured extraction")
        return json.loads("\n".join(text_blocks))
