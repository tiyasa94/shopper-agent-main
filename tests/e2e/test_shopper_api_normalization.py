"""Contract checks against shopper-assistant-api's current WXO normalizer."""

import json
from pathlib import Path

import pytest
from evaluation.shopper_api_normalization import _completed_payload, normalize_artifact

ROOT = Path(__file__).resolve().parents[2]

SHOPPER_API_ROOT = ROOT.parent / "shopper-platform/apps/shopper-assistant-api"


def _artifact(*, calls=(), responses=(), turn_result=None, response_text="Response"):
    context = {"_turn_result": json.dumps(turn_result)} if turn_result else {}
    return {
        "result": {
            "run_id": "compat-run",
            "response_text": response_text,
            "context": context,
            "tool_calls": list(calls),
            "tool_responses": list(responses),
        }
    }


def test_completed_payload_preserves_chat_accumulator_paths():
    result = _artifact(
        responses=[{"name": "search_plans", "content": {"plans_searched_for": ["Plan"]}}],
        turn_result={"schema_version": 1, "route": "search_turn"},
    )["result"]
    details = [{"type": "tool_response", "content": "{}"}]

    payload = _completed_payload(result, "Response", details)

    assert payload["data"]["message"]["context"] == result["context"]
    assert payload["step_history"] == [{"step_details": details}]
    assert "result" not in payload


@pytest.mark.skipif(not SHOPPER_API_ROOT.exists(), reason="shopper-platform checkout not available")
def test_terminal_turn_result_preserves_portal_escalation_contract():
    escalation = {
        "type": True,
        "identification": "live_agent_request",
        "description": "User explicitly requested a live agent (G03)",
    }
    normalized = normalize_artifact(
        _artifact(
            turn_result={
                "schema_version": 1,
                "route": "terminal",
                "business_intent": "generic_info",
                "escalation": escalation,
            }
        ),
        SHOPPER_API_ROOT,
    )

    assert normalized["business_intent"] == "generic_info"
    assert normalized["escalation"] == escalation


@pytest.mark.skipif(not SHOPPER_API_ROOT.exists(), reason="shopper-platform checkout not available")
def test_plan_search_preserves_portal_rag_and_intent_contract():
    normalized = normalize_artifact(
        _artifact(
            turn_result={
                "schema_version": 1,
                "route": "plan_turn",
                "business_intent": "specific_plan",
                "escalation": {"type": False},
            },
            responses=[
                {
                    "name": "search_plans",
                    "content": {
                        "outcome": "candidates",
                        "business_intent": "specific_plan",
                        "plans_found": ["$0 Cost Share EPO AI-AN"],
                        "plans_searched_for": ["$0 Cost Share EPO AI-AN"],
                        "results": [
                            {
                                "text": "Emergency room care has a $100 copay.",
                                "score": 0.9,
                                "metadata": {
                                    "plan_name": "$0 Cost Share EPO AI-AN",
                                    "document_id": "doc-1",
                                    "start_page": 12,
                                },
                            }
                        ],
                        "escalation": {"type": False},
                    },
                }
            ],
        ),
        SHOPPER_API_ROOT,
    )

    assert normalized["business_intent"] == "specific_plan"
    assert normalized["plans_found"] == ["$0 Cost Share EPO AI-AN"]
    assert normalized["plans_searched_for"] == ["$0 Cost Share EPO AI-AN"]
    assert normalized["rag_context_count"] == 1
    assert normalized["rag_context"][0]["reference"]["start_page"] == 12


@pytest.mark.skipif(not SHOPPER_API_ROOT.exists(), reason="shopper-platform checkout not available")
def test_mixed_source_trace_preserves_multi_plan_details_and_document_evidence():
    normalized = normalize_artifact(
        _artifact(
            turn_result={
                "schema_version": 1,
                "route": "plan_turn",
                "business_intent": "broad_plans",
                "escalation": {"type": False},
            },
            calls=[
                {
                    "id": "details-call",
                    "name": "get_plan_details",
                    "args": {
                        "plan_ids": ["8YC8", "8YCN"],
                        "detail_types": ["premium"],
                    },
                },
                {
                    "id": "search-call",
                    "name": "search_plans",
                    "args": {"plan_ids": ["8YC8", "8YCN"]},
                },
            ],
            responses=[
                {
                    "tool_call_id": "details-call",
                    "name": "get_plan_details",
                    "content": {
                        "outcome": "complete",
                        "coverage_complete": True,
                        "definitions": {
                            "completeness": (
                                "coverage_complete indicates whether every requested structured "
                                "detail type was returned for every selected plan."
                            ),
                            "detail_types": [
                                {
                                    "detail_type": "premium",
                                    "definition": "Provider-labeled monthly premium components.",
                                }
                            ],
                        },
                        "missing_plans": [],
                        "fallback_detail_types": [],
                    },
                },
                {
                    "tool_call_id": "search-call",
                    "name": "search_plans",
                    "content": {
                        "outcome": "candidates",
                        "business_intent": "broad_plans",
                        "plans_found": ["Gold", "Silver"],
                        "plans_searched_for": ["Gold", "Silver"],
                        "results": [
                            {
                                "text": "Gold and Silver have documented X-ray benefits.",
                                "score": 0.9,
                                "metadata": {"document_id": "doc-1", "start_page": 4},
                            }
                        ],
                    },
                },
            ],
        ),
        SHOPPER_API_ROOT,
    )

    assert normalized["business_intent"] == "broad_plans"
    assert normalized["plans_searched_for"] == ["Gold", "Silver"]
    assert normalized["rag_context_count"] == 1
    assert normalized["plan_details"] == {
        "called": True,
        "call_count": 1,
        "plan_ids": ["8YC8", "8YCN"],
        "detail_types": ["premium"],
        "outcome": "complete",
        "coverage_complete": True,
        "missing_plans": [],
        "fallback_detail_types": [],
    }


@pytest.mark.skipif(not SHOPPER_API_ROOT.exists(), reason="shopper-platform checkout not available")
def test_general_search_preserves_generic_business_intent_and_evidence():
    normalized = normalize_artifact(
        _artifact(
            turn_result={
                "schema_version": 1,
                "route": "general_turn",
                "business_intent": "generic_info",
                "escalation": {"type": False},
            },
            responses=[
                {
                    "name": "search_general_documents",
                    "content": {
                        "outcome": "candidates",
                        "business_intent": "generic_info",
                        "results": [
                            {
                                "text": "A deductible is an amount paid before coverage begins.",
                                "score": 0.8,
                                "metadata": {
                                    "document_id": "general-1",
                                    "start_page": 3,
                                },
                            }
                        ],
                    },
                }
            ],
        ),
        SHOPPER_API_ROOT,
    )

    assert normalized["business_intent"] == "generic_info"
    assert normalized["rag_context_count"] == 1
    assert normalized["rag_context"][0]["reference"]["start_page"] == 3


@pytest.mark.skipif(not SHOPPER_API_ROOT.exists(), reason="shopper-platform checkout not available")
@pytest.mark.parametrize(
    ("status", "identification"),
    [
        ("insufficient", "rag_insufficient_context"),
        ("error", "Triggered by Guardrails - RAG Error"),
    ],
)
def test_search_fallback_preserves_portal_escalation(status, identification):
    escalation = {"type": True, "identification": identification, "description": "fallback"}
    normalized = normalize_artifact(
        _artifact(
            turn_result={
                "schema_version": 1,
                "route": "plan_turn",
                "business_intent": "specific_plan",
                "escalation": {"type": False},
            },
            responses=[
                {
                    "name": "search_plans",
                    "content": {
                        "status": status,
                        "business_intent": "specific_plan",
                        "results": [],
                        "plans_found": [],
                        "plans_searched_for": ["Plan One"],
                        "escalation": escalation,
                    },
                }
            ],
        ),
        SHOPPER_API_ROOT,
    )

    assert normalized["business_intent"] == "specific_plan"
    assert normalized["plans_searched_for"] == ["Plan One"]
    assert normalized["escalation"] == escalation
