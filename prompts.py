"""Rubric loading + shared JSON-output schemas for the LLM tiers."""

import functools
import os

_RUBRIC_DIR = os.path.join(os.path.dirname(__file__), "rubrics")


@functools.lru_cache(maxsize=None)
def rubric(name: str) -> str:
    """Load a rubric markdown file (cached). Passed as a cached system prompt."""
    with open(os.path.join(_RUBRIC_DIR, name), encoding="utf-8") as fh:
        return fh.read()


_SEVERITY_ENUM = ["info", "minor", "moderate", "serious", "critical"]

# Tier-1 triage output (Haiku). Matches rubrics/triage.md.
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "pipeline": {"type": "string", "enum": ["wcag", "security", "ux"]},
                    "worth_deep_review": {"type": "boolean"},
                    "severity": {"type": "string", "enum": _SEVERITY_ENUM},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": [
                    "ref", "pipeline", "worth_deep_review",
                    "severity", "confidence", "reason",
                ],
            },
        }
    },
    "required": ["items"],
}

# Tier-2 deep-review / vision output (Sonnet). Wrapped in an object (top-level
# arrays are avoided for structured output portability).
DEEP_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string"},
                    "severity": {"type": "string", "enum": _SEVERITY_ENUM},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "evidence": {"type": "object"},
                    "escalate": {"type": "boolean"},
                },
                "required": ["rule", "severity", "title", "detail"],
            },
        }
    },
    "required": ["findings"],
}
