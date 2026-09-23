"""Tests for the interactive local Shopper API client."""

import json
import os

import pytest
import yaml
from evaluation.chat import (
    describe_context,
    format_result,
    load_contexts,
    resolve_api_key,
)
from evaluation.clients.shopper_api import (
    PUBLIC_API_RESPONSE_KEY,
    PUBLIC_API_TRANSPORT_ATTEMPTS_KEY,
)
from evaluation.clients.wxo import AgentRunResult


def test_load_contexts_and_describe_member_plan(tmp_path) -> None:
    path = tmp_path / "contexts.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "contexts": {
                    "member": {
                        "prospect_type": "member",
                        "application_market_segment": "Medicare",
                        "user_current_plan": {"plan_id": "P1", "plan_name": "Current Plan"},
                        "application_recommended_plans": [
                            {"plan_id": "P2", "plan_name": "Recommended Plan"}
                        ],
                    }
                }
            }
        )
    )

    contexts = load_contexts(path)
    rendered = describe_context("member", contexts["member"])

    assert "audience=member market=Medicare" in rendered
    assert "current=Current Plan (P1)" in rendered
    assert "recommended=Recommended Plan (P2)" in rendered


def test_load_contexts_rejects_missing_mapping(tmp_path) -> None:
    path = tmp_path / "contexts.yaml"
    path.write_text("conversations: []\n")

    with pytest.raises(ValueError, match="contexts"):
        load_contexts(path)


def test_resolve_api_key_reads_client_json_directly_from_dotenv(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text('CLIENT_API_KEYS_JSON={"local-client":"secret-key"}\n')
    monkeypatch.delenv("SHOPPER_API_KEY", raising=False)
    monkeypatch.delenv("CLIENT_API_KEYS_JSON", raising=False)

    api_key, source = resolve_api_key("SHOPPER_API_KEY", env_file)

    assert api_key == "secret-key"
    assert source == str(env_file.resolve())


@pytest.mark.parametrize(
    "dotenv_value",
    [
        '\'{"local-client":"secret-key"}\'',
        '"{\\"local-client\\":\\"secret-key\\"}"',
    ],
)
def test_resolve_api_key_accepts_quoted_dotenv_json(
    tmp_path, monkeypatch, dotenv_value: str
) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text(f"CLIENT_API_KEYS_JSON={dotenv_value}\n")
    monkeypatch.delenv("SHOPPER_API_KEY", raising=False)
    monkeypatch.delenv("CLIENT_API_KEYS_JSON", raising=False)

    api_key, _ = resolve_api_key("SHOPPER_API_KEY", env_file)

    assert api_key == "secret-key"


def test_resolve_api_key_prefers_explicit_environment_value(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text('CLIENT_API_KEYS_JSON={"local-client":"file-key"}\n')
    monkeypatch.setenv("SHOPPER_API_KEY", "explicit-key")

    api_key, source = resolve_api_key("SHOPPER_API_KEY", env_file)

    assert api_key == "explicit-key"
    assert source == "$SHOPPER_API_KEY"


def test_resolve_api_key_recovers_from_json_damaged_by_source(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text('CLIENT_API_KEYS_JSON={"local-client":"file-key"}\n')
    monkeypatch.delenv("SHOPPER_API_KEY", raising=False)
    monkeypatch.setenv("CLIENT_API_KEYS_JSON", "{local-client:file-key}")

    api_key, source = resolve_api_key("SHOPPER_API_KEY", env_file)

    assert api_key == "file-key"
    assert source == str(env_file.resolve())


def test_resolve_api_key_reports_searched_file(tmp_path, monkeypatch) -> None:
    missing = tmp_path / ".env.local"
    monkeypatch.delenv("SHOPPER_API_KEY", raising=False)
    monkeypatch.delenv("CLIENT_API_KEYS_JSON", raising=False)

    with pytest.raises(ValueError, match=os.fspath(missing)):
        resolve_api_key("SHOPPER_API_KEY", missing)


def test_format_result_shows_public_agent_diagnostics() -> None:
    payload = {
        "call_info": {"run_id": "run-1", "trace_id": "trace-1"},
        "user_query": {"business_intent": "specific_plan"},
        "response": {"text": "Your deductible is $250."},
        "rag_context": {
            "total_retrieved": 1,
            "contexts": [
                {
                    "content": "The plan has a $250 deductible.",
                    "reference": {"document_name": "Evidence of Coverage"},
                    "relevance_score": 0.91,
                }
            ],
        },
        "metadata": {
            "plans_searched_for": [{"plan_id": "P1", "plan_name": "Current Plan"}],
            "plans_found": ["P1"],
            "escalation": {"type": False},
            "total_processing_time_ms": 1234,
        },
    }
    result = AgentRunResult(
        submitted_run_id="run-1",
        run_id="run-1",
        duration_seconds=1.25,
        response_text="Your deductible is $250.",
        request_context={},
        context={
            PUBLIC_API_RESPONSE_KEY: payload,
            PUBLIC_API_TRANSPORT_ATTEMPTS_KEY: 2,
        },
        tool_calls=[],
        tool_responses=[],
    )

    rendered = format_result(result, rag_mode="summary", raw=True)

    assert "assistant> Your deductible is $250." in rendered
    assert "intent: specific_plan" in rendered
    assert "Current Plan (P1)" in rendered
    assert "Evidence of Coverage score=0.91" in rendered
    assert "HTTP attempts=2" in rendered
    assert json.dumps(payload, indent=2, ensure_ascii=False) in rendered
