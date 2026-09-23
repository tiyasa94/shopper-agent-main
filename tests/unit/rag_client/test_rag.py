"""Unit tests for the refactored plan-only search_plans tool."""

import asyncio
import json
from unittest.mock import Mock, patch
from uuid import UUID

import httpx
import pytest

from shared.rag import RetrievalSearch, SearchPlansResponse, search_plans


def _search(
    query,
    plan_ids,
    context,
    business_intent=None,
    searches=None,
):
    return asyncio.run(
        search_plans(
            query=query,
            searches=searches
            or [
                RetrievalSearch(
                    semantic_query=(
                        query.strip()[:1000]
                        if isinstance(query, str) and query.strip()
                        else "benefits"
                    ),
                )
            ],
            plan_ids=plan_ids,
            business_intent=business_intent
            or ("specific_plan" if len(plan_ids) == 1 else "broad_plans"),
            context=context,
        )
    )


def _credentials(**overrides):
    values = {
        "RAG_API_BASE_URL": "https://rag.example.test",
        "RAG_API_KEY": "test-rag-api-key",
        "RAG_IOLS_ENDPOINT": "/retrieve/iols",
        "RAG_MOLS_ENDPOINT": "/retrieve/mols",
    }
    values.update(overrides)
    connection = Mock()
    connection.get.side_effect = lambda key, default="": values.get(key, default)
    return connection


def _response(
    request_id="request-1",
    collection="IOLS",
    plan_id="8XWE",
    results=None,
    status_code=200,
):
    if results is None:
        results = [
            {
                "rank": 1,
                "score": 0.9,
                "text": "The plan has a deductible.",
                "metadata": {
                    "document_id": "doc-1",
                    "prop_plan_id": plan_id,
                    "start_page": 12,
                },
            }
        ]
    response = Mock()
    response.status_code = status_code
    response.headers = {"X-Request-ID": request_id}
    response.json.return_value = {
        "query": "What is the deductible?",
        "collection": collection,
        "results": results,
        "has_results": bool(results),
        "metadata": {},
        "no_results_reason": None if results else "no_data_for_filters",
        "no_results_message": None if results else "No matching documents",
        "suggestions": None if results else ["Try rephrasing the query"],
    }
    return response


def _assert_failed_plan_context(result):
    assert result.business_intent == "specific_plan"
    assert result.plans_searched_for == ["Anthem Silver 70"]
    assert result.missing_plans == ["Anthem Silver 70"]


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_sends_the_refactored_iols_contract(
    mock_key_value, mock_post, mock_agent_context
):
    mock_agent_context.request_context["request_id"] = "request-1"
    mock_key_value.return_value = _credentials()
    mock_post.return_value = _response(request_id="rag-hop-1")

    with patch("shared.rag.logger.info") as log:
        tool_result = _search(
            query="What is the deductible?",
            plan_ids=["8XWE"],
            context=mock_agent_context,
        )

    result = tool_result
    assert isinstance(result, SearchPlansResponse)
    assert result.model_dump() == {
        "outcome": "candidates",
        "business_intent": "specific_plan",
        "results": [
            {
                "evidence_id": "evidence-1",
                "rank": 1,
                "score": 0.9,
                "text": "The plan has a deductible.",
                "plan_name": "Anthem Silver 70",
                "metadata": {
                    "plan_name": "Anthem Silver 70",
                    "document_id": "doc-1",
                    "document_type": None,
                    "chunk_id": None,
                    "section_name": None,
                    "start_page": 12,
                    "source_url": None,
                },
            }
        ],
        "plans_found": ["Anthem Silver 70"],
        "plans_searched_for": ["Anthem Silver 70"],
        "coverage_complete": True,
        "missing_plans": [],
        "insufficiency_reason": None,
        "error_message": None,
        "no_results_reason": None,
        "retrieval_method": None,
        "retrieval_time_ms": None,
        "escalation": {"type": False},
    }
    url = mock_post.call_args.args[0]
    kwargs = mock_post.call_args.kwargs
    assert url == "https://rag.example.test/retrieve/iols"
    assert kwargs["headers"]["X-API-Key"] == "test-rag-api-key"
    assert "X-Request-ID" not in kwargs["headers"]
    assert kwargs["timeout"] == 30
    assert kwargs["json"] == {
        "query": "What is the deductible?",
        "searches": [
            {
                "semantic_query": "What is the deductible?",
                "requested_facts": [],
                "conditions": [],
                "lexical_terms": [],
            }
        ],
        "plan_ids": ["8XWE"],
        "available_plans": [{"plan_id": "8XWE", "plan_name": "Anthem Silver 70"}],
        "baseline_plan_id": "8XWE",
        "effective_year": 2026,
        "language": "en",
        "user_type": "prospect",
        "state_code": "CA",
        "exchange_indicator": "Off",
    }
    assert "business_intent" not in kwargs["json"]
    assert "top_k" not in kwargs["json"]
    assert "context" not in kwargs["json"]
    log.assert_any_call(
        "rag_response_received",
        extra={
            "request_id": "request-1",
            "downstream_request_id": "rag-hop-1",
            "status_code": 200,
        },
    )


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_forwards_multiple_bounded_searches(
    mock_key_value, mock_post, mock_agent_context
):
    mock_agent_context.request_context["request_id"] = "request-1"
    mock_key_value.return_value = _credentials()
    mock_post.return_value = _response()
    searches = [
        {"semantic_query": "What services are covered?", "lexical_terms": ["benefits"]},
        {
            "semantic_query": "What costs apply?",
            "lexical_terms": ["copay", "deductible"],
        },
    ]

    result = _search(
        "Summarize benefits and costs.",
        ["8XWE"],
        mock_agent_context,
        searches=searches,
    )

    assert result.outcome == "candidates"
    payload = mock_post.call_args.kwargs["json"]
    assert [search["semantic_query"] for search in payload["searches"]] == [
        "What services are covered?",
        "What costs apply?",
    ]


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_routes_medicare_without_exchange_indicator(
    mock_key_value, mock_post, mock_agent_context_medicare
):
    mock_agent_context_medicare.request_context["request_id"] = "request-2"
    mock_agent_context_medicare.request_context["prospect_type"] = "member"
    mock_key_value.return_value = _credentials()
    mock_post.return_value = _response(request_id="request-2", collection="MOLS", plan_id="MED123")

    result = _search(
        query="What are the drug benefits?",
        plan_ids=["MED123"],
        context=mock_agent_context_medicare,
    )

    assert result.outcome == "candidates"
    assert result.plans_found == ["Medicare Advantage Plan"]
    assert result.plans_searched_for == ["Medicare Advantage Plan"]
    assert mock_post.call_args.args[0].endswith("/retrieve/mols")
    payload = mock_post.call_args.kwargs["json"]
    assert payload["user_type"] == "member"
    assert "exchange_indicator" not in payload


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_preserves_selected_plan_order(mock_key_value, mock_post, mock_agent_context):
    mock_agent_context.request_context["request_id"] = "request-3"
    mock_agent_context.request_context["application_available_plans"] = json.dumps(
        [
            {"plan_id": "P1", "plan_name": "Plan One"},
            {"plan_id": "P2", "plan_name": "Plan Two"},
        ]
    )
    mock_key_value.return_value = _credentials()
    mock_post.return_value = _response(
        request_id="request-3",
        results=[
            {
                "rank": 1,
                "score": 0.9,
                "text": "Plan Two benefit.",
                "metadata": {"document_id": "doc-2", "prop_plan_id": "P2"},
            },
            {
                "rank": 2,
                "score": 0.8,
                "text": "Plan One benefit.",
                "metadata": {"document_id": "doc-1", "prop_plan_id": "P1"},
            },
        ],
    )

    result = _search(
        query="Compare these plans",
        plan_ids=["P2", "P1"],
        context=mock_agent_context,
    )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["plan_ids"] == ["P2", "P1"]
    assert payload["baseline_plan_id"] == "P2"
    assert result.plans_searched_for == ["Plan Two", "Plan One"]
    assert result.plans_found == ["Plan Two", "Plan One"]
    assert [item.plan_name for item in result.results] == ["Plan Two", "Plan One"]


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_exposes_incomplete_multi_plan_coverage(
    mock_key_value, mock_post, mock_agent_context
):
    mock_agent_context.request_context["request_id"] = "request-4"
    mock_agent_context.request_context["application_available_plans"] = json.dumps(
        [
            {"plan_id": "P1", "plan_name": "Plan One"},
            {"plan_id": "P2", "plan_name": "Plan Two"},
        ]
    )
    mock_key_value.return_value = _credentials()
    mock_post.return_value = _response(
        request_id="request-4",
        results=[
            {
                "rank": 1,
                "score": 0.95,
                "text": "Only Plan One has supporting evidence.",
                "metadata": {"document_id": "doc-1", "prop_plan_id": "P1"},
            }
        ],
    )

    result = _search("Compare plans", ["P1", "P2"], mock_agent_context)

    assert result.coverage_complete is False
    assert result.missing_plans == ["Plan Two"]
    assert result.outcome == "insufficient"
    assert result.insufficiency_reason == "No evidence was returned for every requested plan"


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_tracks_duplicate_display_name_coverage_by_plan_id(
    mock_key_value, mock_post, mock_agent_context
):
    mock_agent_context.request_context["request_id"] = "request-same-name"
    mock_agent_context.request_context["application_available_plans"] = json.dumps(
        [
            {"plan_id": "P1", "plan_name": "Shared Plan"},
            {"plan_id": "P2", "plan_name": "Shared Plan"},
        ]
    )
    mock_key_value.return_value = _credentials()
    mock_post.return_value = _response(
        request_id="request-same-name",
        results=[
            {
                "rank": 1,
                "score": 0.95,
                "text": "Only P1 has supporting evidence.",
                "metadata": {"document_id": "doc-1", "prop_plan_id": "P1"},
            }
        ],
    )

    result = _search("Compare plans", ["P1", "P2"], mock_agent_context)

    assert result.plans_searched_for == ["Shared Plan", "Shared Plan"]
    assert result.plans_found == ["Shared Plan"]
    assert result.missing_plans == ["Shared Plan"]
    assert result.coverage_complete is False
    assert result.outcome == "insufficient"


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_preserves_declared_insufficient_evidence(
    mock_key_value, mock_post, mock_agent_context
):
    mock_agent_context.request_context["request_id"] = "request-5"
    mock_key_value.return_value = _credentials()
    response = _response(request_id="request-5")
    response.json.return_value["metadata"].update(
        {
            "evidence_quality": "insufficient",
            "evidence_reason": "The retrieved text does not state the requested amount",
        }
    )
    mock_post.return_value = response

    result = _search("What is the amount?", ["8XWE"], mock_agent_context)

    assert result.outcome == "insufficient"
    assert result.coverage_complete is True
    assert result.insufficiency_reason == "The retrieved text does not state the requested amount"


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_normalizes_valid_no_results_to_error(
    mock_key_value, mock_post, mock_agent_context
):
    mock_agent_context.request_context["request_id"] = "request-1"
    mock_key_value.return_value = _credentials()
    mock_post.return_value = _response(results=[])

    result = _search("Unknown benefit", ["8XWE"], mock_agent_context)

    assert result.model_dump() == {
        "outcome": "error",
        "business_intent": "specific_plan",
        "results": [],
        "plans_found": [],
        "plans_searched_for": ["Anthem Silver 70"],
        "coverage_complete": False,
        "missing_plans": ["Anthem Silver 70"],
        "insufficiency_reason": None,
        "error_message": "RAG API returned no matching plan documents",
        "no_results_reason": "no_data_for_filters",
        "retrieval_method": None,
        "retrieval_time_ms": None,
        "escalation": {
            "type": True,
            "identification": "Triggered by Guardrails - RAG Error",
            "description": "RAG tool returned an error or empty result",
        },
    }


@pytest.mark.parametrize(
    ("plan_ids", "message"),
    [
        ([], "1-5"),
        (["P1", "P2", "P3", "P4", "P5", "P6"], "1-5"),
        (["8XWE", "8XWE"], "unique"),
        ([" 8XWE"], "invalid exact"),
        (["X" * 129], "invalid exact"),
    ],
)
@patch("shared.rag.httpx.AsyncClient.post")
def test_search_plans_rejects_invalid_selected_ids(
    mock_post, plan_ids, message, mock_agent_context
):
    result = _search("Question", plan_ids, mock_agent_context)
    assert result.outcome == "error"
    assert message in result.error_message
    mock_post.assert_not_called()


@patch("shared.rag.httpx.AsyncClient.post")
def test_search_plans_rejects_unavailable_plan(mock_post, mock_agent_context):
    result = _search("Question", ["NOT-AVAILABLE"], mock_agent_context)
    assert result.outcome == "error"
    assert "not available" in result.error_message
    mock_post.assert_not_called()


@pytest.mark.parametrize(
    ("context_key", "value", "message"),
    [
        ("application_available_plans", "not-json", "not valid JSON"),
        ("application_available_plans", "[]", "1-100"),
        ("user_requested_eff_date", "", "required"),
        ("user_requested_eff_date", "07/01/2026", "YYYY-MM-DD"),
        ("application_market_segment", "Other", "IND or Medicare"),
        ("application_exchange_indicator", "", "required for IND"),
        ("user_language", "language-too-long", "1-10"),
        ("user_state_code", "state-code-too-long", "at most 16"),
    ],
)
@patch("shared.rag.httpx.AsyncClient.post")
def test_search_plans_rejects_invalid_runtime_context(
    mock_post, context_key, value, message, mock_agent_context
):
    mock_agent_context.request_context[context_key] = value
    result = _search("Question", ["8XWE"], mock_agent_context)
    assert result.outcome == "error"
    assert message in result.error_message
    mock_post.assert_not_called()


@patch("shared.rag.httpx.AsyncClient.post")
def test_search_plans_rejects_missing_injected_runtime_context(mock_post):
    result = _search("Question", ["8XWE"], None)
    assert result.outcome == "error"
    assert result.error_message == "agent runtime context is required"
    mock_post.assert_not_called()


@pytest.mark.parametrize("query", ["", " ", "X" * 2001])
@patch("shared.rag.httpx.AsyncClient.post")
def test_search_plans_rejects_invalid_query(mock_post, query, mock_agent_context):
    result = _search(query, ["8XWE"], mock_agent_context)
    assert result.outcome == "error"
    assert "query" in result.error_message
    mock_post.assert_not_called()


@patch("shared.rag.connections.key_value")
def test_search_plans_rejects_missing_or_invalid_connection(mock_key_value, mock_agent_context):
    mock_key_value.return_value = _credentials(RAG_API_BASE_URL="")
    missing = _search("Question", ["8XWE"], mock_agent_context)
    assert missing.outcome == "error"
    assert "not configured" in missing.error_message

    mock_key_value.return_value = _credentials(RAG_API_KEY="")
    missing_key = _search("Question", ["8XWE"], mock_agent_context)
    assert missing_key.outcome == "error"
    assert missing_key.error_message == "RAG_API_KEY is not configured"

    mock_key_value.return_value = _credentials(RAG_IOLS_ENDPOINT="")
    missing_endpoint = _search("Question", ["8XWE"], mock_agent_context)
    assert missing_endpoint.outcome == "error"
    assert missing_endpoint.error_message == "RAG_IOLS_ENDPOINT is not configured"

    mock_key_value.return_value = _credentials(RAG_API_BASE_URL="not-a-url")
    invalid = _search("Question", ["8XWE"], mock_agent_context)
    assert invalid.outcome == "error"
    assert "HTTP origin" in invalid.error_message


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_handles_network_and_non_json_failures(
    mock_key_value, mock_post, mock_agent_context
):
    mock_key_value.return_value = _credentials()
    mock_post.side_effect = httpx.TimeoutException("timed out")
    network = _search("Question", ["8XWE"], mock_agent_context)
    assert network.outcome == "error"
    assert network.error_message == "RAG API request failed"
    _assert_failed_plan_context(network)

    response = Mock(status_code=502)
    response.json.side_effect = ValueError("not JSON")
    mock_post.side_effect = None
    mock_post.return_value = response
    non_json = _search("Question", ["8XWE"], mock_agent_context)
    assert non_json.outcome == "error"
    assert "non-JSON HTTP 502" in non_json.error_message
    _assert_failed_plan_context(non_json)


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_preserves_api_error_detail(mock_key_value, mock_post, mock_agent_context):
    mock_key_value.return_value = _credentials()
    response = Mock(status_code=422)
    response.json.return_value = {"message": "Request schema validation failed"}
    mock_post.return_value = response

    result = _search("Question", ["8XWE"], mock_agent_context)

    assert result.outcome == "error"
    assert result.error_message == "RAG API returned HTTP 422: Request schema validation failed"
    _assert_failed_plan_context(result)


@pytest.mark.parametrize(
    "updates",
    [
        {"results": "not-a-list"},
        {"has_results": False},
        {"collection": "MOLS"},
        {
            "results": [
                {
                    "rank": 1,
                    "score": 0.9,
                    "text": "Untrusted plan result.",
                    "metadata": {"prop_plan_id": "NOT-SELECTED"},
                }
            ]
        },
    ],
)
@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_rejects_response_contract_drift(
    mock_key_value, mock_post, updates, mock_agent_context
):
    mock_agent_context.request_context["request_id"] = "request-1"
    mock_key_value.return_value = _credentials()
    response = _response()
    body = response.json.return_value
    body.update(updates)
    mock_post.return_value = response

    result = _search("Question", ["8XWE"], mock_agent_context)

    assert result.outcome == "error"
    assert "contract validation failed" in result.error_message
    _assert_failed_plan_context(result)


@patch("shared.rag.uuid4")
@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_replaces_unsafe_runtime_request_id(
    mock_key_value, mock_post, mock_uuid4, mock_agent_context
):
    generated = UUID("550e8400-e29b-41d4-a716-446655440000")
    mock_uuid4.return_value = generated
    mock_agent_context.request_context["request_id"] = "unsafe request id"
    mock_key_value.return_value = _credentials()
    mock_post.return_value = _response(request_id=str(generated))

    result = _search("Question", ["8XWE"], mock_agent_context)

    assert result.outcome == "candidates"
    assert "X-Request-ID" not in mock_post.call_args.kwargs["headers"]
    assert "request_id" not in mock_post.call_args.kwargs["json"]


def test_parse_available_plans_rejects_invalid_json_string():
    """Test that parse_available_plans rejects invalid JSON strings."""
    from shared.rag import parse_available_plans

    with pytest.raises(ValueError, match="application_available_plans is not valid JSON"):
        parse_available_plans("{invalid json")


def test_parse_available_plans_rejects_non_object_plan():
    """Test that parse_available_plans rejects non-object plans."""
    from shared.rag import parse_available_plans

    with pytest.raises(ValueError, match="application_available_plans contains a non-object plan"):
        parse_available_plans([{"plan_id": "P1", "plan_name": "Plan 1"}, "not an object"])


def test_parse_available_plans_rejects_invalid_plan_id():
    """Test that parse_available_plans rejects invalid plan IDs."""
    from shared.rag import parse_available_plans

    with pytest.raises(ValueError, match="application_available_plans contains an invalid plan_id"):
        parse_available_plans([{"plan_id": "", "plan_name": "Plan 1"}])


def test_parse_available_plans_rejects_plan_id_exceeding_max_length():
    """Test that parse_available_plans rejects plan IDs longer than 128 characters."""
    from shared.rag import parse_available_plans

    with pytest.raises(ValueError, match="application_available_plans contains an invalid plan_id"):
        parse_available_plans([{"plan_id": "x" * 129, "plan_name": "Plan 1"}])


def test_parse_available_plans_rejects_invalid_plan_name():
    """Test that parse_available_plans rejects invalid plan names."""
    from shared.rag import parse_available_plans

    with pytest.raises(
        ValueError, match="application_available_plans contains an invalid plan_name"
    ):
        parse_available_plans([{"plan_id": "P1", "plan_name": ""}])


def test_build_endpoint_url_rejects_path_with_scheme():
    """Test that build_endpoint_url rejects paths containing ://."""
    from shared.rag import build_endpoint_url

    with pytest.raises(ValueError, match="RAG endpoint must be an absolute URL path"):
        build_endpoint_url("https://rag.example.test", "https://evil.com/path")


@patch("shared.rag.httpx.AsyncClient.post")
def test_search_plans_rejects_invalid_business_intent(mock_post, mock_agent_context):
    """Test that search_plans rejects invalid business_intent."""
    result = _search(
        "Question",
        ["8XWE"],
        mock_agent_context,
        business_intent="invalid_intent",
    )

    assert result.outcome == "error"
    assert "business_intent must be specific_plan or broad_plans" in result.error_message
    mock_post.assert_not_called()


@patch("shared.rag.httpx.AsyncClient.post")
def test_search_plans_rejects_invalid_search_object(mock_post, mock_agent_context):
    """Test that search_plans rejects invalid search objects."""
    result = _search(
        "Question",
        ["8XWE"],
        mock_agent_context,
        searches=["not a valid search object"],
    )

    assert result.outcome == "error"
    assert "searches contains an invalid retrieval search" in result.error_message
    mock_post.assert_not_called()


@patch("shared.rag.httpx.AsyncClient.post")
def test_search_plans_rejects_search_validation_error(mock_post, mock_agent_context):
    """Test that search_plans rejects searches that fail pydantic validation."""
    result = _search(
        "Question",
        ["8XWE"],
        mock_agent_context,
        searches=[{"semantic_query": "x" * 1001}],  # Exceeds max_length
    )

    assert result.outcome == "error"
    assert "searches contains an invalid retrieval search" in result.error_message
    mock_post.assert_not_called()


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_handles_non_dict_json_response(mock_key_value, mock_post, mock_agent_context):
    """Test that search_plans handles non-dict JSON responses."""
    mock_key_value.return_value = _credentials()
    response = Mock(status_code=200)
    response.json.return_value = ["not", "a", "dict"]
    mock_post.return_value = response

    result = _search("Question", ["8XWE"], mock_agent_context)

    assert result.outcome == "error"
    assert "RAG API returned an invalid response object" in result.error_message


@patch("shared.rag.connections.key_value")
def test_parse_available_plans_rejects_duplicate_plan_ids(mock_key_value, mock_agent_context):
    """Test that parse_available_plans rejects duplicate plan IDs."""
    mock_agent_context.request_context["application_available_plans"] = json.dumps(
        [
            {"plan_id": "123", "plan_name": "Plan A"},
            {"plan_id": "123", "plan_name": "Plan B"},
        ]
    )
    mock_key_value.return_value = _credentials()

    result = _search("Question", ["123"], mock_agent_context)
    assert result.outcome == "error"
    assert "duplicate plan IDs" in result.error_message


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_rejects_non_dict_result_in_results_list(
    mock_key_value, mock_post, mock_agent_context
):
    """Test that search_plans rejects non-dict results in results list."""
    mock_agent_context.request_context["request_id"] = "request-1"
    mock_key_value.return_value = _credentials()
    response = _response(
        request_id="request-1",
        results=[
            {
                "rank": 1,
                "score": 0.9,
                "text": "Valid result",
                "metadata": {"document_id": "doc-1", "prop_plan_id": "8XWE"},
            },
            "not a dict",
        ],
    )
    mock_post.return_value = response

    result = _search("Question", ["8XWE"], mock_agent_context)
    assert result.outcome == "error"
    assert "result is not an object" in result.error_message


def test_search_plans_rejects_empty_searches_list():
    """Test that search_plans rejects empty searches list."""
    result = asyncio.run(
        search_plans(
            query="Question",
            searches=[],
            plan_ids=["8XWE"],
            business_intent="specific_plan",
            context=Mock(
                request_context={
                    "application_available_plans": json.dumps(
                        [{"plan_id": "8XWE", "plan_name": "Plan A"}]
                    )
                }
            ),
        )
    )
    assert result.outcome == "error"
    assert "searches must contain 1-4" in result.error_message


def test_search_plans_rejects_more_than_four_searches():
    """Test that search_plans rejects more than 4 searches."""
    result = asyncio.run(
        search_plans(
            query="Question",
            searches=[
                RetrievalSearch(semantic_query="q1"),
                RetrievalSearch(semantic_query="q2"),
                RetrievalSearch(semantic_query="q3"),
                RetrievalSearch(semantic_query="q4"),
                RetrievalSearch(semantic_query="q5"),
            ],
            plan_ids=["8XWE"],
            business_intent="specific_plan",
            context=Mock(
                request_context={
                    "application_available_plans": json.dumps(
                        [{"plan_id": "8XWE", "plan_name": "Plan A"}]
                    )
                }
            ),
        )
    )
    assert result.outcome == "error"
    assert "searches must contain 1-4" in result.error_message


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_handles_boolean_retrieval_time(mock_key_value, mock_post, mock_agent_context):
    """Test that search_plans handles boolean retrieval time by returning None."""
    mock_agent_context.request_context["request_id"] = "request-1"
    mock_key_value.return_value = _credentials()
    response = _response(request_id="request-1")
    response_data = response.json.return_value
    response_data["metrics"] = {"total_time_ms": True}
    mock_post.return_value = response

    result = _search("Question", ["8XWE"], mock_agent_context)

    # Boolean values should be rejected and retrieval_time_ms should be None
    assert result.retrieval_time_ms is None
    # Should still succeed despite invalid metrics
    assert result.outcome in ("candidates", "insufficient")


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_handles_positive_retrieval_time(
    mock_key_value, mock_post, mock_agent_context
):
    """Test that search_plans extracts positive retrieval time correctly."""
    mock_agent_context.request_context["request_id"] = "request-1"
    mock_key_value.return_value = _credentials()
    response = _response(request_id="request-1")
    # Add metrics with valid retrieval time to the response
    response_data = response.json.return_value
    response_data["metrics"] = {"total_time_ms": 150}
    mock_post.return_value = response

    result = _search("Question", ["8XWE"], mock_agent_context)

    # Should successfully extract the retrieval time from a valid response
    assert result.retrieval_time_ms == 150


@patch("shared.rag.httpx.AsyncClient.post")
@patch("shared.rag.connections.key_value")
def test_search_plans_handles_missing_state_code(mock_key_value, mock_post):
    """Test that search_plans handles missing state_code correctly."""
    # Create a context without user_state_code
    context = Mock()
    context.request_context = {
        "request_id": "request-1",
        "application_available_plans": json.dumps(
            [{"plan_id": "8XWE", "plan_name": "Anthem Silver 70"}]
        ),
        "user_requested_eff_date": "2026-01-01",
        "application_market_segment": "IND",
        "application_exchange_indicator": "Off",
        # Note: user_state_code is intentionally missing
    }
    context.get = lambda key, default=None: context.request_context.get(key, default)

    mock_key_value.return_value = _credentials()
    response = _response(request_id="request-1")
    mock_post.return_value = response

    result = _search("Question", ["8XWE"], context)

    # Should succeed without state_code in payload
    assert result.outcome in ("candidates", "insufficient")
    # Verify state_code was not included in the request
    kwargs = mock_post.call_args.kwargs
    assert "state_code" not in kwargs["json"]
