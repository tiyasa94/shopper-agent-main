import json
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from shared import classifier, structured_generation
from shared.classifier_contract import GuardrailDecision


def _config(model_id: str = "watsonx/openai/gpt-oss-120b") -> dict[str, str]:
    return {
        "WXO_API_URL": "https://example.test",
        "WXO_API_KEY": "api-key",
        "WXO_MODEL_ID": model_id,
        "WXO_IAM_URL": "https://iam.example.test/token",
    }


def _decision_payload() -> dict[str, object]:
    payload: dict[str, object] = {
        "location_request": False,
        "availability_request": False,
        "greeting_only": False,
        "live_agent": False,
        "policy_override": False,
        "medicaid_related": False,
        "off_topic": False,
        "instructional_bias": False,
        "recommendation": False,
        "provider_lookup": False,
        "enrollment_action": False,
        "enroll_now_effective_date_question": False,
        "personalized_explanation": False,
    }
    return payload


def _response(content: object, *, finish_reason: str = "stop") -> Mock:
    response = Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": content},
            }
        ]
    }
    return response


def _classify_with_response(
    response: Mock,
    *,
    query: str = "What is a deductible?",
    market_segment: str = "Medicare",
):
    with (
        patch("shared.structured_generation._iam_token", return_value="token"),
        patch("shared.structured_generation._post", return_value=response),
        patch("shared.structured_generation.time.sleep"),
    ):
        return classifier.classify(
            query,
            market_segment,
            _config(),
            GuardrailDecision,
        )


def test_contract_rejects_omitted_fields():
    with pytest.raises(ValidationError):
        GuardrailDecision.model_validate({"enrollment_action": False})


def test_omitted_fields_retry_and_fail_schema_validation():
    response = _response(json.dumps({"enrollment_action": False}))

    with (
        patch("shared.structured_generation._iam_token", return_value="token"),
        patch("shared.structured_generation._post", return_value=response) as post,
        patch("shared.structured_generation.time.sleep") as sleep,
        pytest.raises(classifier.ClassifierError) as failure,
    ):
        classifier.classify(
            "Explain deductibles",
            "Medicare",
            _config(),
            GuardrailDecision,
        )

    assert failure.value.telemetry.failure_category == "schema_validation_failed"
    assert failure.value.telemetry.attempts == classifier.MAX_CLASSIFIER_ATTEMPTS
    assert post.call_count == classifier.MAX_CLASSIFIER_ATTEMPTS
    assert sleep.call_count == classifier.MAX_CLASSIFIER_ATTEMPTS - 1


def test_complete_contract_captures_finish_reason():
    response = _response(json.dumps(_decision_payload()), finish_reason="stop")

    result = _classify_with_response(response)

    assert result.telemetry.finish_reason == "stop"
    assert result.telemetry.last_http_status == 200
    assert result.telemetry.prompt_version == classifier.CLASSIFIER_PROMPT_VERSION
    assert result.telemetry.contract_version == classifier.CLASSIFIER_CONTRACT_VERSION


def test_classifier_sends_current_contract_to_wxo_gateway():
    with (
        patch.object(structured_generation, "_iam_token", return_value="token"),
        patch.object(
            structured_generation, "_post", return_value=_response(json.dumps(_decision_payload()))
        ) as post,
    ):
        classifier.classify(
            "What would I pay for a specialist visit?", "Medicare", _config(), GuardrailDecision
        )
    assert post.call_args.args == (
        "https://example.test/v1/orchestrate/gateway/model/chat/completions",
    )
    body = post.call_args.kwargs["json"]
    assert body["model"] == "watsonx/openai/gpt-oss-120b"
    assert body["max_completion_tokens"] == classifier.MAX_CLASSIFIER_TOKENS
    assert body["messages"][0]["content"] == classifier.CLASSIFIER_SYSTEM_PROMPT
    assert body["response_format"] == {"type": "json_object"}
    assert body["reasoning_effort"] == "low"
    assert "project_id" not in body and "model_id" not in body


@pytest.mark.parametrize("final_status", [200, 401])
def test_401_refreshes_once_even_with_single_generation_attempt(final_status):
    rejected = Mock(status_code=401)
    final = _response(json.dumps(_decision_payload()))
    final.status_code = final_status
    if final_status == 401:
        final.raise_for_status.side_effect = structured_generation.requests.HTTPError(
            response=final
        )
    with (
        patch.object(structured_generation, "_iam_token", side_effect=["stale", "fresh"]) as token,
        patch.object(structured_generation, "_post", side_effect=[rejected, final]) as post,
    ):
        args = dict(
            system_prompt="test",
            input_payload={},
            config=_config(),
            output_model=GuardrailDecision,
            max_tokens=1200,
            max_attempts=1,
            timeout_seconds=30,
        )
        if final_status == 200:
            result = structured_generation.generate_structured(**args)
            assert result.telemetry.attempts == 2
        else:
            with pytest.raises(structured_generation.StructuredGenerationError) as error:
                structured_generation.generate_structured(**args)
            assert error.value.telemetry.attempts == 2
            assert error.value.telemetry.failure_category == "http_4xx"
        assert token.call_count == post.call_count == 2
        assert token.call_args.kwargs["rejected_token"] == "stale"
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh"


def test_401_auth_failure_is_closed_and_not_retried_as_inference():
    with (
        patch.object(
            structured_generation, "_iam_token", side_effect=["old", ValueError("secret")]
        ),
        patch.object(structured_generation, "_post", return_value=Mock(status_code=401)) as post,
        pytest.raises(classifier.ClassifierError) as error,
    ):
        classifier.classify("query", "Medicare", _config(), GuardrailDecision)
    assert error.value.telemetry.attempts == 1
    assert error.value.telemetry.last_http_status == 401
    assert error.value.telemetry.failure_category == "iam_error"
    assert "secret" not in str(error.value)
    post.assert_called_once()


def test_prompt_hash_is_static_across_queries_and_markets():
    medicare_response = _response(json.dumps(_decision_payload()))
    individual_response = _response(json.dumps(_decision_payload()))

    first = _classify_with_response(medicare_response, query="What is a deductible?")
    second = _classify_with_response(medicare_response, query="What is coinsurance?")
    individual = _classify_with_response(
        individual_response,
        query="What is a deductible?",
        market_segment="IND",
    )

    assert first.telemetry.prompt_template_hash == second.telemetry.prompt_template_hash
    assert first.telemetry.prompt_template_hash == individual.telemetry.prompt_template_hash


def test_classifier_prompt_renders_the_versioned_contract():
    prompt = classifier.CLASSIFIER_SYSTEM_PROMPT

    assert prompt.strip()
    assert "{{OUTPUT_CONTRACT}}" not in prompt
    assert set(json.loads(prompt.rsplit("\n", 1)[-1])) == set(GuardrailDecision.model_fields)
    assert classifier.CLASSIFIER_PROMPT_VERSION
    assert classifier.CLASSIFIER_CONTRACT_VERSION
    assert classifier.CLASSIFIER_PROMPT_HASH
    assert "information_scope" not in GuardrailDecision.model_fields


@pytest.mark.parametrize(
    "query",
    [
        "Which one has the lower out-of-pocket maximum?",
        "Which available plan has the lowest medical deductible?",
        "Are there options that can help me pay deductibles and copays?",
        "What types of HMO plans are available?",
        "What optional plan options and rates does Example Medicare Advantage PPO include?",
    ],
)
def test_terminal_signal_validation_keeps_objective_facts_and_education_nonterminal(query):
    decision = GuardrailDecision.model_validate(
        {**_decision_payload(), "availability_request": True, "recommendation": True}
    )

    validated = classifier.validate_terminal_signals(query, decision)

    assert validated.availability_request is False
    assert validated.recommendation is False


@pytest.mark.parametrize(
    ("query", "field"),
    [
        ("Show me the available plans.", "availability_request"),
        ("How many Medicare plans are offered?", "availability_request"),
        ("Which plan should I choose?", "recommendation"),
        ("Which plan is best if I take medications regularly?", "recommendation"),
        ("Given that, should I choose it?", "recommendation"),
    ],
)
def test_terminal_signal_validation_recovers_explicit_false_negatives(query, field):
    decision = GuardrailDecision.model_validate(_decision_payload())

    validated = classifier.validate_terminal_signals(query, decision)

    assert getattr(validated, field) is True


def test_classifier_prompt_uses_compact_plan_offer_boundary():
    prompt = classifier.CLASSIFIER_SYSTEM_PROMPT

    assert '"Which plans are offered?" is true' in prompt
    assert '"Which plans offer meals or fitness programs?" is factual filtering and false' in prompt


def test_terminal_signal_validation_preserves_classifier_judgment_for_nuanced_wording():
    decision = GuardrailDecision.model_validate(
        {**_decision_payload(), "availability_request": True, "recommendation": True}
    )

    validated = classifier.validate_terminal_signals(
        "I want some help thinking through health plan tradeoffs.",
        decision,
    )

    assert validated is decision


def test_recommended_plan_display_false_positive_is_corrected_nonterminal():
    query = "Which plans are recommended?"
    decision = GuardrailDecision.model_validate(
        {**_decision_payload(), "availability_request": True, "recommendation": True}
    )

    validated = classifier.validate_terminal_signals(query, decision)

    assert validated is not decision
    assert validated.availability_request is False
    assert validated.recommendation is False
    assert classifier._explicit_recommendation_request(query) is False
    assert classifier._broad_availability_request(query) is False
    assert (
        '"Which plans are recommended?" has recommendation=false'
        in classifier.CLASSIFIER_SYSTEM_PROMPT
    )


@pytest.mark.parametrize(
    "query",
    [
        "Which plans are recommended?",
        "What plans were recommended?",
        "What are my recommended plans?",
        "Which are the recommended plans?",
    ],
)
def test_recommended_plan_display_override_is_limited_to_complete_phrases(query):
    assert classifier._RECOMMENDED_PLAN_DISPLAY_PATTERN.search(query)


@pytest.mark.parametrize(
    "query",
    [
        "Which plan do you recommend?",
        "Would you recommend Anthem Prime?",
        "What is the deductible of the recommended plan?",
    ],
)
def test_recommended_plan_display_override_excludes_advice_and_fact_requests(query):
    assert classifier._RECOMMENDED_PLAN_DISPLAY_PATTERN.search(query) is None


@pytest.mark.parametrize(
    "query",
    [
        "Please recommend a health plan.",
        "Would you recommend Anthem Prime?",
        "Which plan do you recommend?",
        "What's your plan recommendation?",
        "Could you advise me on which plan to choose?",
        "Do you endorse this plan?",
        "Is this plan right for me?",
    ],
)
def test_explicit_recommendation_regex_keeps_only_unmistakable_requests(query):
    assert classifier._explicit_recommendation_request(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "What is the deductible of the Wellpoint Medicare Advantage 1 plan you recommend?",
        "What is the deductible of the recommended Anthem Prime plan?",
        "Which plans are recommended?",
        "Compare deductible details, but do not recommend or select a plan.",
        "My doctor recommended this plan.",
        "Tell me about the best plan you mentioned.",
        "This plan is a good fit for me.",
    ],
)
def test_recommendation_regex_does_not_match_keywords_or_sentence_fragments(query):
    assert classifier._explicit_recommendation_request(query) is False


def test_named_recommended_plan_fact_is_a_classifier_owned_boundary():
    query = "What is the deductible of the Anthem Prime plan you recommend?"
    decision = GuardrailDecision.model_validate(_decision_payload())

    validated = classifier.validate_terminal_signals(query, decision)

    assert validated is decision
    assert (
        '"What is the deductible of the Anthem Prime plan you recommend?" also has '
        "recommendation=false" in classifier.CLASSIFIER_SYSTEM_PROMPT
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_tokens": 0}, "limits must be positive"),
        ({"max_retry_delay_seconds": -1}, "retry delay cannot be negative"),
    ],
)
def test_structured_generation_rejects_invalid_retry_limits(overrides, message):
    arguments = {
        "system_prompt": "Return JSON.",
        "input_payload": {"current_message": "test"},
        "config": _config(),
        "output_model": GuardrailDecision,
        "max_tokens": 100,
        "max_attempts": 1,
        "timeout_seconds": 1.0,
    }
    arguments.update(overrides)

    with pytest.raises(ValueError, match=message):
        structured_generation.generate_structured(**arguments)


def test_configuration_and_iam_failures_have_closed_categories():
    with pytest.raises(classifier.ClassifierError) as configuration_error:
        classifier.classify(
            "query",
            "Medicare",
            {},
            GuardrailDecision,
        )
    assert configuration_error.value.telemetry.failure_category == "configuration_error"
    assert configuration_error.value.telemetry.attempts == 0
    assert configuration_error.value.telemetry.missing_configuration == (
        classifier.REQUIRED_CLASSIFIER_CONFIG
    )

    with (
        patch(
            "shared.structured_generation._iam_token",
            side_effect=RuntimeError("secret IAM response"),
        ),
        pytest.raises(classifier.ClassifierError) as iam_error,
    ):
        classifier.classify(
            "query",
            "Medicare",
            _config(),
            GuardrailDecision,
        )
    assert iam_error.value.telemetry.failure_category == "iam_error"
    assert "secret IAM response" not in str(iam_error.value)


@pytest.mark.parametrize(
    ("case", "expected_category"),
    [
        ("http_429", "http_429"),
        ("http_5xx", "http_5xx"),
        ("timeout", "timeout"),
        ("connection", "connection_error"),
        ("invalid_envelope", "invalid_response_envelope"),
        ("non_json", "non_json_model_output"),
        ("schema", "schema_validation_failed"),
        ("truncated", "truncated_output"),
    ],
)
def test_retryable_failures_exhaust_into_distinct_closed_categories(case, expected_category):
    if case in {"http_429", "http_5xx"}:
        status = 429 if case == "http_429" else 503
        response = Mock(status_code=status, headers={"Retry-After": "1.25"})
        response.raise_for_status.side_effect = structured_generation.requests.HTTPError(
            response=response
        )
        side_effect = None
    elif case == "timeout":
        response = None
        side_effect = structured_generation.requests.Timeout("secret timeout")
    elif case == "connection":
        response = None
        side_effect = structured_generation.requests.ConnectionError("secret connection")
    elif case == "invalid_envelope":
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": []}
        side_effect = None
    elif case == "non_json":
        response = _response("not json")
        side_effect = None
    elif case == "schema":
        response = _response(
            json.dumps({**_decision_payload(), "enrollment_action": "unsupported"})
        )
        side_effect = None
    else:
        response = _response("not json", finish_reason="length")
        side_effect = None

    with (
        patch("shared.structured_generation._iam_token", return_value="token"),
        patch(
            "shared.structured_generation._post",
            return_value=response,
            side_effect=side_effect,
        ) as post,
        patch("shared.structured_generation.time.sleep") as sleep,
        pytest.raises(classifier.ClassifierError) as failure,
    ):
        classifier.classify(
            "private shopper query",
            "Medicare",
            _config(),
            GuardrailDecision,
        )

    telemetry = failure.value.telemetry
    assert telemetry.failure_category == expected_category
    assert telemetry.attempts == classifier.MAX_CLASSIFIER_ATTEMPTS
    assert post.call_count == classifier.MAX_CLASSIFIER_ATTEMPTS
    assert sleep.call_count == classifier.MAX_CLASSIFIER_ATTEMPTS - 1
    assert "private shopper query" not in str(failure.value)


def test_non_retryable_http_4xx_fails_on_first_attempt():
    response = Mock(status_code=400, headers={})
    response.raise_for_status.side_effect = structured_generation.requests.HTTPError(
        response=response
    )

    with (
        patch("shared.structured_generation._iam_token", return_value="token"),
        patch("shared.structured_generation._post", return_value=response) as post,
        patch("shared.structured_generation.time.sleep") as sleep,
        pytest.raises(classifier.ClassifierError) as failure,
    ):
        classifier.classify(
            "query",
            "Medicare",
            _config(),
            GuardrailDecision,
        )

    assert failure.value.telemetry.failure_category == "http_4xx"
    assert failure.value.telemetry.attempts == 1
    assert post.call_count == 1
    sleep.assert_not_called()


def test_failure_telemetry_builds_a_content_free_preflight_result():
    telemetry = classifier.failure_telemetry(
        "configuration_error",
        attempts=2,
    )

    assert telemetry.attempts == 2
    assert telemetry.failure_category == "configuration_error"
    assert telemetry.prompt_template_hash


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"choices": [{"message": {}}]},
    ],
)
def test_completion_rejects_invalid_response_shapes(body):
    with pytest.raises(structured_generation._InvalidResponseEnvelope):
        structured_generation._completion(body)


def test_decision_value_accepts_json_fences_and_rejects_non_objects():
    assert structured_generation._json_object('```json\n{"information_scope":"general"}\n```') == {
        "information_scope": "general"
    }

    with pytest.raises(structured_generation._OutputParseError) as failure:
        structured_generation._json_object("[]")

    assert failure.value.category == "schema_validation_failed"


def test_generate_structured_rejects_non_positive_max_tokens():
    with pytest.raises(ValueError, match="generation limits must be positive"):
        structured_generation.generate_structured(
            system_prompt="test",
            input_payload={},
            config=_config(),
            output_model=GuardrailDecision,
            max_tokens=0,
            max_attempts=1,
            timeout_seconds=1.0,
        )


def test_generate_structured_rejects_non_positive_max_attempts():
    with pytest.raises(ValueError, match="generation limits must be positive"):
        structured_generation.generate_structured(
            system_prompt="test",
            input_payload={},
            config=_config(),
            output_model=GuardrailDecision,
            max_tokens=100,
            max_attempts=0,
            timeout_seconds=1.0,
        )


def test_generate_structured_rejects_non_positive_timeout():
    with pytest.raises(ValueError, match="generation limits must be positive"):
        structured_generation.generate_structured(
            system_prompt="test",
            input_payload={},
            config=_config(),
            output_model=GuardrailDecision,
            max_tokens=100,
            max_attempts=1,
            timeout_seconds=0,
        )


def test_generate_structured_rejects_negative_retry_delay():
    with pytest.raises(ValueError, match="retry delay cannot be negative"):
        structured_generation.generate_structured(
            system_prompt="test",
            input_payload={},
            config=_config(),
            output_model=GuardrailDecision,
            max_tokens=100,
            max_attempts=1,
            timeout_seconds=1.0,
            max_retry_delay_seconds=-1,
        )


def test_non_json_http_body_retries_as_an_invalid_envelope():
    response = Mock(status_code=200)
    response.json.side_effect = json.JSONDecodeError("invalid", "not-json", 0)
    with (
        patch.object(structured_generation, "_iam_token", return_value="token"),
        patch.object(structured_generation, "_post", return_value=response),
        patch.object(structured_generation.time, "sleep"),
        pytest.raises(classifier.ClassifierError) as failure,
    ):
        classifier.classify("query", "Medicare", _config(), GuardrailDecision)

    assert failure.value.telemetry.failure_category == "invalid_response_envelope"


def test_rate_limit_with_invalid_retry_after_uses_exponential_fallback():
    response = Mock(status_code=429, headers={"Retry-After": "later"})
    response.raise_for_status.side_effect = structured_generation.requests.HTTPError(
        response=response
    )
    with (
        patch.object(structured_generation, "_iam_token", return_value="token"),
        patch.object(structured_generation, "_post", return_value=response),
        patch.object(structured_generation.time, "sleep") as sleep,
        pytest.raises(classifier.ClassifierError) as failure,
    ):
        classifier.classify("query", "Medicare", _config(), GuardrailDecision)

    assert failure.value.telemetry.failure_category == "http_429"
    assert failure.value.telemetry.retry_after_honored is False
    assert sleep.call_args_list[0].args == (1.0,)


def test_generic_request_exception_is_a_closed_connection_failure():
    with (
        patch.object(structured_generation, "_iam_token", return_value="token"),
        patch.object(
            structured_generation,
            "_post",
            side_effect=structured_generation.requests.RequestException("private provider detail"),
        ),
        patch.object(structured_generation.time, "sleep"),
        pytest.raises(classifier.ClassifierError) as failure,
    ):
        classifier.classify("query", "Medicare", _config(), GuardrailDecision)

    assert failure.value.telemetry.failure_category == "connection_error"
    assert "private provider detail" not in str(failure.value)
