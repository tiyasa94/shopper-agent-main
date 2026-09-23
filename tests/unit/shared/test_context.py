"""Behavioral tests for trusted runtime-context helpers."""

import json
from unittest.mock import Mock

import pytest

from shared.context import (
    MAX_PLAN_CONTEXT_ITEMS,
    MAX_PLAN_CONTEXT_JSON_CHARS,
    authoritative_plans,
    contains_pii_phi,
    plan_identity_key,
    request_context,
    resolve_current_plan,
    retrieval_text,
    safe_plans,
    trusted_general_control,
    trusted_plan_control,
)


def _runtime(**request_values):
    runtime = Mock()
    runtime.request_context = request_values
    return runtime


def test_request_context_accepts_only_an_available_runtime_mapping():
    missing = Mock()
    missing.request_context = None
    present = _runtime(key="value")

    assert request_context(None) == {}
    assert request_context(missing) == {}
    assert request_context(present) == {"key": "value"}


def test_safe_plans_accepts_native_or_json_lists_and_rejects_other_values():
    plans = [{"plan_id": "123"}]

    assert safe_plans(plans) == plans
    assert safe_plans(json.dumps(plans)) == plans
    assert safe_plans(123) == []
    assert safe_plans("not valid json") == []
    assert safe_plans('{"plan_id": "123"}') == []


def test_safe_plans_rejects_inputs_that_exceed_resource_limits():
    too_many_plans = [{"plan_id": str(index)} for index in range(MAX_PLAN_CONTEXT_ITEMS + 1)]

    assert safe_plans(too_many_plans) == []
    assert safe_plans(json.dumps(too_many_plans)) == []
    assert safe_plans(" " * (MAX_PLAN_CONTEXT_JSON_CHARS + 1)) == []


def test_pii_phi_detection_covers_each_blocked_pattern_without_false_positive():
    blocked = [
        "My SSN is 123-45-6789",
        "My number is 123456789",
        "Card: 4111111111111111",
        "Card: 5500000000000004",
        "Card: 340000000000009",
        "MRN is #123456",
        "My medical record shows",
        "My diagnosis is",
        "What is my SSN?",
    ]

    for query in blocked:
        assert contains_pii_phi(query), query
    assert not contains_pii_phi("What is the deductible?")


def test_plan_identity_key_normalizes_runtime_identity_variants():
    equivalent_pairs = [
        ("Plan‐A", "Plan-A"),
        ("Plan–A", "Plan-A"),
        ("Plan—A", "Plan-A"),
        ("Plan ‘A’", "Plan 'A'"),
        ("Plan “A”", 'Plan "A"'),
        ("Plan\u200bA", "PlanA"),
        ("Plan\t A", "Plan A"),
        ("'Plan A'", "Plan A"),
        ("PLAN A", "plan a"),
    ]

    for variant, canonical in equivalent_pairs:
        assert plan_identity_key(variant) == plan_identity_key(canonical), variant


def test_authoritative_plans_filters_catalog_and_resolves_recommendations():
    plan_a_input = {
        "plan_id": " 123 ",
        "plan_name": " Plan A ",
        "visible": False,
        "private": "discard me",
    }
    plan_a = {"plan_id": "123", "plan_name": "Plan A"}
    plan_b = {"plan_id": "456", "plan_name": "Plan B"}
    runtime = _runtime(
        application_available_plans=json.dumps(
            [
                "not a plan",
                {"plan_name": "Missing ID"},
                plan_a_input,
                {"plan_id": "123", "plan_name": "Duplicate ID"},
                {**plan_b, "visible": True},
            ]
        ),
        application_recommended_plans=json.dumps(
            [
                "not a plan",
                {"plan_id": "123", "plan_name": "Stale name"},
                {"plan_id": "123", "plan_name": "Plan A"},
                {"plan_id": "stale", "plan_name": "Plan B"},
                {"plan_id": "unknown", "plan_name": "Unknown"},
            ]
        ),
    )

    assert authoritative_plans(None) == ([], [])
    assert authoritative_plans(runtime) == ([plan_a, plan_b], [plan_a, plan_b])


def test_authoritative_plans_rejects_oversized_identity_fields():
    runtime = _runtime(
        application_available_plans=[
            {"plan_id": "1" * 129, "plan_name": "Plan A"},
            {"plan_id": "123", "plan_name": "P" * 513},
            {"plan_id": 456, "plan_name": "Plan B"},
        ],
        application_recommended_plans=[],
    )

    assert authoritative_plans(runtime) == ([], [])


def test_authoritative_plans_rejects_over_limit_parser_results(monkeypatch):
    monkeypatch.setattr(
        "shared.context.safe_plans",
        lambda _value: [None] * (MAX_PLAN_CONTEXT_ITEMS + 1),
    )

    assert authoritative_plans(_runtime()) == ([], [])


def test_authoritative_plans_ignores_blank_identity_fields():
    runtime = _runtime(
        application_available_plans=[{"plan_id": " ", "plan_name": "Plan A"}],
        application_recommended_plans=[],
    )

    assert authoritative_plans(runtime) == ([], [])


def test_current_plan_requires_one_authoritative_id_or_name_match():
    available = [
        {"plan_id": "123", "plan_name": "Plan A"},
        {"plan_id": "456", "plan_name": "Plan B"},
    ]

    assert resolve_current_plan(None, available) is None
    assert resolve_current_plan(_runtime(user_current_plan="not valid json"), available) is None
    assert resolve_current_plan(_runtime(user_current_plan="Plan A"), available) == available[0]
    assert (
        resolve_current_plan(_runtime(user_current_plan=json.dumps({"plan_id": "456"})), available)
        == available[1]
    )
    assert (
        resolve_current_plan(_runtime(user_current_plan={"plan_name": "Plan B"}), available)
        == available[1]
    )
    assert (
        resolve_current_plan(
            _runtime(user_current_plan={"plan_name": "Plan A"}),
            [available[0], {"plan_id": "789", "plan_name": "Plan A"}],
        )
        is None
    )


def test_retrieval_text_removes_identity_and_preserves_a_bounded_topic():
    result = retrieval_text(
        "  What  is the deductible for Plan A (HMO)?  ",
        ["", "...", "(HMO)", "Plan A (HMO)"],
        limit=100,
    )

    assert result == "What is the deductible for?"
    assert retrieval_text("What applies to my plan?", [], limit=100) == "What applies to?"
    assert retrieval_text("...", [], limit=100) == ""
    assert retrieval_text("a" * 200, [], limit=50) == "a" * 50


def test_trusted_controls_require_valid_json_and_the_neutral_nonterminal_route():
    missing = _runtime()
    invalid = _runtime(_shopper_search_control="not valid json")
    old_route = _runtime(_shopper_search_control=json.dumps({"route": "general_turn"}))
    search_control = {"route": "search_turn"}
    search_runtime = _runtime(_shopper_search_control=json.dumps(search_control))

    with pytest.raises(ValueError, match="trusted search control is missing"):
        trusted_plan_control(missing)
    with pytest.raises(ValueError, match="trusted search control is invalid"):
        trusted_plan_control(invalid)
    with pytest.raises(ValueError, match="trusted search control is missing"):
        trusted_plan_control(old_route)
    assert trusted_plan_control(search_runtime) == search_control

    double_encoded = _runtime(_shopper_search_control=json.dumps(json.dumps(search_control)))
    assert trusted_plan_control(double_encoded) == search_control

    assert trusted_general_control(search_runtime) == search_control
    assert trusted_general_control(double_encoded) == search_control
