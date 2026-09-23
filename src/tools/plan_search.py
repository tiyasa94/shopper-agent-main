"""Registered plan-search tool for the shopper agent."""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, cast

from ibm_watsonx_orchestrate.agent_builder.connections import (
    ConnectionType,
    ExpectedCredentials,
)
from ibm_watsonx_orchestrate.agent_builder.tools import ToolPermission, tool
from ibm_watsonx_orchestrate.run.context import AgentRun
from pydantic import Field

from shared import medicare_supplement, rag
from shared.audit import emit_audit_event, rag_audit_payload
from shared.context import (
    plan_identity_key,
    retrieval_text,
    trusted_plan_control,
)
from shared.guardrails import PLAN_CATALOG_UNAVAILABLE_MESSAGE
from shared.plan_evidence import (
    blatant_premium_amount_requested,
    filter_attributable_plan_results,
    has_query_relevant_table_conflict,
    results_require_focused_question,
)
from shared.plan_search_contract import (
    CANONICAL_NO_EVIDENCE_RESPONSE,
    PlanPassage,
    PlanSearchResponse,
)
from shared.rag import (
    RAG_APP_ID,
    RetrievalSearch,
    parse_available_plans,
)
from shared.response_addenda import capture_context_after_completion, record_response_addendum

RAG_CONNECTION_TYPE = cast(ConnectionType, cast(object, ConnectionType.KEY_VALUE))

logger = logging.getLogger(__name__)
EXTRA_BENEFITS_DOCUMENT_MESSAGE = (
    "Please refer to the plan documents for a full list of extra benefits."
)
ESSENTIAL_EXTRAS_DOCUMENT_MESSAGE = (
    "The information I found may not include every Essential Extras benefit. "
    "Please refer to the plan documents for the full list."
)
ESSENTIAL_EXTRAS_RESPONSE_INSTRUCTIONS = (
    "Check the premise before answering an Essential Extras question. The shopper calling "
    "something an 'Essential Extras allowance' does not establish that it belongs to the program. "
    "Apply these distinctions to the returned evidence:\n"
    "- An allowance under 'Additional benefits', 'Over-the-Counter Products', or 'Dental Services', "
    "without an explicit Essential Extras assignment, does not establish a program allowance. "
    "Do not answer yes or relabel that allowance.\n"
    "- Ordinary 'Transportation: Not Covered' does not establish that an Essential Extras transportation "
    "option is unavailable. A separate reference to that option does not transfer the ordinary "
    "transportation terms to it.\n"
    "- A paid dental/vision package does not establish an Essential Extras option or choice limit.\n"
    "If that is all the evidence provides, the requested program membership or terms are "
    "unestablished. Do not turn this into 'not covered', 'excluded', 'separate', or 'not part "
    "of Essential Extras': those are also factual claims requiring evidence. State that the "
    "documents do not establish whether the benefit belongs to Essential Extras. Return the "
    "unsupported response for that part. A quote of the ordinary "
    "benefit's amount does not support adding 'Essential Extras' in your summary. Keep any "
    "independently supported program facts.\n\n"
    "ESSENTIAL EXTRAS RULE: 'Essential Extras' is a named plan-document category, not a "
    "synonym for extra benefits. Chunked results may be incomplete or include nearby base-plan, "
    "optional supplemental, or SSBCI benefits. Report only benefits that the result text or "
    "section metadata explicitly identifies as belonging to Essential Extras for the selected "
    "plan. For each Essential Extras item, quote its supporting source sentence in Details. "
    "That sentence must either explicitly assign the fact to Essential Extras or appear under "
    "an Essential Extras heading in the same result. A heading in another result is not support. "
    "Base-plan benefits, copays, and limits do not apply to Essential Extras unless the source "
    "explicitly assigns those terms to Essential Extras. A statement that additional "
    "transportation is available through Essential Extras establishes only that the option "
    "exists. If its specific terms are unavailable, say so and omit surrounding base-plan "
    "amounts and conditions, including from quotations and parenthetical notes. Quote only "
    "the sentence establishing the Essential Extras option. When the user also asks about "
    "base-plan benefits, report their terms separately and label them as base-plan benefits. "
    "The Summary answer must not add facts "
    "beyond these supported statements. "
    "If none qualify, "
    "return required_response_if_unsupported exactly, without Summary answer or Details headings. "
    "Report the Essential Extras benefits supported by "
    "the retrieved information. Do not infer that other benefits are absent or unlisted because "
    "they were not found in the search results. The appended notice explains that the list may "
    "be incomplete. Leave completeness statements to that notice: do not add statements such as "
    "'No other benefits are identified' or 'The documents do not list other options.' Do not "
    "repeat the notice in the answer or use it as the Details content.\n\n"
    "A passage may describe two distinct benefits: an existing benefit with stated "
    "terms, and an additional option available through Essential Extras. For example, "
    "a transportation description followed by 'additional transportation benefits "
    "as part of Essential Extras' establishes that the additional option exists; "
    "it does not establish that the preceding transportation terms describe it. "
    "This distinction applies even if the passage never uses the words 'base plan.'\n\n"
    "Do not assume those benefits share terms merely because they appear together "
    "or cover the same service. 'Additional' also does not establish whether the "
    "Essential Extras option adds to, replaces, or otherwise changes the existing "
    "allowance. Unless the source explains that relationship, leave it unresolved.\n\n"
    "Report the supported option and say its specific terms are not established "
    "by the retrieved information. Keep the Summary answer within what the quoted "
    "Essential Extras evidence supports: a quote establishing availability cannot "
    "support adding a copay, allowance, or limit to the summary."
)
PLAN_COMPARISON_LIMIT_MESSAGE = "I can compare up to five plans at a time. Please choose up to five plans you'd like me to compare."
ESSENTIAL_EXTRAS_PREMIUM_INSTRUCTIONS = (
    "The requested fact is the relationship between premium and Essential Extras choices. "
    "A premium amount and a selection limit do not establish that relationship. If no passage "
    "explicitly explains whether premium affects the choice limit, say the relationship is "
    "unestablished. Never turn missing evidence into a claim that they are independent or that "
    "the limit is unchanged regardless of premium. Retain the supported structured premium "
    "and selection limit; the missing relationship does not invalidate those facts."
)
PLAN_EVIDENCE_ATTRIBUTION_INSTRUCTIONS = (
    "For a benefit and setting marked 'Not Covered', do not state a copay. Keep each amount "
    "attached to its explicitly labeled service and setting; a nearby amount does not override "
    "an exclusion. If an amount's service or setting is unclear, omit that amount and state "
    "the uncertainty."
)
TABLE_INTERPRETATION_GUIDANCE = (
    "Read Markdown tables by row, column, and group headers, combining every relevant row. Never "
    "describe a grouped benefit as uncovered when another row in that group shows a plan payment. "
    "Preserve payer, amount, threshold, timing, and eligibility qualifiers verbatim, and keep "
    "summaries consistent with details; if unclear, say the evidence is insufficient."
)
MEDICARE_PLAN_DOCUMENT_TYPES = frozenset(
    {
        "mols-medicare-advantage-plans",
        "mols-medicare-supplement-plans",
    }
)
MAX_TOOL_PLAN_IDS = 100
MAX_TOOL_QUERIES = 10
MAX_RETRIEVAL_SEARCHES = 4
_MARKDOWN_TABLE_ROW = re.compile(r"^\s*\|(?:[^|\n]*\|){2,}\s*$", flags=re.MULTILINE)

PLAN_SEARCH_DESCRIPTION = (
    "Search trusted plan documents for document-only plan facts such as named-drug coverage, "
    "benefit applicability, limits or allowances, exclusions, authorization requirements, "
    "eligibility conditions, and other document-defined facts or relationships. It returns "
    "plan-attributed candidate passages rather than authoritative structured quote values. Review "
    "each candidate for support of the same plan, requested fact, setting, and material conditions."
)
PLAN_IDS_DESCRIPTION = (
    "Array of one to five plan_id values for the active plan set. For each active plan_name, "
    "copy the plan_id character-for-character from the same catalog entry."
)
PLAN_QUERIES_DESCRIPTION = (
    "One to four self-contained questions about the current document-only fact or permitted "
    "fallback, applied to every selected plan. Use one question for one topic and deduplicate "
    "related wording."
)
MAX_PLAN_PASSAGES = 21
PLAN_PASSAGES_PER_LANE = {1: 10, 2: 8, 3: 7, 4: 5, 5: 4}


def _error(message: str) -> PlanSearchResponse:
    return PlanSearchResponse(
        outcome="error",
        error_message=message,
        required_response=CANONICAL_NO_EVIDENCE_RESPONSE,
        escalation={
            "type": True,
            "identification": "Triggered by Guardrails - RAG Error",
            "description": "RAG tool returned an error or empty result",
        },
    )


def _insufficient(message: str) -> PlanSearchResponse:
    """Return a non-retriable request-shape result that requires shopper clarification."""

    return PlanSearchResponse(
        outcome="insufficient",
        insufficiency_reason=message,
        required_response=CANONICAL_NO_EVIDENCE_RESPONSE,
        no_results_reason="clarification_required",
    )


def _is_medicare_passage(result: rag.SearchPlanResult) -> bool:
    return (result.metadata.document_type or "").strip().casefold() in MEDICARE_PLAN_DOCUMENT_TYPES


def _result_applicability(
    result: rag.SearchPlanResult,
) -> Literal["base_plan", "package_specific", "unspecified"]:
    if not _is_medicare_passage(result):
        return "unspecified"
    return _passage_applicability(result.text)


def _passage_applicability(text: str) -> Literal["base_plan", "package_specific", "unspecified"]:
    normalized = re.sub(r"\s+", " ", text).casefold()
    if re.search(
        r"\b(?:this package|optional supplemental(?: benefits)?|package \d+)\b",
        normalized,
    ):
        return "package_specific"
    if re.search(r"\bthis plan covers\b", normalized):
        return "base_plan"
    return "unspecified"


_FOCUSED_OVERVIEW_FACTS = {
    "ambulance",
    "copay",
    "copays",
    "coinsurance",
    "deductible",
    "deductibles",
    "dental",
    "drug",
    "emergency",
    "hearing",
    "hospital",
    "inpatient",
    "network",
    "outpatient",
    "pharmacy",
    "premium",
    "prescription",
    "primary",
    "specialist",
    "transportation",
    "urgent",
    "vision",
}


def _query_tokens(query: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", query.casefold()))


def _extra_benefits_addendum(*queries: str) -> str | None:
    """Return the document-completeness notice for broad supplemental-benefit wording."""

    text = " ".join(queries)
    if _is_essential_extras_request(text):
        return ESSENTIAL_EXTRAS_DOCUMENT_MESSAGE
    if re.search(r"\bextra[\s-]+benefits?\b", text, flags=re.IGNORECASE):
        return EXTRA_BENEFITS_DOCUMENT_MESSAGE
    return None


def _is_essential_extras_request(*queries: str) -> bool:
    """Identify the client's named Essential Extras plan-document category."""

    return bool(
        re.search(
            r"\bessential[\s-]+extras?\b",
            " ".join(queries),
            flags=re.IGNORECASE,
        )
    )


def _is_essential_extras_only_request(query: str) -> bool:
    """Limit category-only questions; preserve evidence for explicit other-benefit requests."""
    if not _is_essential_extras_request(query):
        return False
    if re.search(r"\b(?:base|standard)[ -]?(?:plan|benefits?|transportation)\b", query, re.I):
        return False
    clauses = re.split(r"\b(?:and|also|versus|vs\.?|as well as)\b|[;?]", query, flags=re.I)
    if re.search(r"\b(?:compare|difference|differ)\b", query, re.I):
        clauses = [
            part
            for clause in clauses
            for part in re.split(r"\b(?:with|from|to)\b", clause, flags=re.I)
        ]
    return not any(
        not _is_essential_extras_request(clause)
        and re.search(
            r"\b(?:routine|benefits?|coverage|copays?|transportation|dental|vision)\b", clause, re.I
        )
        for clause in clauses
    )


def _is_broad_plan_overview(queries: list[str]) -> bool:
    """Identify an unbounded request to summarize everything a plan covers."""

    if len(queries) != 1:
        return False
    normalized = " ".join(re.findall(r"[a-z0-9]+", queries[0].casefold()))
    tokens = set(normalized.split())
    broad_shape = bool(
        re.search(
            r"\b(?:what does (?:.+ )?cover|what is covered|what are (?:the )?benefits|"
            r"(?:coverage|benefits) (?:overview|summary)|summari[sz]e (?:the )?(?:coverage|benefits))\b",
            normalized,
        )
    )
    return broad_shape and not bool(tokens & _FOCUSED_OVERVIEW_FACTS)


def _has_applicability_conflict(results: Sequence[rag.SearchPlanResult], query: str) -> bool:
    """Detect base-versus-package evidence that the answering model must not collapse."""

    if not _query_tokens(query) & {"base", "included", "optional", "package"}:
        return False
    scopes = {_result_applicability(result) for result in results}
    return {"base_plan", "package_specific"}.issubset(scopes)


def _retrieval_query(query: str, market_segment: str) -> str:
    """Add deterministic document vocabulary for under-retrieved deductible tables."""

    tokens = _query_tokens(query)
    if not tokens & {"deductible", "deductibles"}:
        return query
    if tokens & {"drug", "drugs", "part", "pharmacy", "prescription", "prescriptions"}:
        return query
    suffix = "medical deductible amount"
    if market_segment.casefold() == "ind":
        suffix += " individual family schedule of cost share and benefits"
    return f"{query.rstrip(' .?!')}; {suffix}"


def _bounded_plan_results(
    results: Sequence[rag.SearchPlanResult], selected: Sequence[rag.PlanSummary] | None
) -> list[rag.SearchPlanResult]:
    """Apply the agent prompt budget by exact plan while preserving lane rank order."""

    if selected:
        plan_order = [plan.plan_id for plan in selected]
        plan_ids_by_name: dict[str, list[str]] = {}
        for plan in selected:
            name_key = plan_identity_key(plan.plan_name)
            plan_ids_by_name.setdefault(name_key, []).append(plan.plan_id)
    else:
        plan_order = []
        plan_ids_by_name = {}
        for result in results:
            result_plan_id = result.plan_id
            result_name = result.plan_name
            key = result_plan_id or plan_identity_key(result_name)
            if key not in plan_order:
                plan_order.append(key)
            plan_ids_by_name.setdefault(plan_identity_key(result_name), []).append(key)

    per_lane_limit = PLAN_PASSAGES_PER_LANE.get(len(plan_order), 0)
    lanes: dict[str, list[rag.SearchPlanResult]] = {key: [] for key in plan_order}
    for result in results:
        key = result.plan_id
        if key not in lanes:
            matching = list(
                dict.fromkeys(plan_ids_by_name.get(plan_identity_key(result.plan_name), []))
            )
            key = matching[0] if len(matching) == 1 else ""
        if key in lanes and len(lanes[key]) < per_lane_limit:
            lanes[key].append(result)
    return [
        lanes[key][rank]
        for rank in range(per_lane_limit)
        for key in plan_order
        if rank < len(lanes[key])
    ][:MAX_PLAN_PASSAGES]


def _response(
    response: rag.SearchPlansResponse,
    *,
    available: list[rag.PlanSummary] | None = None,
    selected: list[rag.PlanSummary] | None = None,
    focused_query: str | None = None,
    requested_fact: str = "",
    required_response_addendum: str | None = None,
    response_instructions: str | None = None,
    max_passages: int = MAX_PLAN_PASSAGES,
) -> PlanSearchResponse:
    if restriction := medicare_supplement.comparison_restriction(
        response, selected, requested_fact
    ):
        return restriction
    raw_results = response.results
    candidate_results = raw_results if response.outcome == "candidates" else []
    usable_results = filter_attributable_plan_results(candidate_results, available or [])
    if focused_query is not None and not _query_tokens(focused_query) & {
        "base",
        "optional",
        "package",
    }:
        base_results = [
            result for result in usable_results if _result_applicability(result) == "base_plan"
        ]
        if base_results:
            usable_results = [
                result
                for result in usable_results
                if _result_applicability(result) != "package_specific"
            ]
    selected_results = _bounded_plan_results(usable_results, selected)[:max_passages]
    outcome = response.outcome
    insufficiency_reason = response.insufficiency_reason
    no_results_reason = response.no_results_reason
    if outcome == "candidates" and raw_results and not selected_results:
        outcome = "insufficient"
        insufficiency_reason = (
            "Retrieved passages could not be safely attributed to the selected plans"
        )
        no_results_reason = "plan_attribution_ambiguous"

    plans_found = response.plans_found
    missing_plans = response.missing_plans
    coverage_complete = response.coverage_complete
    if selected is not None and response.outcome == "candidates":
        result_plan_ids = {result.plan_id for result in selected_results if result.plan_id}
        result_plan_keys = (
            set()
            if result_plan_ids
            else {plan_identity_key(result.plan_name) for result in selected_results}
        )
        plans_found = []
        missing_plans = []
        for plan in selected:
            found = (
                plan.plan_id in result_plan_ids
                if result_plan_ids
                else plan_identity_key(plan.plan_name) in result_plan_keys
            )
            (plans_found if found else missing_plans).append(plan.plan_name)
        coverage_complete = bool(selected_results) and not missing_plans
        if len(selected) > 1 and missing_plans:
            outcome = "insufficient"
            selected_results = []
            plans_found = []
            missing_plans = [plan.plan_name for plan in selected]
            coverage_complete = False
            insufficiency_reason = (
                "Retrieved passages could not support every selected plan in the comparison"
            )
            no_results_reason = "comparison_coverage_incomplete"

    return PlanSearchResponse(
        outcome=outcome,
        business_intent=response.business_intent,
        requested_fact=requested_fact,
        evidence_status=("unreviewed_candidates" if outcome == "candidates" else "unavailable"),
        table_interpretation=(
            TABLE_INTERPRETATION_GUIDANCE
            if outcome == "candidates"
            and any(_MARKDOWN_TABLE_ROW.search(result.text) for result in selected_results)
            else None
        ),
        response_instructions=(
            " ".join(
                part
                for part in (PLAN_EVIDENCE_ATTRIBUTION_INSTRUCTIONS, response_instructions)
                if part
            )
            if outcome == "candidates"
            else None
        ),
        results=[
            PlanPassage(
                rank=result.rank,
                score=result.score,
                plan_name=result.plan_name,
                applicability=_result_applicability(result),
                text=medicare_supplement.payment_text(result),
                metadata=result.metadata,
            )
            for result in selected_results
        ],
        plans_found=plans_found,
        plans_searched_for=response.plans_searched_for,
        coverage_complete=coverage_complete,
        missing_plans=missing_plans,
        insufficiency_reason=insufficiency_reason,
        error_message=response.error_message,
        no_results_reason=no_results_reason,
        required_response=(
            CANONICAL_NO_EVIDENCE_RESPONSE if outcome in {"insufficient", "error"} else None
        ),
        required_response_addendum=(
            required_response_addendum if outcome == "candidates" else None
        ),
        retrieval_method=response.retrieval_method,
        retrieval_time_ms=response.retrieval_time_ms,
        escalation=response.escalation,
    )


def _audited_response(response: PlanSearchResponse, runtime: AgentRun) -> PlanSearchResponse:
    """Complete the SOP-equivalent audit before exposing a retrieval response."""

    medicare_supplement.record_restriction(response, runtime)
    payload = rag_audit_payload(response.outcome, retrieval_scope="plan")
    receipt = emit_audit_event(payload, runtime, event_logger=logger)
    response.escalation = dict(receipt.escalation)
    response.audit_completed = receipt.logged
    record_response_addendum(
        runtime,
        response.required_response_addendum,
        unsupported_response=response.required_response_if_unsupported,
    )
    return response


def _plan_independent_query(query: str, available: Sequence[rag.PlanSummary]) -> str:
    """Remove catalog identities and dangling prepositions from the retrieval topic."""

    plan_tokens: list[str] = []
    for plan in available:
        plan_name = str(plan.plan_name or "").strip()
        plan_id = str(getattr(plan, "plan_id", "") or "").strip()
        short_name = re.sub(r"\s*\([^)]*\)\s*$", "", plan_name).strip()
        plan_tokens.extend(
            (
                plan_name,
                short_name,
                re.sub(r"^(?:Anthem|Wellpoint)\s+", "", short_name).strip(),
                plan_id,
            )
        )
    normalized = retrieval_text(query, plan_tokens, limit=1000)
    normalized = re.sub(
        r"\s+\b(?:for|of|under|from|in|on)\s*([?.!]?)$",
        r"\1",
        normalized,
        flags=re.IGNORECASE,
    ).strip()
    return normalized


@tool(
    name="search_plans",
    description=PLAN_SEARCH_DESCRIPTION,
    permission=ToolPermission.READ_ONLY,
    expected_credentials=[ExpectedCredentials(app_id=RAG_APP_ID, type=RAG_CONNECTION_TYPE)],
)
@capture_context_after_completion
async def search_plans(
    plan_ids: Annotated[
        list[str],
        Field(min_length=0, max_length=MAX_TOOL_PLAN_IDS, description=PLAN_IDS_DESCRIPTION),
    ],
    queries: Annotated[
        list[str],
        Field(min_length=1, max_length=MAX_TOOL_QUERIES, description=PLAN_QUERIES_DESCRIPTION),
    ],
    context: AgentRun,
) -> PlanSearchResponse:
    """Retrieve candidate passages for selected health plans.

    Args:
        context: Agent runtime context injected automatically; never pass it explicitly.
    """

    runtime = cast(AgentRun | None, context)
    if runtime is None or runtime.request_context is None:
        return _error("agent runtime context is required")
    validated_runtime = cast(AgentRun, runtime)
    request_context = cast(Mapping[str, Any], validated_runtime.request_context)

    def error(message: str) -> PlanSearchResponse:
        return _audited_response(_error(message), validated_runtime)

    def insufficient(
        message: str,
        *,
        business_intent: Literal["specific_plan", "broad_plans"] | None = None,
    ) -> PlanSearchResponse:
        response = _insufficient(message)
        response.business_intent = business_intent
        return _audited_response(response, validated_runtime)

    try:
        control = trusted_plan_control(validated_runtime)
        current_user_query = control.get("current_user_query")
        plan_catalog = control.get("plan_lookup")
        if plan_catalog is None:
            plan_catalog = request_context.get("application_available_plans")
        if isinstance(plan_catalog, str):
            plan_catalog = json.loads(plan_catalog)
        if not isinstance(plan_catalog, list):
            raise ValueError("trusted plan catalog is missing")
        catalog_items = [
            {
                "plan_id": plan.get("plan_id"),
                "plan_name": plan.get("plan_name"),
            }
            for plan in plan_catalog
            if isinstance(plan, dict)
        ]
        available = parse_available_plans(catalog_items) if catalog_items else []
    except (TypeError, ValueError) as exc:
        return error(str(exc))
    if not isinstance(plan_ids, list):
        return error(f"plan_ids must contain 0-{MAX_TOOL_PLAN_IDS} plan IDs")
    if len(plan_ids) > 5:
        requested_ids = {str(plan_id).strip() for plan_id in plan_ids}
        return _audited_response(
            PlanSearchResponse(
                outcome="insufficient",
                business_intent="broad_plans",
                plans_searched_for=[
                    plan.plan_name for plan in available if plan.plan_id in requested_ids
                ],
                coverage_complete=False,
                insufficiency_reason="More than five plans were selected for comparison",
                no_results_reason="plan_limit_exceeded",
                required_response=PLAN_COMPARISON_LIMIT_MESSAGE,
            ),
            validated_runtime,
        )
    if not available:
        return _audited_response(
            PlanSearchResponse(
                outcome="insufficient",
                no_results_reason="plan_catalog_unavailable",
                insufficiency_reason="The authoritative application plan catalog is empty",
                required_response=PLAN_CATALOG_UNAVAILABLE_MESSAGE,
            ),
            validated_runtime,
        )
    if not plan_ids:
        return insufficient(
            "No plan was selected from the authoritative application catalog. Do not call "
            "search_plans again this turn; ask the shopper to identify plans shown "
            "in the application without naming the full catalog."
        )

    if isinstance(queries, list) and len(queries) > MAX_TOOL_QUERIES:
        return insufficient(
            "The tool call exceeds ten document questions. Do not call search_plans again this "
            "turn; ask one concise clarification if the requested topic is not already narrow."
        )
    if not isinstance(queries, list) or not queries:
        return error("queries must contain 1-10 document questions")
    raw_queries: list[str] = []
    for raw_query in queries:
        if not isinstance(raw_query, str) or not raw_query.strip():
            return error("queries contains an invalid document question")
        normalized_query = raw_query.strip()
        if len(normalized_query) > 1000:
            return error("each query must contain at most 1000 characters")
        raw_queries.append(normalized_query)

    available_by_id = {plan.plan_id: plan for plan in available}
    selected = []
    selected_ids: set[str] = set()
    for raw_plan_id in plan_ids:
        if not isinstance(raw_plan_id, str) or not raw_plan_id.strip():
            return error("plan_ids contains an invalid plan ID")
        plan_id = raw_plan_id.strip()
        plan = available_by_id.get(plan_id)
        if plan is None:
            return error("plan_ids contains a plan outside the current allowlist")
        if plan_id in selected_ids:
            return error("plan_ids must contain unique plans")
        selected.append(plan)
        selected_ids.add(plan_id)

    if restriction := medicare_supplement.retry_restriction(control, selected):
        return _audited_response(restriction, validated_runtime)

    normalized_queries: list[str] = []
    premium_query_omitted = False
    for raw_query in raw_queries:
        normalized_query = _plan_independent_query(raw_query, available)
        if not normalized_query:
            return error("queries must contain a topic after plan identity is removed")
        if blatant_premium_amount_requested((raw_query, normalized_query)):
            premium_query_omitted = True
            continue
        if normalized_query.casefold() in {existing.casefold() for existing in normalized_queries}:
            continue
        normalized_queries.append(normalized_query)

    normalized_user_query = (
        _plan_independent_query(current_user_query, available)
        if isinstance(current_user_query, str) and current_user_query.strip()
        else ""
    )
    selection_only = control.get("selection_only") is True
    current_query_requests_premium = blatant_premium_amount_requested(
        (current_user_query, normalized_user_query)
        if isinstance(current_user_query, str)
        else (normalized_user_query,)
    )
    if not normalized_queries or (current_query_requests_premium and not premium_query_omitted):
        quote_recovery_message = control.get("structured_plan_quote_user_message")
        required_response = (
            quote_recovery_message
            if control.get("structured_plan_quote_available") is False
            and isinstance(quote_recovery_message, str)
            and quote_recovery_message.strip()
            else CANONICAL_NO_EVIDENCE_RESPONSE
        )
        return _audited_response(
            PlanSearchResponse(
                outcome="insufficient",
                business_intent="specific_plan" if len(selected) == 1 else "broad_plans",
                plans_searched_for=[plan.plan_name for plan in selected],
                coverage_complete=False,
                insufficiency_reason=(
                    "Premium amounts require get_plan_details and cannot be retrieved from plan "
                    "documents"
                ),
                no_results_reason="premium_requires_structured_details",
                required_response=required_response,
            ),
            validated_runtime,
        )
    scope_queries = (
        normalized_queries
        if selection_only or not normalized_user_query
        else [normalized_user_query]
    )
    if len(normalized_queries) > MAX_RETRIEVAL_SEARCHES:
        group_size = (
            len(normalized_queries) + MAX_RETRIEVAL_SEARCHES - 1
        ) // MAX_RETRIEVAL_SEARCHES
        normalized_queries = [
            "; ".join(normalized_queries[index : index + group_size])
            for index in range(0, len(normalized_queries), group_size)
        ]

    rag_runtime = AgentRun(
        request_context={
            **request_context,
            "application_available_plans": json.dumps(
                [{"plan_id": plan.plan_id, "plan_name": plan.plan_name} for plan in available]
            ),
        }
    )
    market_segment = str(request_context.get("application_market_segment", ""))
    retrieval_queries = [_retrieval_query(query, market_segment) for query in normalized_queries]
    response = await rag.search_plans(
        query="; ".join(retrieval_queries),
        searches=[RetrievalSearch(semantic_query=query) for query in retrieval_queries],
        plan_ids=[plan.plan_id for plan in selected],
        business_intent="specific_plan" if len(selected) == 1 else "broad_plans",
        context=rag_runtime,
    )
    if _is_broad_plan_overview(scope_queries) and results_require_focused_question(
        response.results
    ):
        return insufficient(
            "The Medicare Supplement booklet cannot safely support an unbounded coverage "
            "summary. Ask which specific benefit or service to explain.",
            business_intent=response.business_intent,
        )
    attributable_results = filter_attributable_plan_results(response.results, available)
    if normalized_user_query and has_query_relevant_table_conflict(
        attributable_results,
        normalized_user_query,
    ):
        return insufficient(
            "Retrieved passages contain conflicting values for the requested plan benefit. "
            "The available evidence does not establish one answer.",
            business_intent=response.business_intent,
        )
    if (
        len(selected) == 1
        and normalized_user_query
        and _has_applicability_conflict(
            attributable_results,
            normalized_user_query,
        )
    ):
        return insufficient(
            "Retrieved passages describe both a base-plan benefit and optional package variants. "
            "The available evidence does not establish one exclusive applicability answer.",
            business_intent=response.business_intent,
        )
    focused_query = normalized_queries[0] if len(normalized_queries) == 1 else None
    response_addendum = _extra_benefits_addendum(
        current_user_query if isinstance(current_user_query, str) else "",
        *raw_queries,
    )
    response_instructions = (
        ESSENTIAL_EXTRAS_RESPONSE_INSTRUCTIONS
        if _is_essential_extras_request(
            current_user_query if isinstance(current_user_query, str) else "",
            *raw_queries,
        )
        else None
    )
    relationship_question = " ".join(normalized_queries)
    if (
        response_instructions
        and re.search(r"\bpremiums?\b", relationship_question, flags=re.IGNORECASE)
        and re.search(
            r"\b(?:affect|chang\w*|depend\w*|independent|relationship|influence|determine)\b",
            relationship_question,
            flags=re.IGNORECASE,
        )
    ):
        response_instructions += "\n\n" + ESSENTIAL_EXTRAS_PREMIUM_INSTRUCTIONS
    category_query = (
        current_user_query
        if isinstance(current_user_query, str) and _is_essential_extras_request(current_user_query)
        else "; ".join(raw_queries)
    )
    return _audited_response(
        _response(
            response,
            available=available,
            selected=selected,
            focused_query=focused_query,
            requested_fact=(
                current_user_query.strip()
                if isinstance(current_user_query, str) and current_user_query.strip()
                else "; ".join(raw_queries)
            ),
            required_response_addendum=response_addendum,
            response_instructions=response_instructions,
            max_passages=6
            if _is_essential_extras_only_request(category_query)
            else MAX_PLAN_PASSAGES,
        ),
        validated_runtime,
    )
