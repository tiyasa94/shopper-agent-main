"""Medigap booklet evidence formatting and current-turn comparison restrictions."""

import json
from collections.abc import Mapping, Sequence
from typing import cast

from ibm_watsonx_orchestrate.run.context import AgentRun

from shared.context import SEARCH_CONTROL_CONTEXT_KEY, trusted_plan_control
from shared.plan_search_contract import PlanSearchResponse
from shared.rag import PlanSummary, SearchPlanResult, SearchPlansResponse
from shared.table_text import rewrite_markdown_tables

DOCUMENT_TYPE = "mols-medicare-supplement-plans"
INCOMPLETE_MEDSUPP_QUOTE_ADDENDUM = (
    "For accurate information about Medicare Supplement (MedSup) plans, go to the Medicare "
    "Supplement tab and provide your gender, date of birth, and Medicare Part A and Part B "
    "effective dates."
)
_RESTRICTION_REASON = "medicare_supplement_comparison_unavailable"
_BLOCKED_PLANS_KEY = "unavailable_supplement_comparison_plans"
_ROW_HEADERS = frozenset({"service", "services"})
_REQUIRED_HEADERS = frozenset({"you pay"})
_PAYMENT_LABELS = {
    "you pay": "The member pays",
    "plan pays": "The supplement plan pays",
    "medicare pays": "Medicare pays",
}


def _is_supplement(result: SearchPlanResult) -> bool:
    """Match the RAG-owned document type without inferring it from passage text."""
    return (result.metadata.document_type or "").strip().casefold() == DOCUMENT_TYPE


def payment_text(result: SearchPlanResult) -> str:
    """Label explicitly headed Medigap rows with their service, period, and payer.

    Preserve source values, prose, and footnotes without calculating coverage. Withhold
    unheaded or unsupported tables; leave other document families' text unchanged.
    """
    if not _is_supplement(result):
        return result.text
    return rewrite_markdown_tables(
        result.text,
        row_headers=_ROW_HEADERS,
        value_labels=_PAYMENT_LABELS,
        required_headers=_REQUIRED_HEADERS,
        group_label="Service",
        row_label="Applicable row or period",
        note=(
            "Payment evidence below copies only rows with explicit service and payer headers. "
            "Table rows without those headers are unavailable, not evidence of noncoverage. "
            "Each payment applies only to its stated service and row or period."
        ),
    )


def _comparison_unavailable(
    selected: Sequence[PlanSummary], requested_fact: str
) -> PlanSearchResponse:
    """Preserve structured facts while asking for a single-plan booklet question."""
    names = [plan.plan_name for plan in selected]
    return PlanSearchResponse(
        outcome="insufficient",
        business_intent="specific_plan" if len(selected) == 1 else "broad_plans",
        requested_fact=requested_fact,
        evidence_status="unavailable",
        plans_searched_for=names,
        missing_plans=names,
        no_results_reason=_RESTRICTION_REASON,
        insufficiency_reason=(
            "The retrieved Medicare Supplement booklet cannot reliably support a "
            "cross-plan comparison. Review one selected plan and benefit at a time. "
            "Do not retry or split this comparison into separate document calls. "
            "Wait for the shopper to select one plan. Retain independently requested "
            "facts from current structured tool results."
        ),
        required_response=(
            "I can review a specific Medicare Supplement benefit for one plan at a time. "
            "Which plan and benefit would you like to review?"
        ),
    )


def comparison_restriction(
    response: SearchPlansResponse,
    selected: Sequence[PlanSummary] | None,
    requested_fact: str,
) -> PlanSearchResponse | None:
    """Request one plan and benefit when all multi-plan results are Medigap booklets.

    These booklets cannot reliably support cross-plan cost comparisons. The restriction
    applies to this retrieval request, not every comparison phrasing or initially separate
    call; independently supported structured facts remain usable.
    """
    if (
        response.outcome == "candidates"
        and selected is not None
        and len(selected) > 1
        and response.results
        and all(_is_supplement(result) for result in response.results)
    ):
        return _comparison_unavailable(selected, requested_fact)
    return None


def record_restriction(response: PlanSearchResponse, runtime: AgentRun) -> None:
    """Remember affected plans in trusted control, which the next shopper turn replaces."""
    if response.no_results_reason == _RESTRICTION_REASON:
        control = trusted_plan_control(runtime)
        blocked = cast(list[str], control.get(_BLOCKED_PLANS_KEY, []))
        control[_BLOCKED_PLANS_KEY] = list(dict.fromkeys(blocked + response.plans_searched_for))
        runtime.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)


def retry_restriction(
    control: Mapping[str, object], selected: Sequence[PlanSummary]
) -> PlanSearchResponse | None:
    """Block retries for restricted plans, including split calls, before retrieval.

    The pre-invoke plugin resets trusted control each shopper turn, allowing a fresh
    single-plan lookup after the shopper selects a plan and benefit.
    """
    blocked = cast(list[str], control.get(_BLOCKED_PLANS_KEY, []))
    if any(plan.plan_name in blocked for plan in selected):
        query = control.get("current_user_query")
        return _comparison_unavailable(selected, query if isinstance(query, str) else "")
    return None
