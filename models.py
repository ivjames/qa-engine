"""qa-engine's single Anthropic integration point.

Wraps Haiku (triage), Sonnet (deep review + vision), and Opus (rare
escalation) with prompt caching on rubric system prompts and a Batch API
seam. Fully usable with NO api key via mock mode (config.MOCK_MODELS), and
the mock path runs the exact same request-building code (build_request) so
tests that assert on request shape exercise real code, not a mock stand-in.

Per-model-family request shape (see the claude-api skill):
  - Haiku 4.5 (an older, pre-4.6 model): thinking is either omitted or
    {"type": "enabled"/"disabled"}; no `effort` support.
  - Sonnet 5 / Opus 5 (current 5-series): depth is controlled via
    output_config.effort ("low"/"medium"/"high"/"xhigh"/"max"). The
    deprecated/removed `budget_tokens` field is never used by this module.
  - Structured JSON output (either family): output_config.format =
    {"type": "json_schema", "schema": <schema>}.
  - Prompt caching: the FIRST system block gets
    cache_control={"type": "ephemeral"} (rubric prefix caching).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import config

try:  # pragma: no cover - exercised implicitly; anthropic is an optional
    import anthropic  # runtime import for the real (non-mock) path
except ImportError:  # pragma: no cover
    anthropic = None


@dataclass
class ModelResult:
    text: str
    parsed: object | None
    usage: dict
    custom_id: str | None = None


def _is_haiku(model: str) -> bool:
    return "haiku" in model


class Models:
    def __init__(self, mock: bool | None = None):
        # mock defaults to config.MOCK_MODELS (already True when no key set).
        self.mock = config.MOCK_MODELS if mock is None else mock
        self.client = None
        if not self.mock:
            if anthropic is None:  # pragma: no cover - defensive
                raise RuntimeError(
                    "anthropic package is required for non-mock Models(); "
                    "install it or run with MOCK_MODELS=1"
                )
            # Reads ANTHROPIC_API_KEY from env.
            self.client = anthropic.Anthropic()
        # mock-mode in-memory batch store: batch_id -> {custom_id: ModelResult}
        self._batches: dict[str, dict[str, ModelResult]] = {}

    # ---- pure request building -------------------------------------------------

    @staticmethod
    def _normalize_system(system_blocks) -> list[dict]:
        """Accept a plain string or a list of {"type":"text","text":...} blocks.
        Always return a list, with cache_control on exactly the FIRST block."""
        if isinstance(system_blocks, str):
            system_blocks = [{"type": "text", "text": system_blocks}]
        out = []
        for i, block in enumerate(system_blocks):
            b = dict(block)
            if i == 0:
                b["cache_control"] = {"type": "ephemeral"}
            out.append(b)
        return out

    def build_request(
        self,
        *,
        model,
        system_blocks,
        user_content,
        max_tokens,
        effort=None,
        thinking=None,
        json_schema=None,
    ) -> dict:
        """Pure: assemble the kwargs dict for messages.create / a batch request.
        Never calls the API. Deterministic given its inputs."""
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": self._normalize_system(system_blocks),
            "messages": [{"role": "user", "content": user_content}],
        }

        # thinking: Haiku 4.5 takes an explicit enabled/disabled toggle; the
        # 5-series models never see budget_tokens from this codepath.
        if thinking == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        elif thinking == "enabled":
            kwargs["thinking"] = {"type": "enabled"}
        elif isinstance(thinking, dict):
            kwargs["thinking"] = thinking
        elif thinking is not None:
            kwargs["thinking"] = {"type": thinking}

        output_config: dict[str, Any] = {}
        if effort is not None:
            output_config["effort"] = effort
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        if output_config:
            kwargs["output_config"] = output_config

        return kwargs

    # ---- single-call path -------------------------------------------------------

    def call(
        self,
        *,
        model,
        system_blocks,
        user_content,
        max_tokens,
        effort=None,
        thinking=None,
        json_schema=None,
    ) -> ModelResult:
        kwargs = self.build_request(
            model=model,
            system_blocks=system_blocks,
            user_content=user_content,
            max_tokens=max_tokens,
            effort=effort,
            thinking=thinking,
            json_schema=json_schema,
        )
        if self.mock:
            return self._mock_result(json_schema=json_schema)
        response = self.client.messages.create(**kwargs)
        return self._map_response(response)

    # ---- batch path --------------------------------------------------------------

    def submit_batch(self, units: list[dict]) -> str:
        """units: [{"custom_id": str, "request": <kwargs from build_request>}]"""
        if self.mock:
            batch_id = f"mock-batch-{uuid.uuid4().hex[:12]}"
            results: dict[str, ModelResult] = {}
            for unit in units:
                request = unit["request"]
                schema = request.get("output_config", {}).get("format", {}).get("schema")
                result = self._mock_result(json_schema=schema)
                result.custom_id = unit["custom_id"]
                results[unit["custom_id"]] = result
            self._batches[batch_id] = results
            return batch_id

        requests = [
            {"custom_id": unit["custom_id"], "params": unit["request"]} for unit in units
        ]
        batch = self.client.messages.batches.create(requests=requests)
        return batch.id

    def poll_batch(self, batch_id) -> dict:
        if self.mock:
            results = self._batches.get(batch_id, {})
            return {
                "status": "ended",
                "counts": {
                    "processing": 0,
                    "succeeded": len(results),
                    "errored": 0,
                },
            }
        batch = self.client.messages.batches.retrieve(batch_id)
        status = "ended" if batch.processing_status == "ended" else "in_progress"
        counts_obj = batch.request_counts
        counts = {
            "processing": getattr(counts_obj, "processing", 0),
            "succeeded": getattr(counts_obj, "succeeded", 0),
            "errored": getattr(counts_obj, "errored", 0),
        }
        return {"status": status, "counts": counts}

    def batch_results(self, batch_id) -> dict[str, ModelResult]:
        if self.mock:
            return dict(self._batches.get(batch_id, {}))

        results: dict[str, ModelResult] = {}
        for item in self.client.messages.batches.results(batch_id):
            if item.result.type == "succeeded":
                mapped = self._map_response(item.result.message)
                mapped.custom_id = item.custom_id
                results[item.custom_id] = mapped
            else:
                results[item.custom_id] = ModelResult(
                    text="",
                    parsed=None,
                    usage={"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                    custom_id=item.custom_id,
                )
        return results

    # ---- mock synthesis ----------------------------------------------------------

    @staticmethod
    def _mock_value_for_schema(schema: dict):
        """Produce a deterministic, schema-valid value for a JSON schema dict."""
        schema_type = schema.get("type")
        if schema_type == "object" or "properties" in schema:
            props = schema.get("properties", {})
            required = schema.get("required", list(props.keys()))
            return {
                key: Models._mock_value_for_schema(props.get(key, {"type": "string"}))
                for key in required
            }
        if schema_type == "array":
            return []
        if schema_type == "string":
            enum = schema.get("enum")
            return enum[0] if enum else "mock"
        if schema_type == "integer":
            return 0
        if schema_type == "number":
            return 0.0
        if schema_type == "boolean":
            return False
        return None

    def _mock_result(self, *, json_schema=None) -> ModelResult:
        zero_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        if json_schema is not None:
            parsed = self._mock_value_for_schema(json_schema)
            text = json.dumps(parsed)
            return ModelResult(text=text, parsed=parsed, usage=dict(zero_usage))
        # Deep-review-style canned findings, deterministic and schema-free.
        text = "[mock] deep review: no findings (mock mode, no API key configured)."
        return ModelResult(text=text, parsed=None, usage=dict(zero_usage))

    # ---- response mapping (real path) ---------------------------------------------

    @staticmethod
    def _map_response(response) -> ModelResult:
        text = ""
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text = block.text
                break
        parsed = None
        if text:
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                parsed = None
        usage = getattr(response, "usage", None)
        usage_dict = {
            "input": getattr(usage, "input_tokens", 0) or 0,
            "output": getattr(usage, "output_tokens", 0) or 0,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }
        return ModelResult(text=text, parsed=parsed, usage=usage_dict)
