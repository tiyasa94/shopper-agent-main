"""Fast tests for behavioral evaluation and candidate comparison."""

import argparse
import csv
import json
import threading
import time

import requests
import yaml
from evaluation.clients.shopper_api import (
    PUBLIC_API_RESPONSE_KEY,
    PUBLIC_API_TRANSPORT_ATTEMPTS_KEY,
    ShopperApiClient,
    context_to_shopper_api,
)
from evaluation.clients.wxo import AgentRunResult, ToolCall, ToolResponse

from evaluation import behavioral as agent_comparison


def _corpus(tmp_path):
    path = tmp_path / "questions.yaml"
    corpus = {
        "schema_version": 1,
        "contexts": {
            "individual": {
                "application_market_segment": "IND",
                "application_available_plans": [{"plan_id": "P1", "plan_name": "Plan One"}],
                "application_recommended_plans": [],
                "prospect_type": "prospect",
            },
            "medicare": {
                "application_market_segment": "Medicare",
                "application_available_plans": [{"plan_id": "M1", "plan_name": "Medicare One"}],
                "application_recommended_plans": [],
                "prospect_type": "member",
            },
        },
        "conversations": [
            {
                "id": "individual_1",
                "market": "Individual",
                "context": "individual",
                "evaluation_status": "eligible",
                "turns": [
                    {
                        "id": "individual_1_turn_1",
                        "question": "What is a copay?",
                        "reference_answer": "A fixed amount.",
                        "source_intent": "generic_info",
                        "source_rag_expected": True,
                    },
                    {
                        "id": "individual_1_turn_2",
                        "question": "What about Plan One?",
                        "reference_answer": "Plan detail.",
                        "source_intent": "specific_plan",
                        "source_rag_expected": True,
                    },
                ],
            },
            {
                "id": "medicare_1",
                "market": "Medicare",
                "context": "medicare",
                "evaluation_status": "eligible",
                "turns": [
                    {
                        "id": "medicare_1_turn_1",
                        "question": "Hello",
                        "reference_answer": "Hello.",
                        "source_intent": "greeting",
                        "source_rag_expected": False,
                    }
                ],
            },
            {
                "id": "excluded_1",
                "market": "Individual",
                "context": "individual",
                "evaluation_status": "excluded_prompt_overlap",
                "turns": [],
            },
        ],
    }
    path.write_text(yaml.safe_dump(corpus, sort_keys=False))
    return path


class _FakeClient:
    instances = []
    response_prefix = "answer"

    def __init__(
        self,
        base_url,
        agent_name,
        timeout,
        *,
        allow_remote=False,
        api_key=None,
        agent_id=None,
    ):
        self.calls = []
        self.configuration = {
            "base_url": base_url,
            "agent_name": agent_name,
            "timeout": timeout,
            "allow_remote": allow_remote,
            "api_key": api_key,
            "agent_id": agent_id,
        }
        self.instances.append(self)

    def run(self, case_id, query, context, *, session_id=None):
        self.calls.append((case_id, query, context, session_id))
        response = f"{self.response_prefix}: {query}"
        return AgentRunResult(
            submitted_run_id=f"submitted-{case_id}",
            run_id=f"run-{case_id}",
            duration_seconds=1.0,
            response_text=response,
            request_context=context,
            context={
                "_turn_result": json.dumps(
                    {
                        "schema_version": 1,
                        "route": "plan_turn",
                        "business_intent": "specific_plan",
                        "escalation": {"type": False},
                    }
                )
            },
            tool_calls=[
                ToolCall(
                    "rag",
                    "search_plans",
                    {
                        "queries": [query],
                        "plan_ids": ["P1"],
                    },
                ),
            ],
            tool_responses=[
                ToolResponse(
                    "rag",
                    "search_plans",
                    {
                        "outcome": "candidates",
                        "business_intent": "specific_plan",
                        "coverage_complete": True,
                        "results": [],
                        "audit_completed": True,
                        "escalation": {
                            "type": False,
                            "identification": "rag_response",
                            "description": "ok",
                        },
                    },
                ),
            ],
        )


def _args(corpus, output, candidate):
    return argparse.Namespace(
        candidate=candidate,
        source_ref=candidate,
        source_revision=f"sha-{candidate}",
        source_fingerprint="clean",
        rag_base_url=f"https://{candidate}.example.test",
        output=output,
        corpus=corpus,
        suite="full",
        status=["eligible"],
        market=[],
        conversation_id=[],
        limit_conversations=None,
        trials=1,
        warmup=False,
        resume=False,
        concurrency=1,
        url="http://localhost:4321",
        agent="Agent",
        allow_remote=False,
        agent_id=None,
        timeout=5,
        turn_delay=0.0,
        transport="wxo",
        shopper_api_key_env="SHOPPER_API_KEY",
    )


def test_summary_reports_interpolated_p95_latency() -> None:
    records = [
        {
            "status": "completed",
            "result": {"duration_seconds": duration, "response_text": "ok"},
            "observations": {},
        }
        for duration in (1.0, 2.0, 3.0)
    ]

    assert agent_comparison.summarize(records)["p95_duration_seconds"] == 2.9


def test_summary_separates_business_intent_alignment_observability_and_applicability() -> None:
    records = [
        {
            "status": "completed",
            "expected_route": "general_search",
            "source_intent": "generic_info",
            "result": {"duration_seconds": 1.0, "response_text": "ok"},
            "observations": {"business_intent": "generic_info"},
        },
        {
            "status": "completed",
            "expected_route": "guardrail",
            "source_intent": "broad_plans",
            "result": {"duration_seconds": 1.0, "response_text": "ok"},
            "observations": {"business_intent": "generic_info"},
        },
        {
            "status": "completed",
            "expected_route": "clarify_plan",
            "source_intent": "specific_plan",
            "result": {"duration_seconds": 1.0, "response_text": "Which plan?"},
            "observations": {"business_intent": ""},
        },
        {
            "status": "completed",
            "expected_route": "plan_search",
            "source_intent": "specific_plan",
            "result": {"duration_seconds": 1.0, "response_text": "No evidence."},
            "observations": {"business_intent": ""},
        },
    ]

    summary = agent_comparison.summarize(records)

    assert summary["business_intent_aligned"] == 1
    assert summary["business_intent_mismatched"] == 1
    assert summary["business_intent_not_applicable"] == 1
    assert summary["business_intent_missing"] == 1


def test_summary_reclassifies_historical_completed_generic_failure() -> None:
    records = [
        {
            "status": "completed",
            "result": {
                "duration_seconds": 1.0,
                "response_text": "I’m unable to complete that request right now. Please try again.",
            },
            "observations": {"transport_attempts": 2},
        }
    ]

    summary = agent_comparison.summarize(records)

    assert summary["completed"] == 0
    assert summary["execution_failures"] == 1
    assert summary["transport_retries"] == 1


def test_warmup_is_not_recorded_or_counted(monkeypatch, tmp_path) -> None:
    _FakeClient.instances.clear()
    monkeypatch.setattr(agent_comparison, "WxoClient", _FakeClient)
    corpus = _corpus(tmp_path)
    output = tmp_path / "candidate"
    args = _args(corpus, output, "refactor")
    args.warmup = True

    assert agent_comparison.run_candidate(args) == 0

    assert any(
        client.calls and client.calls[0][0] == "evaluation-warmup"
        for client in _FakeClient.instances
    )
    records = agent_comparison._load_records(output / "results.jsonl")
    assert len(records) == 3
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["warmup"] is True
    assert manifest["summary"]["turns"] == 3


def test_context_serialization_and_pilot_selection(tmp_path):
    corpus = agent_comparison.load_corpus(_corpus(tmp_path))
    context = agent_comparison.context_to_wxo(
        corpus["contexts"]["individual"], [{"role": "user", "content": "Prior"}]
    )
    selected = agent_comparison.select_conversations(
        corpus,
        statuses={"eligible"},
        markets=set(),
        conversation_ids=set(),
        suite="pilot",
        limit=2,
    )

    assert json.loads(context["application_available_plans"])[0]["plan_id"] == "P1"
    assert json.loads(context["previous_queries"])[0]["content"] == "Prior"
    assert {item["market"] for item in selected} == {"Individual", "Medicare"}


def test_public_context_maps_curated_fields_without_private_wxo_shapes(tmp_path):
    corpus = agent_comparison.load_corpus(_corpus(tmp_path))
    source = corpus["contexts"]["individual"] | {
        "user_current_plan": {"plan_id": "P1", "plan_name": "Plan One"},
        "user_state_code": "CA",
    }

    context = context_to_shopper_api(source)

    assert context["user_context"]["current_plan"] == {
        "plan_id": "P1",
        "plan_name": "Plan One",
    }
    assert context["user_context"]["state_code"] == "CA"
    assert context["application_context"]["available_plans"][0]["plan_id"] == "P1"
    assert "previous_queries" not in json.dumps(context)


def test_normalized_context_derives_minimal_quote_inputs(tmp_path):
    corpus = agent_comparison.load_corpus(_corpus(tmp_path))
    normalized = {
        "user_brand": "WLP",
        "user_zip_code": "78701",
        "user_county_code": "48453",
        "user_county_name": "TRAVIS",
        "user_state_code": "TX",
        "user_requested_eff_date": "2026-01-01",
        "user_applicants": [
            {
                "applicant_type": "PRIMARY",
                "gender": "FEMALE",
                "date_of_birth": "1985-01-01",
                "is_tobacco_user": "NO",
                "applicant_id": "applicant-1",
            }
        ],
    }
    source = corpus["contexts"]["individual"] | normalized

    public = context_to_shopper_api(source)
    runtime = agent_comparison.context_to_wxo(source, [])
    runtime_quote = runtime["application_plan_quote_inputs"]

    assert "plan_quote_context" not in public
    assert public["user_context"]["applicants"][0]["applicant_id"] == "applicant-1"
    assert runtime_quote["zip_code"] == "78701"
    assert runtime_quote["applicants"][0]["applicant_type"] == "PRIMARY"
    assert runtime_quote["applicants"][0]["is_tobacco_user"] == "NO"
    assert "brand" not in runtime_quote
    assert "state_code" not in runtime_quote
    assert "requested_eff_date" not in runtime_quote
    assert "market_segment" not in runtime_quote


def test_public_observations_preserve_rag_and_audit_metadata() -> None:
    payload = {
        "response": {"text": "Grounded answer"},
        "user_query": {"business_intent": "specific_plan"},
        "rag_context": {
            "contexts": [
                {
                    "content": "Official evidence",
                    "reference": {"source": "wxo_tool", "document_id": "doc"},
                    "relevance_score": 0.9,
                }
            ],
            "total_retrieved": 1,
        },
        "metadata": {
            "plans_found": ["Plan One"],
            "plans_searched_for": ["Plan One"],
            "escalation": {"type": False, "identification": "rag_response"},
        },
    }
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run",
        duration_seconds=1,
        response_text="Grounded answer",
        request_context={},
        context={PUBLIC_API_RESPONSE_KEY: payload},
        tool_calls=[],
        tool_responses=[],
    )

    observed = agent_comparison.observations(result, "What does the plan cover?")

    assert observed["route"] == ""
    assert observed["rag_called"] is True
    assert observed["rag_status"] == "success"
    assert observed["business_intent"] == "specific_plan"
    assert observed["plans_found"] == ["Plan One"]
    assert observed["plans_searched_for"] == ["Plan One"]
    assert observed["rag_evidence_count"] == 1
    assert observed["audit_source"] == "public_metadata"
    assert observed["plan_details_observable"] is False
    assert observed["plan_details_called"] is None


def test_direct_observations_preserve_structured_plan_tool_inputs_and_results() -> None:
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run",
        duration_seconds=1,
        response_text="Summary answer:\nThe premium is $68.\n\nDetails:\n- Current quote.",
        request_context={},
        context={"_turn_result": json.dumps({"route": "search_turn"})},
        tool_calls=[
            ToolCall(
                "details",
                "get_plan_details",
                {
                    "plan_ids": ["H4036-008-000"],
                    "detail_types": ["premium", "medical_deductible"],
                },
            )
        ],
        tool_responses=[
            ToolResponse(
                "details",
                "get_plan_details",
                {
                    "outcome": "complete",
                    "business_intent": "specific_plan",
                    "coverage_complete": True,
                    "missing_plans": [],
                    "fallback_detail_types": [],
                    "plans": [
                        {
                            "plan_id": "H4036-008-000",
                            "plan_name": "Example Medicare Advantage PPO",
                            "details": [{"detail_type": "premium", "premium": {"total": 68}}],
                        }
                    ],
                    "audit_completed": True,
                    "escalation": {
                        "type": False,
                        "identification": "structured_plan_response",
                    },
                },
            )
        ],
    )

    observed = agent_comparison.observations(result, "What is the premium?")

    assert observed["rag_called"] is False
    assert observed["plan_details_called"] is True
    assert observed["plan_details_observable"] is True
    assert observed["plan_details_call_count"] == 1
    assert observed["plan_details_plan_ids"] == ["H4036-008-000"]
    assert observed["plan_detail_types"] == ["premium", "medical_deductible"]
    assert observed["plan_details_outcome"] == "complete"
    assert observed["plan_details_coverage_complete"] is True
    assert observed["plan_details_excerpt"][0]["plan_id"] == "H4036-008-000"
    assert observed["audit_source"] == "structured_plan_details"


def test_public_structured_plan_metadata_is_not_counted_as_rag() -> None:
    payload = {
        "response": {"text": "The monthly premium is $68."},
        "user_query": {"business_intent": "specific_plan"},
        "rag_context": {"contexts": [], "total_retrieved": 0},
        "metadata": {
            "plans_found": ["Example Medicare Advantage PPO"],
            "plans_searched_for": ["Example Medicare Advantage PPO"],
            "plan_details": {
                "called": True,
                "call_count": 1,
                "plan_ids": ["H4036-008-000"],
                "detail_types": ["premium", "specialist_visit"],
                "outcome": "complete",
                "coverage_complete": True,
                "missing_plans": [],
                "fallback_detail_types": [],
            },
            "escalation": {
                "type": False,
                "identification": "structured_plan_response",
            },
        },
    }
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run",
        duration_seconds=1,
        response_text="The monthly premium is $68.",
        request_context={},
        context={PUBLIC_API_RESPONSE_KEY: payload},
        tool_calls=[],
        tool_responses=[],
    )

    observed = agent_comparison.observations(result, "What is the monthly premium?")

    assert observed["rag_called"] is False
    assert observed["rag_call_count"] == 0
    assert observed["plans_searched_for"] == ["Example Medicare Advantage PPO"]
    assert observed["plan_details_called"] is True
    assert observed["plan_details_observable"] is True
    assert observed["plan_details_call_count"] == 1
    assert observed["plan_details_plan_ids"] == ["H4036-008-000"]
    assert observed["plan_detail_types"] == ["premium", "specialist_visit"]
    assert observed["plan_details_outcome"] == "complete"
    assert observed["plan_details_coverage_complete"] is True
    assert observed["audit_identification"] == "structured_plan_response"


def test_public_structured_plan_metadata_with_document_context_is_counted_as_rag() -> None:
    payload = {
        "response": {"text": "The premium is $68 and the X-ray cost is 40%."},
        "user_query": {"business_intent": "specific_plan"},
        "rag_context": {
            "contexts": [
                {
                    "content": "X-rays cost 40% after the deductible.",
                    "reference": {"source": "wxo_tool"},
                }
            ],
            "total_retrieved": 1,
        },
        "metadata": {
            "plans_searched_for": ["Plan One"],
            "plan_details": {
                "called": True,
                "call_count": 1,
                "plan_ids": ["P1"],
                "detail_types": ["premium"],
                "outcome": "complete",
                "coverage_complete": True,
                "missing_plans": [],
                "fallback_detail_types": [],
            },
            "escalation": {
                "type": False,
                "identification": "structured_plan_response",
            },
        },
    }
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run",
        duration_seconds=1,
        response_text="The premium is $68 and the X-ray cost is 40%.",
        request_context={},
        context={PUBLIC_API_RESPONSE_KEY: payload},
        tool_calls=[],
        tool_responses=[],
    )

    observed = agent_comparison.observations(result, "What are the premium and X-ray cost?")

    assert observed["rag_called"] is True
    assert observed["rag_evidence_count"] == 1
    assert observed["plan_details_called"] is True
    assert observed["plan_details_outcome"] == "complete"


def test_public_plan_intent_without_rag_evidence_is_not_counted_as_a_call() -> None:
    payload = {
        "response": {"text": "Which plan would you like details for?"},
        "user_query": {"business_intent": "specific_plan"},
        "rag_context": {"contexts": [], "total_retrieved": 0},
        "metadata": {
            "plans_found": [],
            "plans_searched_for": [],
            "escalation": {"type": False},
        },
    }
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run",
        duration_seconds=1,
        response_text="Which plan would you like details for?",
        request_context={},
        context={PUBLIC_API_RESPONSE_KEY: payload},
        tool_calls=[],
        tool_responses=[],
    )

    observed = agent_comparison.observations(result, "What would I pay for a specialist?")

    assert observed["business_intent"] == "specific_plan"
    assert observed["rag_called"] is False
    assert observed["rag_call_count"] == 0


def test_public_observations_recognize_canonical_rag_error_without_evidence() -> None:
    payload = {
        "response": {"text": "The plan information could not be retrieved."},
        "user_query": {"business_intent": "specific_plan"},
        "rag_context": {"contexts": [], "total_retrieved": 0},
        "metadata": {
            "plans_found": [],
            "plans_searched_for": [],
            "escalation": {
                "type": True,
                "identification": "Triggered by Guardrails - RAG Error",
            },
        },
    }
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run",
        duration_seconds=1,
        response_text="The plan information could not be retrieved.",
        request_context={},
        context={PUBLIC_API_RESPONSE_KEY: payload},
        tool_calls=[],
        tool_responses=[],
    )

    observed = agent_comparison.observations(result, "What does my plan cover?")

    assert observed["rag_called"] is True
    assert observed["rag_call_count"] == 1
    assert observed["rag_status"] == "error"


def test_observations_recognize_shopper_agent_search_tool() -> None:
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run",
        duration_seconds=1,
        response_text="The maximum is $3,000.",
        request_context={},
        context={
            "_turn_result": json.dumps(
                {
                    "route": "plan_turn",
                    "business_intent": "specific_plan",
                    "audit_completed": False,
                }
            )
        },
        tool_calls=[
            ToolCall(
                "search",
                "search_plans",
                {
                    "queries": ["What is the annual out-of-pocket maximum?"],
                    "plan_ids": ["H4161-009-000"],
                },
            )
        ],
        tool_responses=[
            ToolResponse(
                "search",
                "search_plans",
                {
                    "business_intent": None,
                    "coverage_complete": True,
                    "plans_found": ["Anthem Prime (HMO-POS)"],
                    "plans_searched_for": ["Anthem Prime (HMO-POS)"],
                    "results": [{"plan_name": "Anthem Prime (HMO-POS)", "text": "$3,000"}],
                    "audit_completed": True,
                    "escalation": {
                        "type": False,
                        "identification": "rag_response",
                        "description": "Normal RAG response logging",
                    },
                },
            )
        ],
    )

    observed = agent_comparison.observations(result, "What is the out-of-pocket maximum?")

    assert observed["rag_called"] is True
    assert observed["rag_call_count"] == 1
    assert observed["rag_tool"] == "search_plans"
    assert observed["rag_evidence_count"] == 1
    assert observed["plans_found"] == ["Anthem Prime (HMO-POS)"]
    assert observed["business_intent"] == "specific_plan"
    assert observed["search_query"] == "What is the annual out-of-pocket maximum?"
    assert observed["audit_called"] is True
    assert observed["audit_source"] == "search"
    assert observed["audit_identification"] == "rag_response"


def test_observations_recognize_guardrail_audit_without_a_tool_call() -> None:
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run",
        duration_seconds=1,
        response_text="Please call a licensed agent.",
        request_context={},
        context={
            "_turn_result": json.dumps(
                {
                    "route": "terminal",
                    "business_intent": "generic_info",
                    "audit_completed": True,
                    "escalation": {
                        "type": True,
                        "identification": "live_agent_request",
                        "description": "User explicitly requested a live agent (G03)",
                    },
                }
            )
        },
        tool_calls=[],
        tool_responses=[],
    )

    observed = agent_comparison.observations(result, "I want a representative")

    assert observed["route"] == "terminal"
    assert observed["business_intent"] == "generic_info"
    assert observed["audit_called"] is True
    assert observed["audit_source"] == "guardrail"
    assert observed["audit_identification"] == "live_agent_request"


def test_observations_treat_general_search_as_retrieval_and_audit() -> None:
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run",
        duration_seconds=1,
        response_text="A deductible is what you pay before coverage begins.",
        request_context={},
        context={},
        tool_calls=[
            ToolCall(
                "general-search",
                "search_general_documents",
                {"queries": ["health insurance deductible definition"]},
            )
        ],
        tool_responses=[
            ToolResponse(
                "general-search",
                "search_general_documents",
                {
                    "outcome": "candidates",
                    "business_intent": "generic_info",
                    "results": [{"text": "A deductible is an amount paid first."}],
                    "audit_completed": True,
                    "escalation": {
                        "type": False,
                        "identification": "rag_response",
                        "description": "Normal RAG response logging",
                    },
                },
            )
        ],
    )

    observed = agent_comparison.observations(result, "What is a deductible?")

    assert observed["rag_called"] is True
    assert observed["rag_tool"] == "search_general_documents"
    assert observed["search_query"] == "health insurance deductible definition"
    assert observed["audit_called"] is True
    assert observed["audit_source"] == "search"


def test_public_client_rotates_session_when_subject_type_changes() -> None:
    class Response:
        def __init__(self, token: str):
            self.token = token

        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": self.token}

    class Session:
        def __init__(self):
            self.logins = []

        def post(self, url, *, json, timeout):
            del url, timeout
            self.logins.append(json["subject_type"])
            return Response(f"token-{len(self.logins)}")

    client = ShopperApiClient.__new__(ShopperApiClient)
    client.base_url = "http://127.0.0.1:8082"
    client.timeout = 30
    client.session = Session()
    client._tokens = {}

    assert client._token("conversation", "prospect") == "token-1"
    assert client._token("conversation", "prospect") == "token-1"
    assert client._token("conversation", "member") == "token-2"
    assert client.session.logins == ["anonymous", "authenticated_member"]


def test_public_client_retries_transient_query_response(monkeypatch) -> None:
    class Session:
        def __init__(self):
            self.calls = 0

        def post(self, url, **kwargs):
            del url, kwargs
            self.calls += 1
            response = requests.Response()
            response.status_code = 503 if self.calls == 1 else 200
            response.close = lambda: None
            response._content = json.dumps(
                {
                    "success": True,
                    "response": {"text": "Recovered answer"},
                    "call_info": {"run_id": "run-1"},
                }
            ).encode()
            return response

    monkeypatch.setattr("evaluation.clients.shopper_api.time.sleep", lambda _: None)
    client = ShopperApiClient.__new__(ShopperApiClient)
    client.base_url = "http://127.0.0.1:8082"
    client.timeout = 30
    client.session = Session()
    client._tokens = {("conversation", "anonymous"): "token"}

    result = client.run(
        "case", "Question", {"prospect_type": "prospect"}, session_id="conversation"
    )

    assert client.session.calls == 2
    assert result.response_text == "Recovered answer"
    assert result.context[PUBLIC_API_TRANSPORT_ATTEMPTS_KEY] == 2


def test_public_client_does_not_retry_http_200_execution_failure() -> None:
    failure_text = "I’m unable to complete that request right now. Please try again."

    class Session:
        def __init__(self):
            self.calls = 0

        def post(self, url, **kwargs):
            del url, kwargs
            self.calls += 1
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(
                {
                    "success": True,
                    "response": {"text": failure_text},
                    "call_info": {"run_id": "failed-run"},
                    "metadata": {"agents_invoked": ["Shopper"]},
                }
            ).encode()
            return response

    client = ShopperApiClient.__new__(ShopperApiClient)
    client.base_url = "http://127.0.0.1:8082"
    client.timeout = 30
    client.session = Session()
    client._tokens = {("conversation", "anonymous"): "token"}

    result = client.run(
        "case", "Question", {"prospect_type": "prospect"}, session_id="conversation"
    )

    assert client.session.calls == 1
    assert result.run_id == "failed-run"
    assert result.response_text == failure_text
    assert result.context[PUBLIC_API_TRANSPORT_ATTEMPTS_KEY] == 1
    assert result.context[PUBLIC_API_RESPONSE_KEY]["metadata"]["agents_invoked"] == ["Shopper"]


def test_remote_candidate_forwards_explicit_credentials_and_agent_id(tmp_path, monkeypatch):
    _FakeClient.instances.clear()
    monkeypatch.setattr(agent_comparison, "WxoClient", _FakeClient)
    monkeypatch.setenv("WXO_API_KEY", "test-api-key")
    args = _args(_corpus(tmp_path), tmp_path / "remote", "remote")
    args.url = "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/test"
    args.agent = "Elevance_Health_Shopper_Portal"
    args.agent_id = "agent-123"
    args.allow_remote = True

    assert agent_comparison.run_candidate(args) == 0
    assert _FakeClient.instances[-1].configuration == {
        "base_url": args.url,
        "agent_name": args.agent,
        "timeout": 5,
        "allow_remote": True,
        "api_key": "test-api-key",
        "agent_id": "agent-123",
    }


def test_public_api_comparison_caches_stage_and_local_candidates():
    script = (agent_comparison.ROOT / "scripts/run-shopper-api-comparison.sh").read_text()

    assert "validate_stage_cache" in script
    assert "validate_local_cache" in script
    assert "LOCAL_SHOPPER_API_FINGERPRINT" in script
    assert "LOCAL_RAG_API_FINGERPRINT" in script
    assert "USE_STAGE_CACHE" in script
    assert "USE_LOCAL_CACHE" in script


def test_exact_local_paired_comparison_pins_both_repository_snapshots():
    script = (agent_comparison.ROOT / "scripts/run-local-paired-comparison.sh").read_text()

    assert "71b57ad98cd8ba59ac07c40db7256ad25faf16a6" in script
    assert "1e60d8a553f3a508c74563b86ba4b5db72d8e624" in script
    assert "http://127.0.0.1:8082" in script
    assert "http://host.lima.internal:8081" in script
    assert "local-dev-milvus" in script
    assert "--warmup" in script
    assert 'TURN_DELAY_SECONDS="${LOCAL_EVALUATION_TURN_DELAY_SECONDS:-0.5}"' in script
    assert '--turn-delay "${TURN_DELAY_SECONDS}"' in script
    assert '--turn-delay-seconds "${TURN_DELAY_SECONDS}"' in script
    assert "validate_cache" in script
    assert 'git clone --quiet --no-hardlinks --no-checkout "${ROOT_DIR}"' in script
    assert "archive --format=tar" not in script
    assert "--allow-remote" not in script
    assert 'DEPLOY_RUNTIME_SECRETS_FILE="${AGENT_RUNTIME_SECRETS}"' in script
    assert 'DEPLOY_RUNTIME_SECRETS_FILE="${ROOT_DIR}/.env.local"' not in script


def test_candidate_run_preserves_conversation_history_and_checkpoints(monkeypatch, tmp_path):
    _FakeClient.instances = []
    _FakeClient.response_prefix = "candidate"
    monkeypatch.setattr(agent_comparison, "WxoClient", _FakeClient)
    corpus = _corpus(tmp_path)
    output = tmp_path / "candidate"

    assert agent_comparison.run_candidate(_args(corpus, output, "refactor")) == 0

    client = _FakeClient.instances[-1]
    first, second = client.calls[:2]
    assert first[3] == second[3]
    assert json.loads(first[2]["previous_queries"]) == []
    second_history = json.loads(second[2]["previous_queries"])
    assert second_history == [
        {"role": "user", "content": "What is a copay?"},
        {"role": "assistant", "content": "candidate: What is a copay?"},
    ]
    records = agent_comparison._load_records(output / "results.jsonl")
    assert len(records) == 3
    assert all(record["status"] == "completed" for record in records.values())
    individual_record = next(
        record for record in records.values() if record["context_name"] == "individual"
    )
    assert individual_record["current_plan"] == ""
    assert individual_record["available_plans"] == [{"plan_id": "P1", "plan_name": "Plan One"}]
    assert json.loads((output / "manifest.json").read_text())["summary"]["completed"] == 3

    reusable, reason = agent_comparison.candidate_cache_is_reusable(
        output,
        source_revision="sha-refactor",
        rag_base_url="https://refactor.example.test",
        suite="full",
        corpus=corpus,
    )
    assert reusable is True
    assert reason == "cache is complete and compatible"

    reusable, reason = agent_comparison.candidate_cache_is_reusable(
        output,
        source_revision="sha-refactor",
        source_fingerprint="different-tree",
        rag_base_url="https://refactor.example.test",
        suite="full",
        corpus=corpus,
    )
    assert reusable is False
    assert reason == "cached source_fingerprint does not match"


def test_candidate_run_parallelizes_conversations_but_not_turns(monkeypatch, tmp_path):
    class ConcurrentClient(_FakeClient):
        active = 0
        max_active = 0
        active_sessions = set()
        tracker_lock = threading.Lock()

        def run(self, case_id, query, context, *, session_id=None):
            cls = type(self)
            with cls.tracker_lock:
                assert session_id not in cls.active_sessions
                cls.active_sessions.add(session_id)
                cls.active += 1
                cls.max_active = max(cls.max_active, cls.active)
            try:
                time.sleep(0.02)
                return super().run(case_id, query, context, session_id=session_id)
            finally:
                with cls.tracker_lock:
                    cls.active_sessions.remove(session_id)
                    cls.active -= 1

    _FakeClient.instances = []
    monkeypatch.setattr(agent_comparison, "WxoClient", ConcurrentClient)
    corpus = _corpus(tmp_path)
    output = tmp_path / "candidate"
    args = _args(corpus, output, "refactor")
    args.concurrency = 2

    assert agent_comparison.run_candidate(args) == 0

    assert ConcurrentClient.max_active == 2
    records = agent_comparison._load_records(output / "results.jsonl")
    assert len(records) == 3
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["concurrency"] == 2


def test_execution_failure_is_preserved_and_resume_replays_full_conversation(monkeypatch, tmp_path):
    failure_text = "I’m unable to complete that request right now. Please try again."

    class ExecutionFailureClient(_FakeClient):
        def run(self, case_id, query, context, *, session_id=None):
            if case_id == "individual_1_turn_1":
                self.calls.append((case_id, query, context, session_id))
                payload = {
                    "success": True,
                    "response": {"text": failure_text},
                    "call_info": {"run_id": "failed-run"},
                }
                return AgentRunResult(
                    submitted_run_id="failed-run",
                    run_id="failed-run",
                    duration_seconds=2.0,
                    response_text=failure_text,
                    request_context=context,
                    context={
                        PUBLIC_API_RESPONSE_KEY: payload,
                        PUBLIC_API_TRANSPORT_ATTEMPTS_KEY: 1,
                    },
                    tool_calls=[],
                    tool_responses=[],
                )
            return super().run(case_id, query, context, session_id=session_id)

    _FakeClient.instances = []
    monkeypatch.setattr(agent_comparison, "WxoClient", ExecutionFailureClient)
    corpus = _corpus(tmp_path)
    output = tmp_path / "candidate"
    args = _args(corpus, output, "refactor")

    assert agent_comparison.run_candidate(args) == 0

    records = agent_comparison._load_records(output / "results.jsonl")
    failed = records[(1, "individual_1", "individual_1_turn_1")]
    skipped = records[(1, "individual_1", "individual_1_turn_2")]
    assert failed["status"] == "execution_failure"
    assert failed["result"]["run_id"] == "failed-run"
    assert failed["result"]["context"][PUBLIC_API_RESPONSE_KEY]["success"] is True
    assert skipped["status"] == "skipped_prior_error"
    assert json.loads((output / "summary.json").read_text())["execution_failures"] == 1

    reusable, reason = agent_comparison.candidate_cache_is_reusable(
        output,
        source_revision="sha-refactor",
        rag_base_url="https://refactor.example.test",
        suite="full",
        corpus=corpus,
    )
    assert reusable is False
    assert "execution failures" in reason

    _FakeClient.instances = []
    _FakeClient.response_prefix = "recovered"
    monkeypatch.setattr(agent_comparison, "WxoClient", _FakeClient)
    args.resume = True
    assert agent_comparison.run_candidate(args) == 0

    assert [call[0] for call in _FakeClient.instances[-1].calls] == [
        "individual_1_turn_1",
        "individual_1_turn_2",
    ]
    repaired = agent_comparison._load_records(output / "results.jsonl")
    assert all(record["status"] == "completed" for record in repaired.values())


def test_turn_delay_is_bounded_recorded_and_applied_between_live_requests(monkeypatch, tmp_path):
    _FakeClient.instances = []
    _FakeClient.response_prefix = "answer"
    monkeypatch.setattr(agent_comparison, "WxoClient", _FakeClient)
    sleeps = []
    monkeypatch.setattr(agent_comparison.time, "sleep", sleeps.append)
    corpus = _corpus(tmp_path)
    output = tmp_path / "candidate"
    args = _args(corpus, output, "refactor")
    args.turn_delay = 0.5

    assert agent_comparison.run_candidate(args) == 0

    assert sleeps == [0.5, 0.5]
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["turn_delay_seconds"] == 0.5
    reusable, reason = agent_comparison.candidate_cache_is_reusable(
        output,
        source_revision="sha-refactor",
        source_fingerprint="clean",
        rag_base_url="https://refactor.example.test",
        suite="full",
        corpus=corpus,
        endpoint_url="http://localhost:4321",
        transport="wxo",
        turn_delay_seconds=0.0,
    )
    assert reusable is False
    assert reason == "cached turn_delay_seconds does not match"


def test_resume_replays_an_incomplete_conversation_from_its_first_turn(monkeypatch, tmp_path):
    _FakeClient.instances = []
    monkeypatch.setattr(agent_comparison, "WxoClient", _FakeClient)
    corpus = _corpus(tmp_path)
    output = tmp_path / "candidate"
    args = _args(corpus, output, "refactor")
    assert agent_comparison.run_candidate(args) == 0

    records_path = output / "results.jsonl"
    records = agent_comparison._load_records(records_path)
    for key, record in records.items():
        if key[1] != "individual_1":
            continue
        replacement = dict(record)
        replacement["status"] = (
            "error" if key[2] == "individual_1_turn_1" else "skipped_prior_error"
        )
        replacement["error"] = "transient"
        replacement["result"] = {}
        replacement["observations"] = {}
        agent_comparison._append_record(records_path, replacement)

    args.resume = True
    assert agent_comparison.run_candidate(args) == 0

    replay = _FakeClient.instances[-1]
    assert [call[0] for call in replay.calls] == [
        "individual_1_turn_1",
        "individual_1_turn_2",
    ]
    repaired = agent_comparison._load_records(records_path)
    assert all(record["status"] == "completed" for record in repaired.values())


def test_candidate_cache_rejects_incomplete_or_mismatched_runs(monkeypatch, tmp_path):
    _FakeClient.instances = []
    monkeypatch.setattr(agent_comparison, "WxoClient", _FakeClient)
    corpus = _corpus(tmp_path)
    output = tmp_path / "candidate"
    agent_comparison.run_candidate(_args(corpus, output, "main"))

    manifest_path = output / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["summary"]["errors"] = 1
    manifest_path.write_text(json.dumps(manifest))
    records = output / "results.jsonl"
    rows = records.read_text().splitlines()
    failed = json.loads(rows[0])
    failed["status"] = "error"
    rows[0] = json.dumps(failed)
    records.write_text("\n".join(rows) + "\n")

    reusable, reason = agent_comparison.candidate_cache_is_reusable(
        output,
        source_revision="sha-main",
        rag_base_url="https://main.example.test",
        suite="full",
        corpus=corpus,
    )
    assert reusable is False
    assert "errors or skipped" in reason

    reusable, reason = agent_comparison.candidate_cache_is_reusable(
        output,
        source_revision="different",
        rag_base_url="https://main.example.test",
        suite="full",
        corpus=corpus,
    )
    assert reusable is False
    assert "source_revision" in reason


def test_comparison_pairs_runs_and_blinds_candidate_names(monkeypatch, tmp_path):
    _FakeClient.instances = []
    monkeypatch.setattr(agent_comparison, "WxoClient", _FakeClient)
    corpus = _corpus(tmp_path)
    baseline = tmp_path / "baseline"
    challenger = tmp_path / "challenger"

    _FakeClient.response_prefix = "old"
    agent_comparison.run_candidate(_args(corpus, baseline, "main"))
    _FakeClient.response_prefix = "new"
    agent_comparison.run_candidate(_args(corpus, challenger, "refactor"))

    comparison = tmp_path / "comparison"
    args = argparse.Namespace(
        baseline=baseline,
        challenger=challenger,
        output=comparison,
        blind_seed="fixed",
    )
    assert agent_comparison.compare_candidates(args) == 0

    key = json.loads((comparison / "candidate-key.json").read_text())
    assert {key["candidate_a"]["candidate"], key["candidate_b"]["candidate"]} == {
        "main",
        "refactor",
    }
    assert {
        key["candidate_a"]["rag_base_url"],
        key["candidate_b"]["rag_base_url"],
    } == {"https://main.example.test", "https://refactor.example.test"}
    with (comparison / "review-blinded.csv").open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert "main" not in rows[0]["Candidate A Response"]
    assert "refactor" not in rows[0]["Candidate B Response"]
    review_text = (comparison / "review-blinded.csv").read_text(encoding="utf-8-sig")
    assert "search_plans" not in review_text
    assert "unified_rag_retrieval_tool" not in review_text
    assert "plan_retrieval" in review_text
    assert json.loads(rows[0]["Available Plans"])
    assert "Current Plan" in rows[0]
    assert rows[0]["Preferred Candidate (A/B/Tie)"] == ""
