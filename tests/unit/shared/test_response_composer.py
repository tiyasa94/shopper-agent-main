import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from shared import response_composer
from shared.response_composer import (
    GENERATED_TEXT_MARKER,
    ResponseCompositionSpec,
    compose_response,
)
from shared.structured_generation import GenerationResult, GenerationTelemetry


def _config() -> dict[str, str]:
    return {
        "WXO_API_URL": "https://example.test",
        "WXO_API_KEY": "api-key",
        "WXO_MODEL_ID": "watsonx/openai/gpt-oss-120b",
        "WXO_IAM_URL": "https://iam.example.test/token",
    }


def _spec(*, template: str | None = None) -> ResponseCompositionSpec:
    canned = "Please use the listed contact options for personalized assistance."
    return ResponseCompositionSpec(
        template_id="example_v1",
        template=template or f"{canned} {GENERATED_TEXT_MARKER}",
        instructions="Write one concise sentence acknowledging the request.",
        fallback_response=canned,
    )


def _response(content: str) -> Mock:
    response = Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"finish_reason": "stop", "message": {"content": content}}]
    }
    return response


def test_composer_sends_the_current_message_and_trusted_template_as_structured_input():
    response = _response(json.dumps({"generated_text": "I understand you want help enrolling."}))
    spec = _spec()
    with (
        patch("shared.structured_generation._iam_token", return_value="token"),
        patch("shared.structured_generation._post", return_value=response) as post,
    ):
        result = compose_response(spec, "Please enroll me in Prime.", _config())

    request = post.call_args.kwargs["json"]
    composer_input = json.loads(request["messages"][1]["content"])
    assert composer_input == {
        "current_message": "Please enroll me in Prime.",
        "response_template": spec.template,
        "insertion_marker": GENERATED_TEXT_MARKER,
        "composition_instructions": spec.instructions,
    }
    assert request["response_format"] == {"type": "json_object"}
    assert request["temperature"] == 0
    assert request["max_completion_tokens"] == 160
    assert request["reasoning_effort"] == "low"
    assert post.call_args.kwargs["timeout"] == 15
    assert result.message == (
        "Please use the listed contact options for personalized assistance. "
        "I understand you want help enrolling."
    )
    assert result.used_fallback is False


@pytest.mark.parametrize(
    "model_content",
    [
        "not json",
        json.dumps({"generated_text": "Valid sentence.", "unexpected": True}),
        json.dumps({"wrong_key": "Valid sentence."}),
    ],
)
def test_composer_makes_one_attempt_then_returns_only_the_canned_fallback(model_content):
    response = _response(model_content)
    spec = _spec()
    with (
        patch("shared.structured_generation._iam_token", return_value="token"),
        patch("shared.structured_generation._post", return_value=response) as post,
        patch("shared.structured_generation.time.sleep") as sleep,
    ):
        result = compose_response(spec, "Shopper request", _config())

    assert result.message == spec.fallback_response
    assert result.used_fallback is True
    assert result.attempts == 1
    assert post.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "generated_text",
    [
        "",
        "First line.\nSecond line.",
        "My diagnosis means I cannot help.",
        "Email an agent at help@example.com.",
        "Call an agent at 212-555-0100.",
        f"Do this {GENERATED_TEXT_MARKER}",
        "Please use the listed contact options for personalized assistance.",
        "x" * 301,
    ],
)
def test_composer_rejects_generated_text_that_breaks_the_slot_contract(generated_text):
    generation = GenerationResult(
        value=SimpleNamespace(generated_text=generated_text),
        telemetry=GenerationTelemetry(attempts=1, last_http_status=200),
    )
    spec = _spec()
    with patch.object(response_composer, "generate_structured", return_value=generation):
        result = compose_response(spec, "Shopper request", _config())

    assert result.message == spec.fallback_response
    assert result.used_fallback is True
    assert result.failure_category == "invalid_generated_text"


def test_composer_enforces_a_template_specific_generated_prefix():
    generation = GenerationResult(
        value=SimpleNamespace(generated_text="I hear that you are asking about the weather."),
        telemetry=GenerationTelemetry(attempts=1, last_http_status=200),
    )
    spec = ResponseCompositionSpec(
        template_id="acknowledgement_v1",
        template=f"{GENERATED_TEXT_MARKER} Canned response.",
        instructions="Acknowledge the subject.",
        fallback_response="Canned response.",
        required_generated_prefix="I understand you're asking about",
    )

    with patch.object(response_composer, "generate_structured", return_value=generation):
        result = compose_response(spec, "Shopper request", _config())

    assert result.message == spec.fallback_response
    assert result.used_fallback is True
    assert result.failure_category == "invalid_generated_text"


@pytest.mark.parametrize(
    "template",
    [
        "No insertion marker",
        f"{GENERATED_TEXT_MARKER} twice {GENERATED_TEXT_MARKER}",
    ],
)
def test_composition_spec_requires_exactly_one_insertion_marker(template):
    with pytest.raises(ValueError, match="exactly one marker"):
        _spec(template=template)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"template_id": " "}, "template ID"),
        ({"instructions": " "}, "requires instructions"),
        ({"fallback_response": " "}, "requires a fallback response"),
        (
            {"fallback_response": f"Fallback {GENERATED_TEXT_MARKER}"},
            "fallback cannot contain the marker",
        ),
        ({"max_generated_characters": 0}, "at least one character"),
        ({"required_generated_prefix": " "}, "prefix cannot be empty"),
    ],
)
def test_composition_spec_rejects_invalid_contract_fields(overrides, message):
    values = {
        "template_id": "example_v1",
        "template": f"Fallback {GENERATED_TEXT_MARKER}",
        "instructions": "Write one sentence.",
        "fallback_response": "Fallback",
    }
    values.update(overrides)

    with pytest.raises(ValueError, match=message):
        ResponseCompositionSpec(**values)
