import asyncio
import json
from collections.abc import Mapping
from typing import Any, cast
from unittest.mock import Mock, patch

import pytest
from ibm_watsonx_orchestrate.run.context import AgentRun

from shared.context import SEARCH_CONTROL_CONTEXT_KEY
from shared.rag import (
    PlanSummary,
    SearchPlanMetadata,
    SearchPlanResult,
    SearchPlansResponse,
)
from shared.routing import STRUCTURED_PLAN_QUOTE_USER_MESSAGE
from tools import plan_search
from tools.plan_search import search_plans

_ASYNC_SEARCH_PLANS = search_plans.fn.__wrapped__


@pytest.fixture(autouse=True)
def _run_registered_tool(monkeypatch):
    """Keep the behavioral suite synchronous while exercising the async tool wrapper."""

    def run(*args, **kwargs):
        return asyncio.run(_ASYNC_SEARCH_PLANS(*args, **kwargs))

    monkeypatch.setattr(search_plans, "fn", run)


def _context(plans):
    return AgentRun(
        request_context={
            "application_available_plans": json.dumps(plans),
            "user_requested_eff_date": "2026-01-01",
            "application_market_segment": "Medicare",
        }
    )


def _authorize(context, *, structured_quote_available=True):
    plans = json.loads(context.request_context["application_available_plans"])
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(
        {
            "route": "search_turn",
            "plan_lookup": [{**plan, "current": False, "recommended": False} for plan in plans],
            "structured_plan_quote_available": structured_quote_available,
        }
    )
    return context


def _call(context, *, plan_ids=None, queries=None):
    return search_plans.fn(
        plan_ids=["P1"] if plan_ids is None else plan_ids,
        queries=queries or ["What medical deductible applies?"],
        context=context,
    )


def test_adapter_returns_canned_response_for_oversized_plan_set_without_rag():
    context = _authorize(
        _context([{"plan_id": f"P{index}", "plan_name": f"Plan {index}"} for index in range(1, 7)])
    )

    with patch.object(plan_search.rag, "search_plans") as rag_search:
        result = search_plans.fn(
            plan_ids=[f"P{index}" for index in range(1, 7)],
            queries=["What medical deductible applies?"],
            context=context,
        )

    rag_search.assert_not_called()
    assert result.outcome == "insufficient"
    assert result.business_intent == "broad_plans"
    assert result.required_response == plan_search.PLAN_COMPARISON_LIMIT_MESSAGE
    assert result.no_results_reason == "plan_limit_exceeded"
    assert result.insufficiency_reason == "More than five plans were selected for comparison"
    assert result.plans_searched_for == [f"Plan {index}" for index in range(1, 7)]
    assert result.audit_completed is True


@pytest.mark.parametrize(
    ("query", "expected_addendum", "expected_match"),
    [
        ("Which extra benefits are included?", plan_search.EXTRA_BENEFITS_DOCUMENT_MESSAGE, False),
        ("Describe this extra-benefit.", plan_search.EXTRA_BENEFITS_DOCUMENT_MESSAGE, False),
        (
            "What Essential Extras are offered?",
            plan_search.ESSENTIAL_EXTRAS_DOCUMENT_MESSAGE,
            True,
        ),
        (
            "Compare the essential-extra options.",
            plan_search.ESSENTIAL_EXTRAS_DOCUMENT_MESSAGE,
            True,
        ),
        ("Are meals and fitness programs included?", None, False),
    ],
)
def test_extra_benefits_guidance_matches_only_targeted_terms(
    query,
    expected_addendum,
    expected_match,
):
    assert plan_search._extra_benefits_addendum(query) == expected_addendum
    assert plan_search._is_essential_extras_request(query) is expected_match


@pytest.mark.parametrize(
    ("current_query", "document_query", "expected_addendum", "expected_instructions"),
    [
        (
            "Which extra benefits does this plan offer?",
            "Which meals and fitness programs are offered?",
            plan_search.EXTRA_BENEFITS_DOCUMENT_MESSAGE,
            None,
        ),
        (
            "This plan.",
            "Which Essential Extras does it offer?",
            plan_search.ESSENTIAL_EXTRAS_DOCUMENT_MESSAGE,
            plan_search.ESSENTIAL_EXTRAS_RESPONSE_INSTRUCTIONS,
        ),
        ("Does this plan offer meals?", "Are post-discharge meals offered?", None, None),
        (
            "Does premium change Essential Extras choices?",
            "Does premium affect the Essential Extras choice limit?",
            plan_search.ESSENTIAL_EXTRAS_DOCUMENT_MESSAGE,
            plan_search.ESSENTIAL_EXTRAS_RESPONSE_INSTRUCTIONS
            + "\n\n"
            + plan_search.ESSENTIAL_EXTRAS_PREMIUM_INSTRUCTIONS,
        ),
        (
            "Does premium change the benefits?",
            "Does premium affect the choice limit?",
            None,
            None,
        ),
        (
            "What Essential Extras does Premium Savings offer?",
            "What Essential Extras options are available with Premium Savings?",
            plan_search.ESSENTIAL_EXTRAS_DOCUMENT_MESSAGE,
            plan_search.ESSENTIAL_EXTRAS_RESPONSE_INSTRUCTIONS,
        ),
    ],
)
def test_adapter_returns_supported_response_addendum_for_targeted_broad_terms(
    current_query,
    document_query,
    expected_addendum,
    expected_instructions,
):
    plan = {"plan_id": "P1", "plan_name": "Anthem Prime (HMO-POS)"}
    context = _authorize(_context([plan]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = current_query
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            SearchPlanResult(
                evidence_id="extra-benefit",
                plan_id="P1",
                rank=1,
                score=0.9,
                plan_name=plan["plan_name"],
                text="Post-discharge meals and a fitness program are included.",
                metadata=SearchPlanMetadata(plan_name=plan["plan_name"]),
            )
        ],
        plans_found=[plan["plan_name"]],
        plans_searched_for=[plan["plan_name"]],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream):
        result = _call(context, queries=[document_query])

    assert result.outcome == "candidates"
    assert result.required_response_addendum == expected_addendum
    assert result.response_instructions == " ".join(
        part
        for part in (plan_search.PLAN_EVIDENCE_ATTRIBUTION_INSTRUCTIONS, expected_instructions)
        if part
    )


def test_response_omits_addendum_when_candidate_evidence_is_unavailable():
    result = plan_search._response(
        SearchPlansResponse(outcome="insufficient"),
        required_response_addendum=plan_search.EXTRA_BENEFITS_DOCUMENT_MESSAGE,
        response_instructions=plan_search.ESSENTIAL_EXTRAS_RESPONSE_INSTRUCTIONS,
    )

    assert result.outcome == "insufficient"
    assert result.required_response_addendum is None
    assert result.response_instructions is None


def test_adapter_validates_ids_and_preserves_source_owned_business_intent():
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Anthem Prime (HMO-POS)"}]))
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        plans_found=["Anthem Prime (HMO-POS)"],
        plans_searched_for=["Anthem Prime (HMO-POS)"],
        coverage_complete=True,
    )
    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(context)

    assert search.call_args.kwargs["plan_ids"] == ["P1"]
    assert search.call_args.kwargs["business_intent"] == "specific_plan"
    assert "medical deductible" in search.call_args.kwargs["query"].casefold()
    assert "plan_level_benefits" not in search.call_args.kwargs
    assert search.call_args.kwargs["searches"][0].semantic_query == search.call_args.kwargs["query"]
    assert result.business_intent == "specific_plan"
    assert result.requested_fact == "What medical deductible applies?"
    assert result.evidence_status == "unreviewed_candidates"
    assert result.required_response_if_unsupported == plan_search.CANONICAL_NO_EVIDENCE_RESPONSE
    assert "candidate_evidence_instruction" not in result.model_dump()
    assert result.audit_completed is True
    assert result.escalation == {
        "type": False,
        "identification": "rag_response",
        "description": "Normal RAG response logging",
    }
    assert "audit_payload" not in result.model_dump()


def test_adapter_retrieves_agent_resolved_rehab_comparison():
    context = _authorize(
        _context(
            [
                {"plan_id": "8XVY", "plan_name": "Anthem Bronze 60 D HMO"},
                {"plan_id": "8XWN", "plan_name": "Anthem Platinum 90 D HMO"},
            ]
        )
    )
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = "compare bronze vs plat for rehab benefits"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="broad_plans",
        results=[
            SearchPlanResult(
                evidence_id="bronze-rehab",
                plan_id="8XVY",
                rank=1,
                score=0.9,
                plan_name="Anthem Bronze 60 D HMO",
                text="Rehabilitation therapy evidence for Bronze.",
                metadata=SearchPlanMetadata(plan_name="Anthem Bronze 60 D HMO"),
            ),
            SearchPlanResult(
                evidence_id="platinum-rehab",
                plan_id="8XWN",
                rank=2,
                score=0.8,
                plan_name="Anthem Platinum 90 D HMO",
                text="Rehabilitation therapy evidence for Platinum.",
                metadata=SearchPlanMetadata(plan_name="Anthem Platinum 90 D HMO"),
            ),
        ],
        plans_found=["Anthem Bronze 60 D HMO", "Anthem Platinum 90 D HMO"],
        plans_searched_for=["Anthem Bronze 60 D HMO", "Anthem Platinum 90 D HMO"],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(
            context,
            plan_ids=["8XVY", "8XWN"],
            queries=["What are the rehabilitation therapy benefits for each plan?"],
        )

    assert result.outcome == "candidates"
    assert search.call_args.kwargs["plan_ids"] == ["8XVY", "8XWN"]
    assert search.call_args.kwargs["searches"][0].semantic_query == (
        "What are the rehabilitation therapy benefits for each plan?"
    )


def test_adapter_rejects_broad_supplement_overview_after_document_type_is_known():
    context = _authorize(_context([{"plan_id": "G", "plan_name": "Plan G"}]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = "What does Plan G cover?"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text="# Plan G (continued)\n\n## Parts A and B Services\n\nPlan pays 100%.",
            )
        ],
        plans_found=["Plan G"],
        plans_searched_for=["Plan G"],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(
            context,
            plan_ids=["G"],
            queries=[
                "What hospital coverage applies?",
                "What physician coverage applies?",
            ],
        )

    assert search.call_count == 1
    assert result.outcome == "insufficient"
    assert result.business_intent == "specific_plan"
    assert "specific benefit" in result.insufficiency_reason
    assert result.results == []


def test_adapter_keeps_premium_policy_question_on_document_search_path():
    context = _authorize(_context([{"plan_id": "G", "plan_name": "Plan G"}]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = "When is written notice of a premium change sent?"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text="# Plan G\n\nWritten notice of a premium change is sent before renewal.",
            )
        ],
        plans_found=["Plan G"],
        plans_searched_for=["Plan G"],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(
            context,
            plan_ids=["G"],
            queries=["When is written notice of a premium change sent?"],
        )

    search.assert_called_once()
    assert result.outcome == "candidates"
    assert result.no_results_reason is None


@pytest.mark.parametrize(
    ("current_query", "selection_only", "document_query"),
    [
        (
            "What is the monthly premium for Plan G?",
            False,
            "What is the monthly premium for Plan G?",
        ),
        ("Plan G", True, "What is the monthly premium?"),
        (
            "When is written notice of a premium change sent?",
            False,
            "What is the monthly premium?",
        ),
        (
            "What is the monthly premium for Plan G?",
            False,
            "What is the medical deductible?",
        ),
    ],
)
def test_adapter_rejects_plan_purchase_amounts_before_document_retrieval(
    current_query,
    selection_only,
    document_query,
):
    context = _authorize(_context([{"plan_id": "G", "plan_name": "Plan G"}]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = current_query
    control["selection_only"] = selection_only
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)

    with patch.object(plan_search.rag, "search_plans") as search:
        result = _call(context, plan_ids=["G"], queries=[document_query])

    search.assert_not_called()
    assert result.outcome == "insufficient"
    assert result.business_intent == "specific_plan"
    assert result.plans_searched_for == ["Plan G"]
    assert result.results == []
    assert result.no_results_reason == "premium_requires_structured_details"
    assert result.required_response == plan_search.CANONICAL_NO_EVIDENCE_RESPONSE
    assert result.audit_completed is True


def test_unavailable_quote_premium_returns_website_recovery_without_rag():
    context = _authorize(
        _context([{"plan_id": "G", "plan_name": "Plan G"}]),
        structured_quote_available=False,
    )
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control.update(
        {
            "current_user_query": "What is the monthly premium for Plan G?",
            "structured_plan_quote_user_message": STRUCTURED_PLAN_QUOTE_USER_MESSAGE,
        }
    )
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)

    with patch.object(plan_search.rag, "search_plans") as search:
        result = _call(
            context,
            plan_ids=["G"],
            queries=["What is the monthly premium for Plan G?"],
        )

    search.assert_not_called()
    assert result.outcome == "insufficient"
    assert result.no_results_reason == "premium_requires_structured_details"
    assert result.required_response == STRUCTURED_PLAN_QUOTE_USER_MESSAGE


def test_adapter_omits_premium_subquery_and_retrieves_supported_parts():
    context = _authorize(_context([{"plan_id": "G", "plan_name": "Plan G"}]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = "What benefits and costs does Plan G cover?"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text="# Plan G\n\nThe medical deductible is $0.",
            )
        ],
        plans_found=["Plan G"],
        plans_searched_for=["Plan G"],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(
            context,
            plan_ids=["G"],
            queries=[
                "What is the monthly premium for Plan G?",
                "What is the medical deductible for Plan G?",
            ],
        )

    search.assert_called_once()
    searches = search.call_args.kwargs["searches"]
    assert len(searches) == 1
    assert "deductible" in searches[0].semantic_query
    assert "premium" not in searches[0].semantic_query
    assert result.outcome == "candidates"


def test_adapter_consolidates_bounded_excess_questions_before_retrieval():
    context = _authorize(_context([{"plan_id": "G", "plan_name": "Plan G"}]))
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        plans_searched_for=["Plan G"],
    )
    queries = [f"What is benefit {index}?" for index in range(8)]

    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(context, plan_ids=["G"], queries=queries)

    assert result.outcome == "candidates"
    searches = search.call_args.kwargs["searches"]
    assert len(searches) == 4
    assert all(";" in item.semantic_query for item in searches)


def test_adapter_does_not_treat_generic_plan_cost_as_a_blatant_premium_query():
    context = _authorize(_context([{"plan_id": "G", "plan_name": "Plan G"}]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = "How much does Plan G cost?"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text="# Plan G\n\nThe current document describes plan costs.",
            )
        ],
        plans_found=["Plan G"],
        plans_searched_for=["Plan G"],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(context, plan_ids=["G"], queries=["How much does this plan cost?"])

    search.assert_called_once()
    assert result.outcome == "candidates"


def test_adapter_keeps_medsup_benefit_question_on_document_evidence_path():
    context = _authorize(_context([{"plan_id": "G", "plan_name": "Plan G"}]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = "Does Plan G cover foreign travel emergencies?"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text="# Plan G\n\nForeign travel emergency care is covered at 80%.",
            )
        ],
        plans_found=["Plan G"],
        plans_searched_for=["Plan G"],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream):
        result = _call(
            context,
            plan_ids=["G"],
            queries=["Does foreign travel emergency care have coverage?"],
        )

    assert result.outcome == "candidates"
    assert result.required_response is None
    assert result.results


def test_adapter_keeps_medsup_benefit_amount_on_document_evidence_path():
    context = _authorize(_context([{"plan_id": "G", "plan_name": "Plan G"}]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = "What is the Part A deductible for Plan G?"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text="# Plan G\n\nPlan G pays the Medicare Part A deductible.",
            )
        ],
        plans_found=["Plan G"],
        plans_searched_for=["Plan G"],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream):
        result = _call(context, plan_ids=["G"], queries=["What is the Part A deductible?"])

    assert result.outcome == "candidates"
    assert result.required_response is None
    assert result.results


def test_adapter_uses_normalized_control_when_raw_plan_context_is_scrubbed():
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Anthem Prime (HMO-POS)"}]))
    scrubbed_context = dict(cast(Mapping[str, Any], context.request_context))
    scrubbed_context.pop("application_available_plans")
    context.request_context = scrubbed_context
    upstream = SearchPlansResponse(outcome="candidates")

    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(context)

    assert result.outcome == "candidates"
    rag_context = search.call_args.kwargs["context"]
    assert json.loads(rag_context.request_context["application_available_plans"]) == [
        {"plan_id": "P1", "plan_name": "Anthem Prime (HMO-POS)"}
    ]
    assert "application_available_plans" not in context.request_context


def test_minimal_response_caps_agent_visible_passages():
    upstream = Mock(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            SearchPlanResult(
                evidence_id=f"evidence-{index + 1}",
                rank=index + 1,
                score=0.5,
                plan_name="Plan",
                text=f"passage {index}",
                metadata=SearchPlanMetadata(
                    plan_name="Plan",
                    document_id=f"doc-{index}",
                    start_page=12,
                ),
            )
            for index in range(20)
        ],
        plans_found=["Plan"],
        plans_searched_for=["Plan"],
        coverage_complete=True,
        missing_plans=[],
        insufficiency_reason=None,
        error_message=None,
        no_results_reason=None,
        retrieval_method="hybrid",
        retrieval_time_ms=25,
        escalation={"type": False},
    )

    response = plan_search._response(upstream)

    assert len(response.results) == 10
    assert response.results[-1].text == "passage 9"
    assert response.results[0].metadata.start_page == 12


@pytest.mark.parametrize(
    "plan_count",
    [1, 2, 3, 4, 5],
)
def test_response_balances_the_plan_aware_agent_budget(plan_count):
    plans = [
        PlanSummary(plan_id=f"P{index}", plan_name=f"Plan {index}")
        for index in range(1, plan_count + 1)
    ]
    results = [
        SearchPlanResult(
            evidence_id=f"evidence-{plan_index}-{lane_rank}",
            plan_id=plan.plan_id,
            rank=(lane_rank - 1) * plan_count + plan_index,
            score=0.9,
            plan_name=plan.plan_name,
            text=f"{plan.plan_name} passage {lane_rank}",
            metadata=SearchPlanMetadata(
                plan_name=plan.plan_name,
                document_id=f"doc-{plan_index}-{lane_rank}",
            ),
        )
        for lane_rank in range(1, 11)
        for plan_index, plan in enumerate(plans, 1)
    ]
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan" if plan_count == 1 else "broad_plans",
        results=results,
        plans_found=[plan.plan_name for plan in plans],
        plans_searched_for=[plan.plan_name for plan in plans],
        coverage_complete=True,
    )

    response = plan_search._response(upstream, available=plans, selected=plans)

    passages_per_plan = [
        sum(result.plan_name == plan.plan_name for result in response.results) for plan in plans
    ]
    assert len(set(passages_per_plan)) == 1
    assert passages_per_plan[0] > 0
    assert [result.plan_name for result in response.results[:plan_count]] == [
        plan.plan_name for plan in plans
    ]
    assert len(response.results) <= plan_search.MAX_PLAN_PASSAGES


def test_plan_budget_uses_exact_ids_when_display_names_match():
    plans = [
        PlanSummary(plan_id="P1", plan_name="Shared Name"),
        PlanSummary(plan_id="P2", plan_name="Shared Name"),
    ]
    results = [
        SearchPlanResult(
            evidence_id=f"evidence-{plan.plan_id}-{rank}",
            plan_id=plan.plan_id,
            rank=(rank - 1) * 2 + plan_index,
            score=0.9,
            plan_name=plan.plan_name,
            text=f"{plan.plan_id} passage {rank}",
            metadata=SearchPlanMetadata(plan_name=plan.plan_name),
        )
        for rank in range(1, 11)
        for plan_index, plan in enumerate(plans, 1)
    ]

    selected = plan_search._bounded_plan_results(results, plans)

    passages_per_plan = [
        sum(result.plan_id == plan.plan_id for result in selected) for plan in plans
    ]
    assert set(result.plan_id for result in selected) == {"P1", "P2"}
    assert passages_per_plan[0] == passages_per_plan[1]


def test_plan_budget_does_not_backfill_a_sparse_lane():
    plans = [
        PlanSummary(plan_id="P1", plan_name="Plan 1"),
        PlanSummary(plan_id="P2", plan_name="Plan 2"),
    ]
    results = [
        SearchPlanResult(
            evidence_id=f"evidence-{plan.plan_id}-{rank}",
            plan_id=plan.plan_id,
            rank=rank,
            score=0.9,
            plan_name=plan.plan_name,
            text=f"{plan.plan_id} passage {rank}",
            metadata=SearchPlanMetadata(plan_name=plan.plan_name),
        )
        for plan in plans
        for rank in range(1, 3 if plan.plan_id == "P1" else 11)
    ]

    selected = plan_search._bounded_plan_results(results, plans)

    passages_per_plan = {
        plan.plan_id: sum(result.plan_id == plan.plan_id for result in selected) for plan in plans
    }
    assert passages_per_plan["P1"] == 2
    assert passages_per_plan["P2"] > passages_per_plan["P1"]
    assert len(selected) < len(results)


def _supplement_result(*, plan_name, text, rank=1):
    return SearchPlanResult(
        evidence_id=f"evidence-{rank}",
        rank=rank,
        score=0.9,
        plan_name=plan_name,
        text=text,
        metadata=SearchPlanMetadata(
            plan_name=plan_name,
            document_id=f"doc-{plan_name}",
            document_type="mols-medicare-supplement-plans",
        ),
    )


def test_adapter_withholds_query_relevant_conflicting_table_values():
    context = _authorize(_context([{"plan_id": "G", "plan_name": "Plan G"}]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = "For Plan G, who pays for the requested service?"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text=(
                    "# Plan G\n\n"
                    "| Requested service | Requested service | Requested service |\n"
                    "| Allowed amount | $0 | 100% |"
                ),
            ),
            _supplement_result(
                plan_name="Plan G",
                rank=2,
                text=(
                    "# Plan G\n\n"
                    "| Requested service | Requested service | Requested service |\n"
                    "| Allowed amount | $0 | All costs |"
                ),
            ),
        ],
        plans_found=["Plan G"],
        plans_searched_for=["Plan G"],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream):
        result = _call(
            context,
            plan_ids=["G"],
            queries=["Who pays for the requested service?"],
        )

    assert result.outcome == "insufficient"
    assert result.insufficiency_reason == (
        "Retrieved passages contain conflicting values for the requested plan benefit. "
        "The available evidence does not establish one answer."
    )
    assert result.business_intent == "specific_plan"
    assert result.results == []
    assert result.escalation["identification"] == "rag_insufficient_context"


def test_comparison_preserves_attributable_plain_text_for_every_selected_plan():
    plan_one = PlanSummary(plan_id="one", plan_name="Plan One")
    plan_two = PlanSummary(plan_id="two", plan_name="Plan Two")
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="broad_plans",
        results=[
            SearchPlanResult(
                evidence_id="one",
                rank=1,
                score=1,
                plan_name="Plan One",
                text="Inpatient visits cost $200 per day.",
                metadata=SearchPlanMetadata(plan_name="Plan One"),
            ),
            SearchPlanResult(
                evidence_id="two",
                rank=2,
                score=0.9,
                plan_name="Plan Two",
                text="Inpatient visits cost $265 per day.",
                metadata=SearchPlanMetadata(plan_name="Plan Two"),
            ),
        ],
        plans_found=["Plan One", "Plan Two"],
        plans_searched_for=["Plan One", "Plan Two"],
        coverage_complete=True,
    )

    response = plan_search._response(
        upstream,
        available=[plan_one, plan_two],
        selected=[plan_one, plan_two],
    )

    assert response.outcome == "candidates"
    assert [result.plan_name for result in response.results] == ["Plan One", "Plan Two"]
    assert "table_interpretation" not in response.model_dump()
    assert all("table_interpretation" not in result.model_dump() for result in response.results)


def test_response_adds_interpretation_guidance_once_for_retained_markdown_tables():
    plan = PlanSummary(plan_id="G", plan_name="Plan G")
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text=(
                    "# Plan G\n\n"
                    "| Services | Medicare pays | Plan pays | You pay |\n"
                    "| - | - | - | - |\n"
                    "| Foreign travel emergency care | $0 | 80% | 20% |"
                ),
            ),
            _supplement_result(
                plan_name="Plan G",
                rank=2,
                text=(
                    "# Plan G\n\n"
                    "| Service | Plan pays | You pay |\n"
                    "| - | - | - |\n"
                    "| Preventive care | 100% | $0 |"
                ),
            ),
        ],
        plans_found=["Plan G"],
        plans_searched_for=["Plan G"],
        coverage_complete=True,
    )

    response = plan_search._response(upstream, available=[plan], selected=[plan])

    assert response.outcome == "candidates"
    assert len(response.results) == 2
    assert response.table_interpretation == plan_search.TABLE_INTERPRETATION_GUIDANCE
    assert all("table_interpretation" not in result.model_dump() for result in response.results)
    assert response.model_dump_json().count(plan_search.TABLE_INTERPRETATION_GUIDANCE) == 1


def test_broad_plan_overview_is_distinct_from_a_focused_coverage_question():
    assert plan_search._is_broad_plan_overview(["What does Plan G cover?"])
    assert not plan_search._is_broad_plan_overview(["Does Plan G cover emergency care?"])


def test_adapter_preserves_attributable_cost_candidate_for_focused_tool_query():
    plan = {"plan_id": "P1", "plan_name": "Plan One"}
    context = _authorize(_context([plan]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = "What is my in-network specialist copay?"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            SearchPlanResult(
                evidence_id="specialist-copay",
                rank=1,
                score=1,
                plan_name="Plan One",
                text="In-network specialist visits have a $40 copay.",
                metadata=SearchPlanMetadata(plan_name="Plan One"),
            )
        ],
        plans_found=["Plan One"],
        plans_searched_for=["Plan One"],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream):
        result = _call(
            context,
            queries=["What cost sharing applies to an in-network specialist visit?"],
        )

    assert result.outcome == "candidates"
    assert [passage.text for passage in result.results] == [
        "In-network specialist visits have a $40 copay."
    ]


def test_plan_independent_query_removes_canonical_name_and_plan_type_alias():
    available = [PlanSummary(plan_id="P1", plan_name="Anthem Prime (HMO-POS)")]

    assert (
        plan_search._plan_independent_query(
            "What is the specialist copay for Anthem Prime?",
            available,
        )
        == "What is the specialist copay?"
    )
    assert (
        plan_search._plan_independent_query(
            "What's the specialist copay on Prime?",
            available,
        )
        == "What's the specialist copay?"
    )


def test_retrieval_query_enriches_only_non_drug_deductible_questions():
    individual = plan_search._retrieval_query("Compare the deductibles", "IND")
    medicare = plan_search._retrieval_query("Compare medical deductibles", "Medicare")

    assert "medical deductible amount" in individual
    assert "individual family schedule of cost share and benefits" in individual
    assert "medical deductible amount" in medicare
    assert "individual family" not in medicare
    assert (
        plan_search._retrieval_query("What is the Part D deductible?", "Medicare")
        == "What is the Part D deductible?"
    )


def test_applicability_conflict_requires_both_explicit_scopes_and_a_scope_question():
    results = [
        Mock(
            text="This plan covers up to $150 for eyewear.",
            metadata=SearchPlanMetadata(
                plan_name="Plan One", document_type="mols-medicare-advantage-plans"
            ),
        ),
        Mock(
            text="This package offers a $200 eyewear allowance.",
            metadata=SearchPlanMetadata(
                plan_name="Plan One", document_type="mols-medicare-advantage-plans"
            ),
        ),
    ]

    assert plan_search._has_applicability_conflict(
        results,
        "Is the eyewear allowance included in the base plan or an optional package?",
    )
    assert not plan_search._has_applicability_conflict(results, "What is the eyewear allowance?")


def test_response_leaves_semantic_relevance_to_retrieval_and_answering_layers():
    plan = PlanSummary(plan_id="one", plan_name="Plan One")
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            SearchPlanResult(
                evidence_id="travel-limit",
                rank=1,
                score=1,
                plan_name="Plan One",
                text=(
                    "## Emergency and Urgent Care Worldwide Coverage\n\nUrgent care and "
                    "emergency transportation have a $100,000 annual benefit limit."
                ),
                metadata=SearchPlanMetadata(plan_name="Plan One"),
            ),
            SearchPlanResult(
                evidence_id="emergency-copay",
                rank=2,
                score=0.9,
                plan_name="Plan One",
                text="## Emergency care\n\nEmergency room visits have a $115 copay.",
                metadata=SearchPlanMetadata(plan_name="Plan One"),
            ),
        ],
        plans_found=["Plan One"],
        plans_searched_for=["Plan One"],
        coverage_complete=True,
    )

    response = plan_search._response(
        upstream,
        available=[plan],
        selected=[plan],
        focused_query="What would urgent care cost?",
    )

    assert response.outcome == "candidates"
    assert [result.plan_name for result in response.results] == ["Plan One", "Plan One"]


def test_response_fails_closed_when_only_supplement_attribution_is_ambiguous():
    plan_g = PlanSummary(plan_id="G", plan_name="Plan G")
    plan_n = PlanSummary(plan_id="N", plan_name="Plan N")
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text="# Plan N (continued)\n\nYou pay office visit copayments.",
            )
        ],
        plans_found=["Plan G"],
        plans_searched_for=["Plan G"],
        coverage_complete=True,
    )

    response = plan_search._response(
        upstream,
        available=[plan_g, plan_n],
        selected=[plan_g],
    )

    assert response.outcome == "insufficient"
    assert response.results == []
    assert response.plans_found == []
    assert response.missing_plans == ["Plan G"]
    assert response.coverage_complete is False
    assert response.no_results_reason == "plan_attribution_ambiguous"


@pytest.mark.parametrize(
    ("document_type", "expected_reason"),
    [
        ("mols-medicare-supplement-plans", "medicare_supplement_comparison_unavailable"),
        ("iols-plans", "comparison_coverage_incomplete"),
    ],
)
def test_response_fails_closed_when_a_comparison_lacks_one_selected_plan(
    document_type, expected_reason
):
    plan_g = PlanSummary(plan_id="G", plan_name="Plan G")
    plan_n = PlanSummary(plan_id="N", plan_name="Plan N")
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="broad_plans",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text="# Plan G (continued)\n\nPlan pays 100% of Part B excess charges.",
            )
        ],
        plans_found=["Plan G", "Plan N"],
        plans_searched_for=["Plan G", "Plan N"],
        coverage_complete=True,
    )
    upstream.results[0].metadata.document_type = document_type

    response = plan_search._response(
        upstream,
        available=[plan_g, plan_n],
        selected=[plan_g, plan_n],
    )

    assert response.outcome == "insufficient"
    assert response.results == []
    assert response.plans_found == []
    assert response.missing_plans == ["Plan G", "Plan N"]
    assert response.coverage_complete is False
    assert response.no_results_reason == expected_reason


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("This plan covers up to $150 for eyewear.", "base_plan"),
        ("This package offers a $200 eyewear allowance.", "package_specific"),
        ("This package says this plan covers eyewear.", "package_specific"),
        ("Package 2: Dental and Vision Package", "package_specific"),
        ("Routine eyewear allowance: $150.", "unspecified"),
    ],
)
def test_passage_applicability_uses_only_explicit_passage_wording(text, expected):
    assert plan_search._passage_applicability(text) == expected


def test_response_drops_optional_package_passages_when_base_evidence_exists():
    plan = PlanSummary(plan_id="one", plan_name="Plan One")
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            SearchPlanResult(
                evidence_id="base",
                rank=1,
                score=1,
                plan_name="Plan One",
                text="This plan covers one routine vision exam with a $0 copay.",
                metadata=SearchPlanMetadata(
                    plan_name="Plan One", document_type="mols-medicare-advantage-plans"
                ),
            ),
            SearchPlanResult(
                evidence_id="package",
                rank=2,
                score=0.9,
                plan_name="Plan One",
                text="Optional Supplemental Benefits Package 2 includes $200 for eyewear.",
                metadata=SearchPlanMetadata(
                    plan_name="Plan One", document_type="mols-medicare-advantage-plans"
                ),
            ),
        ],
        plans_found=["Plan One"],
        plans_searched_for=["Plan One"],
        coverage_complete=True,
    )

    response = plan_search._response(
        upstream,
        available=[plan],
        selected=[plan],
        focused_query="Does my plan cover routine vision?",
    )

    assert [result.applicability for result in response.results] == ["base_plan"]
    assert "optional" not in response.results[0].text.casefold()


def test_adapter_withholds_conflicting_base_and_optional_package_applicability():
    plan = {"plan_id": "P1", "plan_name": "Plan One"}
    context = _authorize(_context([plan]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = (
        "Is the eyewear allowance included in the base plan or an optional package?"
    )
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            SearchPlanResult(
                evidence_id="base",
                rank=1,
                score=1,
                plan_name="Plan One",
                text="This plan covers up to $150 for eyewear.",
                metadata=SearchPlanMetadata(
                    plan_name="Plan One", document_type="mols-medicare-advantage-plans"
                ),
            ),
            SearchPlanResult(
                evidence_id="package",
                rank=2,
                score=0.9,
                plan_name="Plan One",
                text="This package offers a $200 eyewear allowance.",
                metadata=SearchPlanMetadata(
                    plan_name="Plan One", document_type="mols-medicare-advantage-plans"
                ),
            ),
        ],
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream):
        result = _call(
            context,
            queries=["Is the eyewear allowance in the base plan or an optional package?"],
        )

    assert result.outcome == "insufficient"
    assert result.business_intent == "specific_plan"
    assert "optional package variants" in result.insufficiency_reason


def test_adapter_accepts_distinct_ids_when_names_collide_after_unicode_normalization():
    context = _authorize(
        _context(
            [
                {"plan_id": "P1", "plan_name": "Anthem Prime (HMO-POS)"},
                {"plan_id": "P2", "plan_name": "Anthem\u202fPrime (HMO\u2011POS)"},
            ]
        )
    )
    upstream = SearchPlansResponse(outcome="candidates")
    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(context, plan_ids=["P1"])

    assert result.outcome == "candidates"
    assert search.call_args.kwargs["plan_ids"] == ["P1"]


def test_adapter_requires_plugin_owned_search_control():
    context = _context([{"plan_id": "P1", "plan_name": "Anthem Prime (HMO-POS)"}])

    with patch.object(plan_search.rag, "search_plans", Mock()) as search:
        result = _call(context)

    assert result.outcome == "error"
    assert "trusted search control" in result.error_message
    assert result.audit_completed is True
    assert result.escalation["identification"] == "Triggered by Guardrails - RAG Error"
    search.assert_not_called()


def test_adapter_accepts_bounded_wxo_double_encoded_control():
    context = _context([{"plan_id": "P1", "plan_name": "Anthem Prime (HMO-POS)"}])
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(
        json.dumps(
            {
                "route": "search_turn",
                "plan_lookup": [
                    {
                        "plan_id": "P1",
                        "plan_name": "Anthem Prime (HMO-POS)",
                        "current": False,
                        "recommended": False,
                    }
                ],
            }
        )
    )
    upstream = SearchPlansResponse(outcome="candidates")
    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(context)

    assert result.outcome == "candidates"
    assert search.call_count == 1


def test_adapter_accepts_neutral_control_and_uses_current_application_catalog():
    context = _context([{"plan_id": "P1", "plan_name": "Anthem Prime (HMO-POS)"}])
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps({"route": "search_turn"})
    upstream = SearchPlansResponse(outcome="candidates")
    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(context)

    assert result.outcome == "candidates"
    assert search.call_args.kwargs["plan_ids"] == ["P1"]


def test_adapter_rejects_malformed_trusted_plan_catalog():
    plan = {"plan_id": "P1", "plan_name": "Plan One"}
    context = _context([plan])
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(
        {"route": "search_turn", "plan_lookup": "not a list"}
    )

    with patch.object(plan_search.rag, "search_plans", Mock()) as search:
        result = _call(context)

    assert result.outcome == "error"
    search.assert_not_called()


def test_adapter_rejects_non_list_trusted_plan_catalog():
    context = _context([{"plan_id": "P1", "plan_name": "Plan One"}])
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(
        {"route": "search_turn", "plan_lookup": 42}
    )

    with patch.object(plan_search.rag, "search_plans") as search:
        result = _call(context)

    assert result.outcome == "error"
    assert result.error_message == "trusted plan catalog is missing"
    search.assert_not_called()


def test_adapter_rejects_id_outside_current_allowlist():
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Anthem Prime (HMO-POS)"}]))
    upstream = SearchPlansResponse(outcome="candidates")
    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(context, plan_ids=["P2"])

    assert result.outcome == "error"
    assert "outside the current allowlist" in result.error_message
    search.assert_not_called()


def test_adapter_accepts_multiple_allowlisted_plans_in_agent_selected_order():
    plans = [
        {"plan_id": "P1", "plan_name": "Plan One"},
        {"plan_id": "P2", "plan_name": "Plan Two"},
    ]
    context = _authorize(_context(plans))
    upstream = SearchPlansResponse(
        outcome="candidates",
        results=[
            SearchPlanResult(
                evidence_id=f"{plan['plan_id']}-deductible",
                rank=index,
                score=1,
                plan_name=plan["plan_name"],
                text="## Medical Deductible\n\nThe medical deductible is $0.",
                metadata=SearchPlanMetadata(plan_name=plan["plan_name"]),
            )
            for index, plan in enumerate(plans, start=1)
        ],
        plans_found=[plan["plan_name"] for plan in plans],
        plans_searched_for=[plan["plan_name"] for plan in plans],
        coverage_complete=True,
    )

    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = _call(
            context,
            plan_ids=["P2", "P1"],
            queries=["What is the medical deductible?"],
        )

    assert result.outcome == "candidates"
    assert search.call_args.kwargs["plan_ids"] == ["P2", "P1"]


def test_adapter_passes_exact_id_and_builds_plan_independent_search():
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Anthem Prime (HMO-POS)"}]))
    upstream = SearchPlansResponse(outcome="candidates")
    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = search_plans.fn(
            plan_ids=["P1"],
            queries=["What cost sharing applies to an in-network specialist visit?"],
            context=context,
        )

    assert result.outcome == "candidates"
    assert search.call_args.kwargs["plan_ids"] == ["P1"]
    facet = search.call_args.kwargs["searches"][0]
    assert facet.semantic_query == "What cost sharing applies to an in-network specialist visit?"
    assert facet.conditions == []
    assert facet.lexical_terms == []


def test_adapter_deterministically_removes_plan_identity_from_search_text():
    context = _authorize(
        _context(
            [
                {
                    "plan_id": "H0544-063-000",
                    "plan_name": "Anthem Medicare Advantage (HMO-POS)",
                }
            ]
        )
    )
    upstream = SearchPlansResponse(outcome="candidates")
    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = search_plans.fn(
            plan_ids=["H0544-063-000"],
            queries=[
                "What is the annual deductible for the Anthem Medicare Advantage HMO-POS plan?"
            ],
            context=context,
        )

    assert result.outcome == "candidates"
    retrieval_query = search.call_args.kwargs["query"]
    assert "Anthem Medicare Advantage" not in retrieval_query
    assert "HMO-POS" not in retrieval_query
    assert "annual deductible" in retrieval_query
    assert "medical deductible amount" in retrieval_query
    assert search.call_args.kwargs["searches"][0].semantic_query == retrieval_query


def test_adapter_rejects_a_query_containing_only_plan_identity():
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Plan One"}]))
    with patch.object(plan_search.rag, "search_plans", Mock()) as search:
        result = _call(context, queries=["Plan One"])

    assert result.outcome == "error"
    assert "contain a topic" in result.error_message
    search.assert_not_called()


def test_adapter_preserves_single_and_multiple_search_shapes():
    context = _authorize(
        _context(
            [
                {"plan_id": "P1", "plan_name": "Plan One"},
                {"plan_id": "P2", "plan_name": "Plan Two"},
            ]
        )
    )
    upstream = SearchPlansResponse(outcome="candidates")
    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        _call(context, plan_ids=["P1", "P2"])
        assert len(search.call_args.kwargs["searches"]) == 1

        _call(
            context,
            queries=[
                "What medical deductible applies?",
                "What annual out-of-pocket maximum applies?",
            ],
        )
        assert len(search.call_args.kwargs["searches"]) == 2


@pytest.mark.parametrize(
    ("queries", "expected_status"),
    [
        ([], "error"),
        ([""], "error"),
        ([f"query {index}" for index in range(11)], "insufficient"),
    ],
)
def test_adapter_rejects_invalid_queries(queries, expected_status):
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Plan One"}]))
    with patch.object(plan_search.rag, "search_plans", Mock()) as search:
        result = search_plans.fn(
            plan_ids=["P1"],
            queries=queries,
            context=context,
        )

    assert result.outcome == expected_status
    search.assert_not_called()


def test_adapter_audits_insufficient_upstream_evidence():
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Plan One"}]))
    upstream = SearchPlansResponse(
        outcome="insufficient",
        results=[
            SearchPlanResult(
                evidence_id="untrusted",
                rank=1,
                score=1,
                plan_name="Plan One",
                text="This passage was declared insufficient upstream.",
                metadata=SearchPlanMetadata(plan_name="Plan One"),
            )
        ],
        insufficiency_reason="The retrieved passages do not establish the requested fact",
    )
    with patch.object(
        plan_search.rag,
        "search_plans",
        return_value=upstream,
    ):
        result = _call(context)

    assert result.outcome == "insufficient"
    assert result.results == []
    assert result.required_response == plan_search.CANONICAL_NO_EVIDENCE_RESPONSE
    assert result.audit_completed is True
    assert result.escalation == {
        "type": True,
        "identification": "rag_insufficient_context",
        "description": "Plan search returned ambiguous or insufficient evidence",
    }
    assert "audit_payload" not in result.model_dump()


def test_adapter_fails_closed_when_required_audit_write_fails():
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Plan One"}]))
    upstream = SearchPlansResponse(outcome="candidates")
    with (
        patch.object(
            plan_search.rag,
            "search_plans",
            return_value=upstream,
        ),
        patch.object(
            plan_search,
            "emit_audit_event",
            side_effect=RuntimeError("audit sink rejected event"),
        ),
        pytest.raises(RuntimeError, match="audit sink rejected event"),
    ):
        _call(context)


def test_adapter_rejects_missing_runtime_context():
    """Test line 208: runtime context is required."""
    result = search_plans.fn(
        plan_ids=["P1"],
        queries=["What is the deductible?"],
        context=None,
    )
    assert result.outcome == "error"
    assert "runtime context is required" in result.error_message


def test_adapter_rejects_invalid_plan_ids_list():
    """The adapter still rejects non-list plan selectors."""
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Plan One"}]))

    result = search_plans.fn(
        plan_ids="P1",
        queries=["What is the deductible?"],
        context=context,
    )
    assert result.outcome == "error"
    assert "plan_ids must contain 0-100 plan IDs" in result.error_message


def test_empty_catalog_plan_search_fails_closed_without_calling_rag():
    context = _authorize(_context([]))

    with patch.object(plan_search.rag, "search_plans") as rag_search:
        result = _call(context, plan_ids=[])

    rag_search.assert_not_called()
    assert result.outcome == "insufficient"
    assert result.results == []
    assert result.no_results_reason == "plan_catalog_unavailable"
    assert result.required_response == plan_search.PLAN_CATALOG_UNAVAILABLE_MESSAGE
    assert result.business_intent is None
    assert result.audit_completed is True


def test_plan_search_without_a_selected_plan_requests_clarification():
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Plan One"}]))

    with patch.object(plan_search.rag, "search_plans") as rag_search:
        result = _call(context, plan_ids=[])

    rag_search.assert_not_called()
    assert result.outcome == "insufficient"
    assert result.no_results_reason == "clarification_required"
    assert "No plan was selected" in result.insufficiency_reason


def test_adapter_rejects_query_exceeding_max_length():
    """Test line 234: each query must be at most 1000 characters."""
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Plan One"}]))
    long_query = "x" * 1001

    result = search_plans.fn(
        plan_ids=["P1"],
        queries=[long_query],
        context=context,
    )
    assert result.outcome == "error"
    assert "at most 1000 characters" in result.error_message


def test_adapter_rejects_invalid_plan_id_in_list():
    """Test line 244: plan_ids must contain valid strings."""
    context = _authorize(_context([{"plan_id": "P1", "plan_name": "Plan One"}]))

    result = search_plans.fn(
        plan_ids=["P1", ""],
        queries=["What is the deductible?"],
        context=context,
    )
    assert result.outcome == "error"
    assert "invalid plan ID" in result.error_message


def test_adapter_rejects_duplicate_plan_ids():
    """Test line 250: plan_ids must contain unique plans."""
    context = _authorize(
        _context(
            [
                {"plan_id": "P1", "plan_name": "Plan One"},
                {"plan_id": "P2", "plan_name": "Plan Two"},
            ]
        )
    )

    result = search_plans.fn(
        plan_ids=["P1", "P1"],
        queries=["What is the deductible?"],
        context=context,
    )
    assert result.outcome == "error"
    assert "unique plans" in result.error_message


def test_adapter_collapses_duplicate_queries_before_search():
    context = _authorize(
        _context(
            [
                {"plan_id": "P1", "plan_name": "Plan One"},
                {"plan_id": "P2", "plan_name": "Plan Two"},
            ]
        )
    )
    upstream = SearchPlansResponse(outcome="candidates")

    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = search_plans.fn(
            plan_ids=["P1", "P2"],
            queries=[
                "What is the individual medical deductible?",
                "What is the individual medical deductible?",
            ],
            context=context,
        )

    assert result.outcome == "insufficient"
    assert len(search.call_args.kwargs["searches"]) == 1
    assert "individual medical deductible" in (
        search.call_args.kwargs["searches"][0].semantic_query
    )


def test_adapter_collapses_queries_that_duplicate_after_plan_identity_removal():
    context = _authorize(
        _context(
            [
                {"plan_id": "P1", "plan_name": "Plan One"},
                {"plan_id": "P2", "plan_name": "Plan Two"},
            ]
        )
    )
    upstream = SearchPlansResponse(outcome="candidates")

    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        result = search_plans.fn(
            plan_ids=["P1", "P2"],
            queries=[
                "What is the deductible for Plan One?",
                "What is the Plan Two deductible?",
            ],
            context=context,
        )

    assert result.outcome == "insufficient"
    assert len(search.call_args.kwargs["searches"]) == 1
    assert "What is the deductible" in search.call_args.kwargs["searches"][0].semantic_query


@pytest.mark.parametrize("document_type", ["iols-plans", None, "unrecognized"])
def test_non_medicare_evidence_bypasses_package_processing(document_type):
    plan = {"plan_id": "P1", "plan_name": "Plan One"}
    context = _authorize(_context([plan]))
    context.request_context["application_market_segment"] = "IND"
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = "Is eyewear included in the base plan or optional package?"
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        business_intent="specific_plan",
        results=[
            SearchPlanResult(
                evidence_id=str(rank),
                rank=rank,
                score=0.9,
                plan_name="Plan One",
                text=text,
                metadata=SearchPlanMetadata(plan_name="Plan One", document_type=document_type),
            )
            for rank, text in enumerate(
                [
                    "This package offers a $200 eyewear allowance.",
                    "This plan covers up to $150 for eyewear.",
                ],
                1,
            )
        ],
    )
    response = plan_search._response(
        upstream,
        available=[PlanSummary(**plan)],
        selected=[PlanSummary(**plan)],
        focused_query="What eyewear is covered?",
    )
    assert [r.text for r in response.results] == [r.text for r in upstream.results]
    assert [r.applicability for r in response.results] == ["unspecified", "unspecified"]
    assert response.response_instructions == plan_search.PLAN_EVIDENCE_ATTRIBUTION_INSTRUCTIONS
    with patch.object(plan_search.rag, "search_plans", return_value=upstream):
        result = _call(context, queries=[control["current_user_query"]])
    assert result.outcome == "candidates"
    assert len(result.results) == 2
    assert result.response_instructions == plan_search.PLAN_EVIDENCE_ATTRIBUTION_INSTRUCTIONS


@pytest.mark.parametrize(
    ("question", "expected_count"),
    [
        ("What Essential Extras are offered?", 6),
        ("Which Essential Extras can I choose?", 6),
        ("What are the Essential Extras rates and limits?", 6),
        ("What routine transportation benefits are offered?", 10),
        ("What extra benefits are offered?", 10),
        ("What routine transportation benefits are offered, and what Essential Extras?", 10),
        ("Compare Essential Extras with routine transportation.", 10),
        ("What are the Essential Extras and dental benefits?", 10),
        ("Compare base-plan benefits with Essential Extras.", 10),
    ],
)
def test_essential_extras_passage_budget_preserves_order_and_mixed_evidence(
    question, expected_count
):
    plan = {"plan_id": "P1", "plan_name": "Plan One"}
    context = _authorize(_context([plan]))
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = question
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    upstream = SearchPlansResponse(
        outcome="candidates",
        plans_found=["Plan One"],
        plans_searched_for=["Plan One"],
        results=[
            SearchPlanResult(
                evidence_id=str(i),
                plan_id="P1",
                rank=i,
                score=0.9,
                plan_name="Plan One",
                text=f"Passage {i}: Essential Extras information.",
                metadata=SearchPlanMetadata(plan_name="Plan One"),
            )
            for i in range(1, 11)
        ],
    )
    with patch.object(plan_search.rag, "search_plans", return_value=upstream):
        result = _call(context, queries=[question])
    assert [r.text for r in result.results] == [r.text for r in upstream.results[:expected_count]]
    assert [r.rank for r in result.results] == list(range(1, expected_count + 1))
    assert len(upstream.results) == 10
    assert result.coverage_complete


def test_supplement_comparison_restriction_blocks_split_retries_and_resets_next_turn():
    """Rebuilding trusted control on a new shopper turn restores single-plan retrieval."""
    context = _authorize(
        _context(
            [
                {"plan_id": "G", "plan_name": "Plan G"},
                {"plan_id": "N", "plan_name": "Plan N"},
            ]
        )
    )
    upstream = SearchPlansResponse(
        outcome="candidates",
        results=[
            _supplement_result(
                plan_name="Plan G",
                text=("# Plan G\n| Service | You pay |\n| - | - |\n| Office visit | $0 |"),
            )
        ],
    )
    with patch.object(plan_search.rag, "search_plans", return_value=upstream) as search:
        comparison = _call(context, plan_ids=["G", "N"], queries=["Office visit costs"])
        assert comparison.no_results_reason == "medicare_supplement_comparison_unavailable"
        assert comparison.results == []
        for plan_id in ["G", "N", "G"]:
            retry = _call(context, plan_ids=[plan_id], queries=["Office visit costs"])
            assert retry.required_response == comparison.required_response
            assert retry.results == []
        assert search.call_count == 1

        _authorize(context)
        selected = _call(context, plan_ids=["G"], queries=["Office visit costs"])
        assert search.call_count == 2
        assert selected.outcome == "candidates"
        assert "The member pays: $0." in selected.results[0].text
