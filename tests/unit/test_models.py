"""Unit tests for models.py — all mock mode, no ANTHROPIC_API_KEY, no network.

Run: cd /workspace/qa-engine && MOCK_MODELS=1 .venv/bin/python -m pytest tests/unit/test_models.py -q
"""

import json

import config
from models import Models, ModelResult


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {"type": "object"},
        }
    },
    "required": ["items"],
}


def make_models():
    # Explicit mock=True: no key needed, no network, exercises the exact
    # same request-building code as the real path.
    return Models(mock=True)


# ---- build_request: pure request-shape assertions --------------------------------


def test_cache_control_on_first_system_block_only():
    m = make_models()
    req = m.build_request(
        model=config.MODEL_TIER2,
        system_blocks=[
            {"type": "text", "text": "rubric prefix"},
            {"type": "text", "text": "second block"},
            {"type": "text", "text": "third block"},
        ],
        user_content="hello",
        max_tokens=config.TIER2_MAX_TOKENS,
    )
    system = req["system"]
    assert len(system) == 3
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system[1]
    assert "cache_control" not in system[2]


def test_string_system_block_is_wrapped_and_cached():
    m = make_models()
    req = m.build_request(
        model=config.MODEL_TIER1,
        system_blocks="you are a triage assistant",
        user_content="hi",
        max_tokens=config.TIER1_MAX_TOKENS,
    )
    assert req["system"] == [
        {
            "type": "text",
            "text": "you are a triage assistant",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_model_id_threaded_per_tier():
    m = make_models()
    for model_id, max_tokens in (
        (config.MODEL_TIER1, config.TIER1_MAX_TOKENS),
        (config.MODEL_TIER2, config.TIER2_MAX_TOKENS),
        (config.MODEL_TIER3, config.TIER3_MAX_TOKENS),
    ):
        req = m.build_request(
            model=model_id,
            system_blocks="sys",
            user_content="user",
            max_tokens=max_tokens,
        )
        assert req["model"] == model_id
        assert req["max_tokens"] == max_tokens


def test_haiku_thinking_disabled_shape():
    m = make_models()
    req = m.build_request(
        model=config.MODEL_TIER1,
        system_blocks="triage rubric",
        user_content="page html",
        max_tokens=config.TIER1_MAX_TOKENS,
        thinking=config.TIER1_THINKING,  # "disabled"
    )
    assert req["thinking"] == {"type": "disabled"}
    # Haiku path never emits budget_tokens or effort by itself
    assert "budget_tokens" not in req.get("thinking", {})
    assert "output_config" not in req


def test_sonnet_and_opus_use_effort_not_budget_tokens():
    m = make_models()
    for model_id, effort in (
        (config.MODEL_TIER2, config.TIER2_EFFORT),
        (config.MODEL_TIER3, config.TIER3_EFFORT),
    ):
        req = m.build_request(
            model=model_id,
            system_blocks="deep review rubric",
            user_content="page content",
            max_tokens=config.TIER2_MAX_TOKENS,
            effort=effort,
        )
        assert req["output_config"]["effort"] == effort
        # Never send budget_tokens to the 5-series models.
        assert "budget_tokens" not in req
        assert "budget_tokens" not in req.get("thinking", {})
        assert "thinking" not in req or "budget_tokens" not in req["thinking"]


def test_json_schema_wired_into_output_config_format():
    m = make_models()
    req = m.build_request(
        model=config.MODEL_TIER1,
        system_blocks="triage rubric",
        user_content="page html",
        max_tokens=config.TIER1_MAX_TOKENS,
        json_schema=TRIAGE_SCHEMA,
    )
    assert req["output_config"]["format"] == {
        "type": "json_schema",
        "schema": TRIAGE_SCHEMA,
    }


def test_json_schema_and_effort_coexist_in_output_config():
    m = make_models()
    req = m.build_request(
        model=config.MODEL_TIER2,
        system_blocks="rubric",
        user_content="content",
        max_tokens=config.TIER2_MAX_TOKENS,
        effort=config.TIER2_EFFORT,
        json_schema=TRIAGE_SCHEMA,
    )
    assert req["output_config"]["effort"] == config.TIER2_EFFORT
    assert req["output_config"]["format"]["schema"] == TRIAGE_SCHEMA


# ---- call(): mock mode runs build_request under the hood -------------------------


def test_call_no_key_mock_mode_no_network_triage_shaped():
    m = make_models()
    result = m.call(
        model=config.MODEL_TIER1,
        system_blocks="triage rubric",
        user_content="page html",
        max_tokens=config.TIER1_MAX_TOKENS,
        thinking=config.TIER1_THINKING,
        json_schema=TRIAGE_SCHEMA,
    )
    assert isinstance(result, ModelResult)
    assert result.usage == {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    # Schema-valid: parsed matches the triage schema shape.
    assert result.parsed == {"items": []}
    assert json.loads(result.text) == {"items": []}


def test_call_deep_review_without_schema_returns_text():
    m = make_models()
    result = m.call(
        model=config.MODEL_TIER2,
        system_blocks="deep review rubric",
        user_content="page content",
        max_tokens=config.TIER2_MAX_TOKENS,
        effort=config.TIER2_EFFORT,
    )
    assert isinstance(result, ModelResult)
    assert result.parsed is None
    assert isinstance(result.text, str) and result.text


def test_models_constructed_with_no_key_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(config, "MOCK_MODELS", True)
    m = Models()  # no explicit mock=... — should pick up config.MOCK_MODELS
    assert m.mock is True
    assert m.client is None
    result = m.call(
        model=config.MODEL_TIER1,
        system_blocks="rubric",
        user_content="content",
        max_tokens=config.TIER1_MAX_TOKENS,
        thinking="disabled",
    )
    assert isinstance(result, ModelResult)


# ---- batch round-trip --------------------------------------------------------------


def test_submit_batch_and_batch_results_round_trip_by_custom_id():
    m = make_models()

    units = []
    for i in range(3):
        req = m.build_request(
            model=config.MODEL_TIER1,
            system_blocks="triage rubric",
            user_content=f"page-{i}",
            max_tokens=config.TIER1_MAX_TOKENS,
            thinking=config.TIER1_THINKING,
            json_schema=TRIAGE_SCHEMA,
        )
        units.append({"custom_id": f"unit-{i}", "request": req})

    batch_id = m.submit_batch(units)
    assert isinstance(batch_id, str) and batch_id

    status = m.poll_batch(batch_id)
    assert status["status"] == "ended"
    assert status["counts"]["succeeded"] == 3
    assert status["counts"]["errored"] == 0

    results = m.batch_results(batch_id)
    assert set(results.keys()) == {"unit-0", "unit-1", "unit-2"}
    for custom_id, result in results.items():
        assert isinstance(result, ModelResult)
        assert result.custom_id == custom_id
        assert result.parsed == {"items": []}


def test_batch_results_for_unknown_batch_id_is_empty():
    m = make_models()
    assert m.batch_results("does-not-exist") == {}
    status = m.poll_batch("does-not-exist")
    assert status == {"status": "ended", "counts": {"processing": 0, "succeeded": 0, "errored": 0}}
