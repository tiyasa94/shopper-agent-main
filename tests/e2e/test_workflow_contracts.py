"""Static contracts for the intentionally small functional E2E set."""

import pytest

from tests.e2e.support.workflows import CONTEXTS, WORKFLOWS, Action, select_workflows


def test_workflow_set_is_small_and_covers_each_cross_service_action() -> None:
    assert len(WORKFLOWS) == 10
    assert sum(len(workflow.turns) for workflow in WORKFLOWS) == 13
    assert {turn.action for workflow in WORKFLOWS for turn in workflow.turns} == set(Action)
    assert sum(turn.all_available_plans for workflow in WORKFLOWS for turn in workflow.turns) == 2


def test_smoke_is_a_three_workflow_subset() -> None:
    smoke = select_workflows("smoke")

    assert [workflow.id for workflow in smoke] == [
        "api_round_trip",
        "general_document_search",
        "mols_structured_details",
    ]
    assert set(smoke) < set(select_workflows("full"))


def test_workflows_reuse_evaluation_contexts_and_catalog_ids() -> None:
    for workflow in WORKFLOWS:
        context = CONTEXTS[workflow.context]
        available_ids = {plan["plan_id"] for plan in context.get("application_available_plans", [])}
        for turn in workflow.turns:
            assert set(turn.plan_ids) <= available_ids, workflow.id


def test_unknown_workflow_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="E2E_WORKFLOW_SET"):
        select_workflows("behavioral")
