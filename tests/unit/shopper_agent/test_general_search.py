import asyncio
import json
from unittest.mock import Mock, patch

import pytest
from ibm_watsonx_orchestrate.run.context import AgentRun

from shared.context import SEARCH_CONTROL_CONTEXT_KEY
from tools import general_search
from tools.general_search import search_general_documents

_ASYNC_GENERAL_SEARCH = search_general_documents.fn


@pytest.fixture(autouse=True)
def _run_registered_tool(monkeypatch):
    """Keep the behavioral suite synchronous while exercising the async tool wrapper."""

    def run(*args, **kwargs):
        return asyncio.run(_ASYNC_GENERAL_SEARCH(*args, **kwargs))

    monkeypatch.setattr(search_general_documents, "fn", run)


def _context(*, market="Medicare", prospect_type="prospect", brand="ABCBS"):
    return AgentRun(
        request_context={
            SEARCH_CONTROL_CONTEXT_KEY: json.dumps({"route": "search_turn"}),
            "request_id": "request-1",
            "user_requested_eff_date": "2026-01-01",
            "user_language": "en",
            "user_brand": brand,
            "application_market_segment": market,
            "prospect_type": prospect_type,
        }
    )


def _api_response(*, collection="MOLS"):
    response = Mock(status_code=200)
    response.headers = {"X-Request-ID": "rag-hop-1"}
    response.json.return_value = {
        "query": "What is a deductible?",
        "collection": collection,
        "strategy": "hybrid",
        "results": [
            {
                "rank": 1,
                "score": 0.91,
                "text": "A deductible is an amount paid before some coverage begins.",
                "metadata": {
                    "document_id": "general-1",
                    "document_type": (
                        "mols-general-medicare-info"
                        if collection == "MOLS"
                        else "iols-general-aca-info"
                    ),
                    "chunk_id": 42,
                    "start_page": 5,
                    "prop_brand": "ABCBS",
                },
            }
        ],
        "metrics": {"total_time_ms": 17.5},
        "metadata": {},
        "has_results": True,
    }
    return response


def _connection():
    return {
        "RAG_API_BASE_URL": "https://rag.example.test",
        "RAG_API_KEY": "test-rag-api-key",
        "RAG_MOLS_GENERAL_ENDPOINT": "/retrieve/mols/general",
        "RAG_IOLS_GENERAL_ENDPOINT": "/retrieve/iols/general",
    }


def _call(context):
    return search_general_documents.fn(
        queries=["health insurance deductible definition and when it applies"],
        context=context,
    )


def test_tool_requires_plugin_owned_search_control():
    context = _context()
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = ""

    result = _call(context)

    assert result.outcome == "error"
    assert "trusted general search control" in result.error_message
    assert result.required_response == general_search.CANONICAL_NO_EVIDENCE_RESPONSE


def test_general_tool_accepts_minimal_general_control():
    context = _context()
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps({"route": "search_turn"})
    response = _api_response()
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response) as post,
    ):
        result = _call(context)

    assert result.outcome == "candidates"
    assert post.call_count == 1


def test_general_tool_keeps_deterministic_personal_effective_date_policy():
    current_user_query = "When would my coverage start if I enroll today?"
    context = _context()
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(
        {"route": "search_turn", "current_user_query": current_user_query}
    )
    response = _api_response()
    response.json.return_value["results"][0]["text"] = (
        "The Medicare Annual Enrollment Period runs from October 15 through December 7."
    )
    with (
        patch.object(general_search.connections, "key_value", return_value=_connection()),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(context)

    assert result.outcome == "insufficient"
    assert result.results == []
    assert result.response_instructions is None
    assert result.required_response == general_search.CANONICAL_NO_EVIDENCE_RESPONSE
    assert result.escalation["identification"] == "rag_insufficient_context"


def test_general_tool_does_not_lexically_judge_no_action_or_hmo_pos_evidence():
    assert (
        general_search._policy_insufficiency_reason(
            "What happens if I don't make changes during Open Enrollment?"
        )
        is None
    )
    assert (
        general_search._policy_insufficiency_reason(
            "What is the difference between an HMO and an HMO-POS?"
        )
        is None
    )


def test_general_tool_accepts_semantically_equivalent_hmo_pos_wording():
    context = _context()
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(
        {
            "route": "search_turn",
            "current_user_query": "What is the difference between an HMO and an HMO-POS?",
        }
    )
    response = _api_response()
    response.json.return_value["results"][0]["text"] = (
        "An HMO with a point-of-service option may allow specified services outside its network."
    )

    with (
        patch.object(general_search.connections, "key_value", return_value=_connection()),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(context)

    assert result.outcome == "candidates"
    assert "point-of-service option" in result.results[0].text


def test_medicare_general_search_uses_general_endpoint_and_public_metadata():
    response = _api_response()
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response) as post,
        patch.object(general_search.logger, "info") as log,
    ):
        result = _call(_context())

    assert post.call_args.args[0] == "https://rag.example.test/retrieve/mols/general"
    assert post.call_args.kwargs["headers"]["X-API-Key"] == "test-rag-api-key"
    assert "X-Request-ID" not in post.call_args.kwargs["headers"]
    payload = post.call_args.kwargs["json"]
    assert payload["user_type"] == "prospect"
    assert payload["effective_year"] == 2026
    assert payload["brand"] == "ABCBS"
    assert "plan_ids" not in payload
    assert "available_plans" not in payload
    assert "document_type" not in payload
    assert payload["searches"] == [
        {
            "semantic_query": "health insurance deductible definition and when it applies",
            "requested_facts": [],
            "conditions": [],
            "lexical_terms": [],
        }
    ]
    assert result.outcome == "candidates"
    assert result.evidence_status == "unreviewed_candidates"
    assert result.requested_fact == "health insurance deductible definition and when it applies"
    assert result.original_user_query == result.requested_fact
    assert result.executed_searches == [result.requested_fact]
    assert result.candidate_validation_status == "UNVALIDATED"
    assert result.response_instructions == general_search.MEDICARE_CANDIDATE_RESPONSE_INSTRUCTIONS
    assert "candidate_evidence_instruction" not in result.model_dump()
    assert result.required_response_if_unsupported == general_search.CANONICAL_NO_EVIDENCE_RESPONSE
    assert result.business_intent == "generic_info"
    assert result.retrieval_method == "hybrid"
    assert result.retrieval_time_ms == 17
    assert result.results[0].metadata.document_type == "mols-general-medicare-info"
    assert result.results[0].metadata.chunk_id == "42"
    assert result.results[0].metadata.start_page == 5
    assert result.escalation == {
        "type": False,
        "identification": "rag_response",
        "description": "Normal RAG response logging",
    }
    assert result.audit_completed is True
    assert "audit_payload" not in result.model_dump()
    assert "audit_request_id" not in result.model_dump()
    log.assert_any_call(
        "rag_response_received",
        extra={
            "request_id": "request-1",
            "downstream_request_id": "rag-hop-1",
            "status_code": 200,
        },
    )


def test_individual_member_search_uses_iols_endpoint_and_member_user_type():
    response = _api_response(collection="IOLS")
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response) as post,
    ):
        result = _call(_context(market="IND", prospect_type="member"))

    assert post.call_args.args[0] == "https://rag.example.test/retrieve/iols/general"
    assert post.call_args.kwargs["json"]["user_type"] == "member"
    assert result.results[0].metadata.document_type == "iols-general-aca-info"
    assert result.response_instructions == general_search.GENERAL_CANDIDATE_RESPONSE_INSTRUCTIONS


@pytest.mark.parametrize(
    ("connection", "expected_error"),
    [
        (
            {
                "RAG_MOLS_GENERAL_ENDPOINT": "/retrieve/mols/general",
            },
            "RAG_API_BASE_URL is not configured",
        ),
        (
            {
                "RAG_API_BASE_URL": "https://rag.example.test",
                "RAG_API_KEY": "test-rag-api-key",
            },
            "RAG_MOLS_GENERAL_ENDPOINT is not configured",
        ),
        (
            {
                "RAG_API_BASE_URL": "https://rag.example.test",
                "RAG_MOLS_GENERAL_ENDPOINT": "/retrieve/mols/general",
            },
            "RAG_API_KEY is not configured",
        ),
    ],
)
def test_general_search_rejects_missing_connection_configuration(connection, expected_error):
    with (
        patch.object(general_search.connections, "key_value", return_value=connection),
        patch.object(general_search.requests, "post") as post,
    ):
        result = _call(_context())

    assert result.outcome == "error"
    assert result.error_message == expected_error
    post.assert_not_called()


def test_empty_general_result_is_an_escalated_retrieval_error():
    response = _api_response()
    response.json.return_value["results"] = []
    response.json.return_value["has_results"] = False
    response.json.return_value["no_results_reason"] = "no_data_for_filters"
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(_context())

    assert result.outcome == "error"
    assert result.no_results_reason == "no_data_for_filters"
    assert result.escalation["type"] is True
    assert result.audit_completed is True
    assert result.escalation["identification"] == "Triggered by Guardrails - RAG Error"


def test_declared_insufficient_general_evidence_is_not_exposed_to_agent():
    response = _api_response()
    metadata = response.json.return_value["metadata"]
    metadata["evidence_quality"] = "insufficient"
    metadata["evidence_reason"] = "Retrieved passages do not address general search facet 1"
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(_context())

    assert result.outcome == "insufficient"
    assert result.results == []
    assert result.response_instructions is None
    assert result.candidate_validation_status == "UNAVAILABLE"
    assert result.insufficiency_reason == (
        "Retrieved passages do not address general search facet 1"
    )
    assert result.escalation["type"] is True
    assert result.audit_completed is True
    assert result.escalation["identification"] == "rag_insufficient_context"


def test_general_response_preserves_original_question_separately_from_executed_searches():
    context = _context()
    original = "Does smoking/tobacco/vaping/e-cigarettes change my rate?"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(
        {"route": "search_turn", "current_user_query": original}
    )
    with (
        patch.object(general_search.connections, "key_value", return_value=_connection()),
        patch.object(general_search.requests, "post", return_value=_api_response()) as post,
    ):
        result = search_general_documents.fn(
            queries=[" smoking and rates ", "SMOKING AND RATES", "vaping and rates"],
            context=context,
        )

    assert result.original_user_query == result.requested_fact == original
    assert result.executed_searches == ["smoking and rates", "vaping and rates"]
    assert result.executed_searches == [
        search["semantic_query"] for search in post.call_args.kwargs["json"]["searches"]
    ]
    assert original not in result.response_instructions
    assert result.results[0].text == "A deductible is an amount paid before some coverage begins."
    assert result.required_response is None
    assert result.required_response_if_unsupported == general_search.CANONICAL_NO_EVIDENCE_RESPONSE


def test_general_search_rejects_more_than_five_queries():
    result = search_general_documents.fn(
        queries=[f"general topic {index}" for index in range(6)],
        context=_context(),
    )

    assert result.outcome == "error"
    assert result.error_message == "queries must contain 1-5 strings"


def test_general_search_expands_each_query_into_an_independent_search():
    response = _api_response()
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response) as post,
    ):
        result = search_general_documents.fn(
            queries=[
                "HMO network and specialist referral rules",
                "HMO-POS network and out-of-network coverage rules",
            ],
            context=_context(),
        )

    assert result.outcome == "candidates"
    payload = post.call_args.kwargs["json"]
    assert [search["semantic_query"] for search in payload["searches"]] == [
        "HMO network and specialist referral rules",
        "HMO-POS network and out-of-network coverage rules",
    ]


def test_general_search_fails_closed_when_required_audit_write_fails():
    response = _api_response()
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response),
        patch.object(
            general_search,
            "emit_audit_event",
            side_effect=RuntimeError("audit sink rejected event"),
        ),
        pytest.raises(RuntimeError, match="audit sink rejected event"),
    ):
        _call(_context())


def test_general_search_rejects_non_string_query():
    """Test that general search rejects queries containing non-string elements."""
    result = search_general_documents.fn(
        queries=["valid query", 123, "another query"],
        context=_context(),
    )
    assert result.outcome == "error"
    assert "queries must contain 1-5 strings" in result.error_message


def test_general_search_rejects_empty_query():
    """Test that general search rejects empty queries after stripping."""
    result = search_general_documents.fn(
        queries=["   ", "valid query"],
        context=_context(),
    )
    assert result.outcome == "error"
    assert "each query must contain 1-1000 characters" in result.error_message


def test_general_search_rejects_query_exceeding_max_length():
    """Test that general search rejects queries longer than 1000 characters."""
    result = search_general_documents.fn(
        queries=["x" * 1001],
        context=_context(),
    )
    assert result.outcome == "error"
    assert "each query must contain 1-1000 characters" in result.error_message


def test_general_search_rejects_missing_runtime_context():
    """Test that general search rejects calls without runtime context."""
    result = search_general_documents.fn(
        queries=["test query"],
        context=None,
    )
    assert result.outcome == "error"
    assert "agent runtime context is required" in result.error_message


def test_general_search_rejects_invalid_market_segment():
    """Test that general search rejects invalid market segments."""
    context = _context()
    context.request_context["application_market_segment"] = "invalid"
    result = _call(context)
    assert result.outcome == "error"
    assert "application_market_segment must be IND or Medicare" in result.error_message


@pytest.mark.parametrize(
    "brand",
    ["", "future-brand"],
)
def test_general_search_leaves_brand_contract_validation_to_rag_api(brand):
    response = Mock(status_code=422)
    response.headers = {"X-Request-ID": "rag-hop-1"}
    response.json.return_value = {"detail": "Invalid brand"}
    with (
        patch.object(general_search.connections, "key_value", return_value=_connection()),
        patch.object(general_search.requests, "post", return_value=response) as post,
    ):
        result = _call(_context(brand=brand))

    assert result.outcome == "error"
    assert result.error_message == "RAG API returned HTTP 422"
    assert post.call_args.kwargs["json"]["brand"] == brand


def test_general_search_rejects_language_exceeding_max_length():
    """Test that general search rejects language longer than 10 characters."""
    context = _context()
    context.request_context["user_language"] = "x" * 11
    result = _call(context)
    assert result.outcome == "error"
    assert "user_language must contain 1-10 characters" in result.error_message


def test_general_search_deduplicates_queries_case_insensitively():
    """Test that general search deduplicates queries case-insensitively."""
    response = _api_response()
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response) as post,
    ):
        result = search_general_documents.fn(
            queries=["HMO network rules", "hmo network rules", "HMO NETWORK RULES"],
            context=_context(),
        )
    assert result.outcome == "candidates"
    payload = post.call_args.kwargs["json"]
    # Should only have one query after deduplication
    assert len(payload["searches"]) == 1
    assert payload["searches"][0]["semantic_query"] == "HMO network rules"


def test_general_search_handles_api_request_exception():
    """Test that general search handles API request exceptions."""
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(
            general_search.requests,
            "post",
            side_effect=general_search.requests.exceptions.ConnectionError("Connection failed"),
        ),
    ):
        result = _call(_context())
    assert result.outcome == "error"
    assert "RAG API request failed" in result.error_message


def test_general_search_handles_non_json_response():
    """Test that general search handles non-JSON responses."""
    response = Mock(status_code=200)
    response.json.side_effect = ValueError("Invalid JSON")
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(_context())
    assert result.outcome == "error"
    assert "RAG API returned non-JSON HTTP 200" in result.error_message


def test_general_search_handles_non_dict_response():
    """Test that general search handles non-dict JSON responses."""
    response = Mock(status_code=200)
    response.json.return_value = ["not", "a", "dict"]
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(_context())
    assert result.outcome == "error"
    assert "RAG API returned an invalid response object" in result.error_message


def test_general_search_handles_non_200_status_code():
    """Test that general search handles non-200 status codes."""
    response = Mock(status_code=500)
    response.json.return_value = {"error": "Internal server error"}
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(_context())
    assert result.outcome == "error"
    assert "RAG API returned HTTP 500" in result.error_message


def test_general_search_validates_response_contract():
    """Test that general search validates the response contract."""
    response = _api_response()
    response.json.return_value["collection"] = "WRONG"
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(_context())
    assert result.outcome == "error"
    assert "RAG API response contract validation failed" in result.error_message


def test_general_search_validates_result_structure():
    """Test that general search validates individual result structure."""
    response = _api_response()
    response.json.return_value["results"][0] = "not a dict"
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(_context())
    assert result.outcome == "error"
    assert "result is not an object" in result.error_message


def test_general_search_validates_result_has_required_fields():
    """Test that general search validates results have required fields."""
    response = _api_response()
    response.json.return_value["results"][0]["metadata"]["document_id"] = ""
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(_context())
    assert result.outcome == "error"
    assert "result is missing trusted document evidence" in result.error_message


def test_general_search_rejects_cross_brand_result_metadata():
    response = _api_response()
    response.json.return_value["results"][0]["metadata"]["prop_brand"] = "WLP"
    with (
        patch.object(general_search.connections, "key_value", return_value=_connection()),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(_context())

    assert result.outcome == "error"
    assert "result is missing trusted document evidence" in result.error_message


def test_general_search_handles_negative_retrieval_time():
    """Test that general search handles negative retrieval time."""
    response = _api_response()
    response.json.return_value["metrics"]["total_time_ms"] = -5
    with (
        patch.object(
            general_search.connections,
            "key_value",
            return_value=_connection(),
        ),
        patch.object(general_search.requests, "post", return_value=response),
    ):
        result = _call(_context())
    assert result.outcome == "candidates"
    assert result.retrieval_time_ms is None
