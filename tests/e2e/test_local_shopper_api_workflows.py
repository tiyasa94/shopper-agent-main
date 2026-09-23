"""Functional E2E workflows through the local Shopper Assistant API."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from evaluation.clients.shopper_api import (
    PUBLIC_API_RESPONSE_KEY,
    PUBLIC_API_TRANSPORT_ATTEMPTS_KEY,
    ShopperApiClient,
)

from tests.e2e.support.workflows import (
    CONTEXTS,
    Action,
    Turn,
    Workflow,
    select_workflows,
)

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = os.getenv("LOCAL_SHOPPER_API_URL", "http://127.0.0.1:8082")
PROFILE = os.getenv("E2E_WORKFLOW_SET", "smoke")
WORKFLOWS = select_workflows(PROFILE)
ENV_FILE = Path(
    os.getenv(
        "SHOPPER_ASSISTANT_API_ENV_FILE",
        str(ROOT.parent / "shopper-platform/apps/shopper-assistant-api/.env.local"),
    )
)

pytestmark = pytest.mark.e2e


def _api_key() -> str:
    if explicit := os.getenv("LOCAL_SHOPPER_API_KEY"):
        return explicit
    if not ENV_FILE.is_file():
        raise RuntimeError(f"Missing Shopper Assistant API environment file: {ENV_FILE}")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("CLIENT_API_KEYS_JSON="):
            values = json.loads(line.partition("=")[2])
            if isinstance(values, dict) and values:
                return str(next(iter(values.values())))
    raise RuntimeError("CLIENT_API_KEYS_JSON is missing from the Shopper Assistant API environment")


@pytest.fixture(scope="session")
def shopper_api() -> ShopperApiClient:
    client = ShopperApiClient(
        BASE_URL,
        api_key=_api_key(),
        timeout=int(os.getenv("LOCAL_SHOPPER_API_E2E_TIMEOUT", "330")),
        allow_remote=False,
    )
    yield client
    client.close()


def _assert_common_contract(query: str, body: dict[str, Any]) -> None:
    assert body.get("success") is True, body
    response = body.get("response")
    assert isinstance(response, dict) and str(response.get("text", "")).strip(), body

    call_info = body.get("call_info")
    assert isinstance(call_info, dict), body
    request_id = call_info.get("request_id")
    run_id = call_info.get("run_id")
    assert isinstance(request_id, str) and request_id, body
    assert isinstance(run_id, str) and run_id, body
    assert request_id != run_id, body

    user_query = body.get("user_query")
    assert isinstance(user_query, dict) and user_query.get("text") == query, body
    metadata = body.get("metadata")
    assert isinstance(metadata, dict), body
    assert isinstance(metadata.get("agents_invoked"), list) and metadata["agents_invoked"], body
    assert isinstance(metadata.get("plans_searched_for"), list), body
    assert isinstance(metadata.get("plan_details"), dict), body

    rag = body.get("rag_context")
    assert isinstance(rag, dict), body
    contexts = rag.get("contexts")
    total = rag.get("total_retrieved")
    assert isinstance(contexts, list), body
    assert isinstance(total, int) and not isinstance(total, bool), body
    assert total == len(contexts), body
    for context in contexts:
        assert isinstance(context, dict) and str(context.get("content", "")).strip(), body
        reference = context.get("reference")
        assert isinstance(reference, dict) and reference.get("source") == "wxo_tool", body


def _assert_plan_details(turn: Turn, details: dict[str, Any]) -> None:
    assert details.get("called") is True, details
    assert details.get("call_count", 0) >= 1, details
    if not turn.all_available_plans:
        assert set(turn.plan_ids) <= set(details.get("plan_ids", [])), details
    assert set(turn.detail_types) <= set(details.get("detail_types", [])), details
    assert details.get("outcome") == "complete", details
    assert details.get("coverage_complete") is True, details


def _assert_action(turn: Turn, body: dict[str, Any]) -> None:
    metadata = body["metadata"]
    details = metadata["plan_details"]
    searched = set(metadata["plans_searched_for"])
    rag_total = body["rag_context"]["total_retrieved"]
    intent = body["user_query"].get("business_intent")

    if turn.action is Action.TERMINAL:
        assert rag_total == 0, body
        assert not searched, body
        assert details.get("called") is False, body
        return
    if turn.action is Action.GENERAL_SEARCH:
        assert intent == "generic_info", body
        assert rag_total > 0, body
        assert not searched, body
        assert details.get("called") is False, body
        return

    expected_intent = "broad_plans" if len(turn.plan_ids) > 1 else "specific_plan"
    assert intent == expected_intent, body
    assert set(turn.plan_names) <= searched, body
    if turn.action is Action.PLAN_SEARCH:
        assert rag_total > 0, body
    else:
        assert turn.action is Action.PLAN_DETAILS
        _assert_plan_details(turn, details)


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda workflow: workflow.id)
def test_local_shopper_workflow(workflow: Workflow, shopper_api: ShopperApiClient) -> None:
    context = workflow.context
    session_id = f"e2e-{workflow.id}-{uuid4().hex[:10]}"

    for index, turn in enumerate(workflow.turns, start=1):
        result = shopper_api.run(
            f"{workflow.id}-{index}",
            turn.query,
            CONTEXTS[context],
            session_id=session_id,
        )
        body = result.context[PUBLIC_API_RESPONSE_KEY]
        assert result.context[PUBLIC_API_TRANSPORT_ATTEMPTS_KEY] <= 3
        _assert_common_contract(turn.query, body)
        _assert_action(turn, body)


def _incomplete_quote_context() -> dict[str, Any]:
    context = deepcopy(CONTEXTS["medicare_prospect"])
    context["user_county_name"] = ""
    return context


def _assert_website_completion_guidance(body: dict[str, Any]) -> None:
    response = body["response"]["text"].casefold()
    assert "complete" in response, body
    assert "quote" in response or "required information" in response, body
    assert "website" in response or "portal" in response, body
    assert body["rag_context"]["total_retrieved"] == 0, body
    details = body["metadata"]["plan_details"]
    if details["called"]:
        assert details["outcome"] == "unavailable", body
        assert details["coverage_complete"] is False, body
        assert not details["fallback_detail_types"], body


def test_general_search_works_with_incomplete_quote_context(
    shopper_api: ShopperApiClient,
) -> None:
    query = "What is a copay?"
    result = shopper_api.run(
        "incomplete-quote-general-search",
        query,
        _incomplete_quote_context(),
    )
    body = result.context[PUBLIC_API_RESPONSE_KEY]

    _assert_common_contract(query, body)
    _assert_action(Turn(query=query, action=Action.GENERAL_SEARCH), body)


def test_structured_plan_request_with_incomplete_quote_context_uses_website_guidance(
    shopper_api: ShopperApiClient,
) -> None:
    query = "What is the medical deductible for Anthem Prime (HMO-POS)?"
    result = shopper_api.run(
        "incomplete-quote-plan-search",
        query,
        _incomplete_quote_context(),
    )
    body = result.context[PUBLIC_API_RESPONSE_KEY]

    _assert_common_contract(query, body)
    _assert_website_completion_guidance(body)


def test_premium_request_with_incomplete_quote_context_uses_website_guidance(
    shopper_api: ShopperApiClient,
) -> None:
    query = "What is the monthly premium for Anthem Prime (HMO-POS)?"
    result = shopper_api.run(
        "incomplete-quote-premium-guidance",
        query,
        _incomplete_quote_context(),
    )
    body = result.context[PUBLIC_API_RESPONSE_KEY]

    _assert_common_contract(query, body)
    _assert_website_completion_guidance(body)


def test_premium_request_without_current_or_selected_plan_requires_clarification(
    shopper_api: ShopperApiClient,
) -> None:
    query = "What is my monthly premium?"
    result = shopper_api.run(
        "missing-plan-premium-clarification",
        query,
        CONTEXTS["plans_api_mols_minimum"],
    )
    body = result.context[PUBLIC_API_RESPONSE_KEY]

    _assert_common_contract(query, body)
    response = body["response"]["text"].casefold()
    assert "which plan" in response, body
    assert body["rag_context"]["total_retrieved"] == 0, body
    assert not body["metadata"]["plans_searched_for"], body
    assert body["metadata"]["plan_details"]["called"] is False, body
