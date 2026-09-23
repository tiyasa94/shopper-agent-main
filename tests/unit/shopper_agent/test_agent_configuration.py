"""Static runtime contracts for the native shopper agent."""

from pathlib import Path

import yaml

from shared.routing import STRUCTURED_PLAN_QUOTE_USER_MESSAGE

AGENT_FILE = Path(__file__).resolve().parents[3] / "src/agent.yaml"


def test_agent_uses_only_request_and_thread_context() -> None:
    agent = yaml.safe_load(AGENT_FILE.read_text())

    assert agent["guidelines"] == []
    assert agent["memory_enabled"] is False
    assert agent["sync_tool_flow_interactions"] is False
    assert agent["llm_config"] == {"temperature": 0.0}


def test_agent_does_not_receive_ui_visibility_instructions() -> None:
    instructions = yaml.safe_load(AGENT_FILE.read_text())["instructions"]

    assert "visible" not in instructions.casefold()
    assert "visibility" not in instructions.casefold()
    assert "available_plans: {application_available_plans}" in instructions
    assert "recommended_plans: {application_recommended_plans}" in instructions
    assert "current_enrolled_plan: {user_current_plan}" in instructions


def test_agent_follows_the_current_structured_quote_control_instruction() -> None:
    instructions = yaml.safe_load(AGENT_FILE.read_text())["instructions"]

    assert "First resolve plan scope as directed above" in instructions
    assert "Follow shopper_control.structured_plan_quote_instruction exactly" in instructions
    assert "do not infer or apply the opposite state" in instructions
    assert "Unless that instruction requires a no-tool response" in instructions
    assert STRUCTURED_PLAN_QUOTE_USER_MESSAGE not in instructions
    assert "structured_plan_quote_available=false" not in instructions


def test_agent_uses_explicit_all_catalog_mode_for_structured_comparisons() -> None:
    instructions = yaml.safe_load(AGENT_FILE.read_text())["instructions"]

    assert "all_available_plans=true and plan_ids=[]" in instructions
    assert "do not enumerate the catalog into plan_ids" in instructions
    assert (
        "Never use full-catalog mode as a fallback for an unresolved singular plan reference"
        in instructions
    )
    assert "The absence of a selected plan does not make a request exhaustive" in instructions
    assert "The most I could pay in a year" in instructions


def test_agent_plan_scope_prefers_current_enrollment_and_conversation_context() -> None:
    instructions = yaml.safe_load(AGENT_FILE.read_text())["instructions"]

    assert "Use plans explicitly selected in the latest message first" in instructions
    assert "use current_enrolled_plan when it is populated" in instructions
    assert (
        "use an unambiguous active plan or plan set established in conversation history"
        in instructions
    )
    assert "ask which plan or plans the shopper means" in instructions
    assert "The word “my” alone never selects a plan" in instructions
    assert "stop before applying quote-availability or tool-choice rules" in instructions
    assert "A singular first-person plan fact is not a full-catalog request" in instructions


def test_agent_preserves_partial_success_and_conflicting_evidence() -> None:
    instructions = yaml.safe_load(AGENT_FILE.read_text())["instructions"]

    assert "does not erase facts successfully returned by another current tool call" in instructions
    assert "Keep every successful structured result even if that search fails" in instructions
    assert "mark that requested plan and fact as conflicting" in instructions
    assert "Evidence about a different plan or a general plan type cannot establish" in instructions
    assert "those statements conflict for an unqualified routine-care question" in instructions
    assert "The only supported conclusion in that case" in instructions
    assert (
        "a conflicting row cannot produce a positive or negative factual conclusion" in instructions
    )
    assert "A returned numeric zero is a supported value" in instructions
    assert (
        "a follow-up requesting a different structured type requires a fresh call" in instructions
    )
