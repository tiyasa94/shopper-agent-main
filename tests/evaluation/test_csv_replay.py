"""Unit tests for the local CSV replay workflow."""

import json

from evaluation.clients.wxo import AgentRunResult, ToolCall, ToolResponse
from evaluation.csv_replay import read_source_rows, result_columns, source_context_to_wxo


def test_read_source_rows_accepts_mac_roman(tmp_path):
    source = tmp_path / "questions.csv"
    source.write_bytes(
        'Nature,Question,Answer,Context\ngeneric_info,"What’s a copay?",,"{}"\n'.encode("mac_roman")
    )

    fields, rows, encoding = read_source_rows(source)

    assert fields == ["Nature", "Question", "Answer", "Context"]
    assert rows[0]["Question"] == "What’s a copay?"
    assert encoding == "mac_roman"


def test_source_context_to_wxo_maps_nested_spreadsheet_context():
    raw = json.dumps(
        {
            "application_context": {
                "available_plans": [{"plan_id": "P1", "plan_name": "Plan One", "visible": True}],
                "recommended_plans": [{"plan_id": "P1", "plan_name": "Plan One"}],
                "market_segment": "Medicare",
                "exchange_indicator": "On",
                "current_page": "plans",
            },
            "prospect_context": {"prospect_type": "member"},
            "user_context": {
                "brand": "ABC",
                "state_code": "CA",
                "zip_code": "91320",
                "county_code": "06111",
                "county_name": "VENTURA",
                "requested_eff_date": "2026-07-01",
                "dsnp_eligibility": "",
                "current_plan": {"plan_id": "P1", "plan_name": "Plan One"},
                "language": "en",
                "applicants": [],
                "subsidy_amt": "",
                "cost_share_reduction": "",
                "part_a_eff_date": "",
                "part_b_eff_date": "",
            },
            "business_context": {},
        }
    )

    context = source_context_to_wxo(raw)

    assert context["application_market_segment"] == "Medicare"
    assert context["prospect_type"] == "member"
    assert json.loads(context["user_current_plan"])["plan_id"] == "P1"
    assert json.loads(context["application_available_plans"])[0]["plan_id"] == "P1"


def test_result_columns_include_response_search_context_audit_and_trace(tmp_path):
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run-1",
        duration_seconds=1.25,
        response_text="Summary answer: response",
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
                "1",
                "search_plans",
                {"queries": ["Question"], "plan_ids": ["P1"]},
            ),
        ],
        tool_responses=[
            ToolResponse(
                "1",
                "search_plans",
                {
                    "outcome": "candidates",
                    "coverage_complete": True,
                    "missing_plans": [],
                    "results": [{"text": "Copay is $20", "metadata": {"plan_id": "P1"}}],
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

    columns = result_columns(result, tmp_path / "results.jsonl")

    assert columns["Actual Response"] == "Summary answer: response"
    assert columns["Search Plan IDs"] == '["P1"]'
    assert "Copay is $20" in columns["Retrieved RAG Context"]
    assert columns["Guardrail/Audit Identification"] == "rag_response"
    assert columns["Tool Trace"] == "search_plans"


def test_result_columns_read_embedded_terminal_audit(tmp_path):
    result = AgentRunResult(
        submitted_run_id="submitted",
        run_id="run-2",
        duration_seconds=0.75,
        response_text="Please call an agent.",
        request_context={},
        context={
            "_turn_result": json.dumps(
                {
                    "route": "terminal",
                    "audit_completed": True,
                    "escalation": {
                        "type": True,
                        "identification": "live_agent_request",
                        "description": "ok",
                    },
                }
            )
        },
        tool_calls=[],
        tool_responses=[],
    )

    columns = result_columns(result, tmp_path / "results.jsonl")

    assert json.loads(columns["Turn Result"])["route"] == "terminal"
    assert columns["RAG Status"] == ""
    assert columns["Guardrail/Audit Identification"] == "live_agent_request"
