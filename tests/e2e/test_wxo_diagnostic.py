"""Optional direct-WXO diagnostics kept outside the release E2E profile."""

from __future__ import annotations

import os

import pytest
from evaluation.clients.wxo import GENERAL_SEARCH_TOOL, PLAN_DETAILS_TOOL, WxoClient

from tests.e2e.support.workflows import CONTEXTS, WORKFLOWS

WXO_URL = os.getenv("LOCAL_WXO_URL", "http://localhost:4321")
AGENT_NAME = os.getenv("LOCAL_WXO_AGENT_NAME", "Elevance_Health_Shopper_Portal")
ALLOW_REMOTE = os.getenv("LOCAL_WXO_ALLOW_REMOTE") == "1"

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="session")
def wxo() -> WxoClient:
    return WxoClient(
        base_url=WXO_URL,
        agent_name=AGENT_NAME,
        timeout=int(os.getenv("LOCAL_WXO_E2E_TIMEOUT", "120")),
        allow_remote=ALLOW_REMOTE,
        api_key=os.getenv("WXO_API_KEY") if ALLOW_REMOTE else None,
        agent_id=os.getenv("LOCAL_WXO_AGENT_ID") or None,
    )


DIAGNOSTICS = (
    ("general_document_search", GENERAL_SEARCH_TOOL),
    ("mols_structured_details", PLAN_DETAILS_TOOL),
)


@pytest.mark.parametrize(
    ("workflow_id", "tool_name"),
    DIAGNOSTICS,
    ids=("general_document_search", "mols_structured_details"),
)
def test_wxo_tool_round_trip(workflow_id: str, tool_name: str, wxo: WxoClient) -> None:
    workflow = next(workflow for workflow in WORKFLOWS if workflow.id == workflow_id)
    turn = workflow.turns[0]
    result = wxo.run(workflow.id, turn.query, CONTEXTS[workflow.context])

    assert result.response_text.strip()
    assert tool_name in [call.name for call in result.tool_calls], result.trace_summary()
    if tool_name == GENERAL_SEARCH_TOOL:
        envelope = result.search_envelope()
    else:
        envelope = result.plan_details_envelope()
    assert envelope is not None
    assert envelope.get("outcome") != "error", envelope
