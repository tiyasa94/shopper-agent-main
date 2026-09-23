"""Shared RAG API client and response contracts for shopper search tools."""

import json
import logging
import re
import time
from datetime import date
from typing import Any, Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from ibm_watsonx_orchestrate.run import connections
from ibm_watsonx_orchestrate.run.context import AgentRun
from pydantic import BaseModel, Field

RAG_APP_ID = "elevance-rag-tool-anthem"
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
MAX_TARGET_PLANS = 5
MAX_AVAILABLE_PLANS = 100
MAX_SEARCHES = 4

logger = logging.getLogger(__name__)


class PlanSummary(BaseModel):
    """One trusted plan from the runtime plan catalog."""

    plan_id: str
    plan_name: str


class RetrievalSearch(BaseModel):
    """One plan-independent dense and lexical retrieval facet."""

    semantic_query: str = Field(min_length=1, max_length=1000)
    requested_facts: list[str] = Field(default_factory=list, max_length=12)
    conditions: list[str] = Field(default_factory=list, max_length=12)
    lexical_terms: list[str] = Field(default_factory=list, max_length=16)


class SearchPlanMetadata(BaseModel):
    """Plan metadata consumed by the shopper API normalizer."""

    plan_name: str
    document_id: str = ""
    document_type: str | None = None
    chunk_id: str | None = None
    section_name: str | None = None
    start_page: int | None = None
    source_url: str | None = None


class SearchPlanResult(BaseModel):
    """One agent-facing document chunk with trusted plan attribution."""

    evidence_id: str
    plan_id: str = Field(default="", exclude=True)
    rank: int = Field(ge=1)
    score: float = Field(ge=0.0, le=1.0)
    text: str
    plan_name: str
    metadata: SearchPlanMetadata


class SearchPlansResponse(BaseModel):
    """Current agent-facing plan-evidence response."""

    outcome: Literal["candidates", "insufficient", "error"]
    business_intent: Literal["specific_plan", "broad_plans"] | None = None
    results: list[SearchPlanResult] = Field(default_factory=list)
    plans_found: list[str] = Field(default_factory=list)
    plans_searched_for: list[str] = Field(default_factory=list)
    coverage_complete: bool = False
    missing_plans: list[str] = Field(default_factory=list)
    insufficiency_reason: str | None = None
    error_message: str | None = None
    no_results_reason: str | None = None
    retrieval_method: str | None = None
    retrieval_time_ms: int | None = None
    escalation: dict[str, Any] = Field(default_factory=lambda: {"type": False})


def _error(message: str) -> SearchPlansResponse:
    """Return one stable tool error without exposing response documents."""
    return SearchPlansResponse(
        outcome="error",
        error_message=message,
        escalation={
            "type": True,
            "identification": "Triggered by Guardrails - RAG Error",
            "description": "RAG tool returned an error or empty result",
        },
    )


def normalize_request_id(value: Any) -> str:
    """Use a safe runtime correlation ID or create one when context is unusable."""
    candidate = str(value or "").strip()
    return candidate if REQUEST_ID_PATTERN.fullmatch(candidate) else str(uuid4())


def parse_available_plans(value: Any) -> list[PlanSummary]:
    """Parse and validate the authoritative runtime plan catalog."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError("application_available_plans is not valid JSON") from exc
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_AVAILABLE_PLANS:
        raise ValueError("application_available_plans must contain 1-100 plans")
    plans: list[PlanSummary] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("application_available_plans contains a non-object plan")
        plan_id = item.get("plan_id")
        plan_name = item.get("plan_name")
        if not isinstance(plan_id, str) or not plan_id.strip() or len(plan_id.strip()) > 128:
            raise ValueError("application_available_plans contains an invalid plan_id")
        if not isinstance(plan_name, str) or not plan_name.strip() or len(plan_name.strip()) > 512:
            raise ValueError("application_available_plans contains an invalid plan_name")
        normalized_id = plan_id.strip()
        if normalized_id in seen:
            raise ValueError("application_available_plans contains duplicate plan IDs")
        seen.add(normalized_id)
        plans.append(PlanSummary(plan_id=normalized_id, plan_name=plan_name.strip()))
    return plans


def _validate_plan_ids(plan_ids: list[str]) -> list[str]:
    """Validate exact agent-selected IDs without adding, removing, or reordering them."""
    if not isinstance(plan_ids, list) or not 1 <= len(plan_ids) <= MAX_TARGET_PLANS:
        raise ValueError("plan_ids must contain 1-5 exact plan IDs")
    if any(
        not isinstance(plan_id, str)
        or not plan_id
        or plan_id != plan_id.strip()
        or len(plan_id) > 128
        for plan_id in plan_ids
    ):
        raise ValueError("plan_ids contains an invalid exact plan ID")
    if len(set(plan_ids)) != len(plan_ids):
        raise ValueError("plan_ids must be unique")
    return list(plan_ids)


def effective_year_from_date(value: Any) -> int:
    """Derive the API year filter from the runtime effective date."""
    candidate = str(value or "").strip()
    if not candidate:
        raise ValueError("user_requested_eff_date is required")
    try:
        return date.fromisoformat(candidate).year
    except ValueError as exc:
        raise ValueError("user_requested_eff_date must use YYYY-MM-DD") from exc


def build_endpoint_url(base_url: str, path: str) -> str:
    """Build a reviewed HTTP endpoint from connection credentials."""
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("RAG_API_BASE_URL must be an HTTP origin")
    if not path.startswith("/") or "://" in path:
        raise ValueError("RAG endpoint must be an absolute URL path")
    return f"{base_url.rstrip('/')}{path}"


def _agent_results(
    results: list[Any], selected_plans: list[PlanSummary]
) -> tuple[list[SearchPlanResult], set[str]]:
    """Remove API internals and retain exact IDs for retrieval coverage checks."""
    selected_by_id = {plan.plan_id: plan for plan in selected_plans}
    found_ids: set[str] = set()
    normalized: list[SearchPlanResult] = []
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("result is not an object")
        rank = result.get("rank")
        score = result.get("score")
        text = result.get("text")
        metadata = result.get("metadata")
        plan_id = metadata.get("prop_plan_id") if isinstance(metadata, dict) else None
        document_id = metadata.get("document_id") if isinstance(metadata, dict) else ""
        plan = selected_by_id.get(plan_id) if isinstance(plan_id, str) else None
        if (
            not isinstance(rank, int)
            or rank < 1
            or not isinstance(score, int | float)
            or isinstance(score, bool)
            or not 0.0 <= float(score) <= 1.0
            or not isinstance(text, str)
            or not text.strip()
            or plan is None
        ):
            raise ValueError("result is missing trusted text or plan attribution")
        normalized.append(
            SearchPlanResult(
                evidence_id=f"evidence-{rank}",
                plan_id=plan.plan_id,
                rank=rank,
                score=float(score),
                text=text,
                plan_name=plan.plan_name,
                metadata=SearchPlanMetadata(
                    plan_name=plan.plan_name,
                    document_id=(
                        document_id.strip()
                        if isinstance(document_id, str) and len(document_id.strip()) <= 512
                        else ""
                    ),
                    document_type=(
                        str(metadata["document_type"])
                        if isinstance(metadata, dict) and metadata.get("document_type")
                        else None
                    ),
                    chunk_id=(
                        str(metadata["chunk_id"])
                        if isinstance(metadata, dict) and metadata.get("chunk_id") is not None
                        else None
                    ),
                    section_name=(
                        str(metadata["section_name"])
                        if isinstance(metadata, dict) and metadata.get("section_name")
                        else None
                    ),
                    start_page=(
                        metadata.get("start_page")
                        if isinstance(metadata, dict)
                        and isinstance(metadata.get("start_page"), int)
                        else None
                    ),
                    source_url=(
                        str(metadata["source_url"])
                        if isinstance(metadata, dict) and metadata.get("source_url")
                        else None
                    ),
                ),
            )
        )
        found_ids.add(plan.plan_id)
    return normalized, found_ids


async def search_plans(
    query: str,
    searches: list[RetrievalSearch],
    plan_ids: list[str],
    business_intent: Literal["specific_plan", "broad_plans"],
    context: AgentRun,
) -> SearchPlansResponse:
    """Search official documents for one to five exact plans available in the session.

    The registered plan-search adapter calls this client only after trusted pre-invoke control.
    The client preserves selected plan order and rejects IDs outside the authoritative catalog.

    Args:
        query: Shopper question retained for traceability and evidence assessment.
        searches: One to four plan-independent semantic and lexical retrieval facets.
        plan_ids: Ordered exact plan IDs selected by the agent from trusted control.
        business_intent: Shopper API routing label for upstream observability; it is not forwarded
            to the RAG API.
        context: Agent runtime context injected automatically; never pass it explicitly.

    Returns:
        SearchPlansResponse: Ranked document chunks or a stable retrieval error.
    """
    started = time.perf_counter()
    runtime_context = cast(AgentRun | None, context)
    if runtime_context is None:
        return _error("agent runtime context is required")
    ctx = runtime_context.request_context or {}
    request_id = normalize_request_id(ctx.get("request_id", ""))
    normalized_query = str(query or "").strip()
    if not normalized_query or len(normalized_query) > 2000:
        return _error("query must contain 1-2000 characters")
    if business_intent not in {"specific_plan", "broad_plans"}:
        return _error("business_intent must be specific_plan or broad_plans")
    normalized_searches: list[RetrievalSearch] = []
    for raw_search in searches if isinstance(searches, list) else []:
        try:
            search = (
                raw_search
                if isinstance(raw_search, RetrievalSearch)
                else RetrievalSearch.model_validate(raw_search)
            )
        except (TypeError, ValueError):
            return _error("searches contains an invalid retrieval search")
        normalized_searches.append(search)
    if not 1 <= len(normalized_searches) <= MAX_SEARCHES:
        return _error("searches must contain 1-4 retrieval searches")
    try:
        selected_ids = _validate_plan_ids(plan_ids)
        available_plans = parse_available_plans(ctx.get("application_available_plans", "[]"))
        effective_year = effective_year_from_date(ctx.get("user_requested_eff_date", ""))
    except ValueError as exc:
        return _error(str(exc))

    available_by_id = {plan.plan_id: plan for plan in available_plans}
    unavailable = [plan_id for plan_id in selected_ids if plan_id not in available_by_id]
    if unavailable:
        return _error("Selected plan IDs are not available in the current session")
    selected_plans = [available_by_id[plan_id] for plan_id in selected_ids]
    plans_searched_for = [plan.plan_name for plan in selected_plans]

    def retrieval_error(message: str) -> SearchPlansResponse:
        """Retain resolved plan context when retrieval cannot produce evidence."""

        result = _error(message)
        result.business_intent = business_intent
        result.plans_searched_for = list(plans_searched_for)
        result.missing_plans = list(plans_searched_for)
        return result

    segment = str(ctx.get("application_market_segment", "") or "").strip().lower()
    if segment == "medicare":
        collection = "MOLS"
    elif segment == "ind":
        collection = "IOLS"
    else:
        return retrieval_error("application_market_segment must be IND or Medicare")

    language = str(ctx.get("user_language", "en") or "en").strip()
    state_code = str(ctx.get("user_state_code", "") or "").strip()
    if not language or len(language) > 10:
        return retrieval_error("user_language must contain 1-10 characters")
    if len(state_code) > 16:
        return retrieval_error("user_state_code must contain at most 16 characters")

    prospect_type = str(ctx.get("prospect_type", "") or "").strip().lower()
    user_type = "member" if prospect_type == "member" else "prospect"
    payload: dict[str, Any] = {
        "query": normalized_query,
        "searches": [search.model_dump() for search in normalized_searches],
        "plan_ids": selected_ids,
        "available_plans": [plan.model_dump() for plan in available_plans],
        "baseline_plan_id": selected_ids[0],
        "effective_year": effective_year,
        "language": language,
        "user_type": user_type,
    }
    if state_code:
        payload["state_code"] = state_code
    if collection == "IOLS":
        exchange = str(ctx.get("application_exchange_indicator", "") or "").strip()
        if not exchange or len(exchange) > 20:
            return retrieval_error("application_exchange_indicator is required for IND")
        payload["exchange_indicator"] = exchange

    connection = connections.key_value(RAG_APP_ID)
    base_url = str(connection.get("RAG_API_BASE_URL", "") or "").strip()
    if not base_url:
        return retrieval_error("RAG_API_BASE_URL is not configured")
    api_key = str(connection.get("RAG_API_KEY", "") or "").strip()
    if not api_key:
        return retrieval_error("RAG_API_KEY is not configured")
    endpoint_key = "RAG_MOLS_ENDPOINT" if collection == "MOLS" else "RAG_IOLS_ENDPOINT"
    endpoint_path = str(connection.get(endpoint_key, "") or "").strip()
    if not endpoint_path:
        return retrieval_error(f"{endpoint_key} is not configured")
    try:
        url = build_endpoint_url(base_url, endpoint_path)
    except ValueError as exc:
        return retrieval_error(str(exc))

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-API-Key": api_key,
                },
                json=payload,
                timeout=30,
            )
    except httpx.RequestError:
        return retrieval_error("RAG API request failed")

    logger.info(
        "rag_response_received",
        extra={
            "request_id": request_id,
            "downstream_request_id": response.headers.get("X-Request-ID"),
            "status_code": response.status_code,
        },
    )

    try:
        data = response.json()
    except ValueError:
        return retrieval_error(f"RAG API returned non-JSON HTTP {response.status_code}")
    if not isinstance(data, dict):
        return retrieval_error("RAG API returned an invalid response object")
    if not 200 <= response.status_code < 300:
        detail = data.get("message") or data.get("detail") or data.get("error") or "request failed"
        return retrieval_error(f"RAG API returned HTTP {response.status_code}: {detail}")

    results = data.get("results")
    has_results = data.get("has_results")
    metadata = data.get("metadata")
    declared_quality = metadata.get("evidence_quality") if isinstance(metadata, dict) else None
    evidence_reason = metadata.get("evidence_reason") if isinstance(metadata, dict) else None
    if (
        not isinstance(results, list)
        or not isinstance(has_results, bool)
        or has_results != bool(results)
        or data.get("collection") != collection
        or declared_quality not in {None, "sufficient", "insufficient"}
        or (evidence_reason is not None and not isinstance(evidence_reason, str))
    ):
        return retrieval_error(
            f"RAG API response contract validation failed — "
            f"HTTP {response.status_code} collection={data.get('collection')!r} "
            f"expected={collection!r} "
            f"has_results={has_results!r} results_type={type(results).__name__}"
        )

    if not results:
        result = SearchPlansResponse(
            outcome="error",
            business_intent=business_intent,
            plans_searched_for=plans_searched_for,
            missing_plans=plans_searched_for,
            error_message="RAG API returned no matching plan documents",
            no_results_reason=data.get("no_results_reason"),
            retrieval_method=(str(data.get("strategy")) if data.get("strategy") else None),
            retrieval_time_ms=_retrieval_time_ms(data),
            escalation={
                "type": True,
                "identification": "Triggered by Guardrails - RAG Error",
                "description": "RAG tool returned an error or empty result",
            },
        )
        _log_retrieval(result, request_id, started, len(selected_plans))
        return result

    try:
        agent_results, found_ids = _agent_results(results, selected_plans)
    except ValueError as exc:
        return retrieval_error(f"RAG API response contract validation failed — {exc}")
    plans_found = [plan.plan_name for plan in selected_plans if plan.plan_id in found_ids]
    missing_plans = [plan.plan_name for plan in selected_plans if plan.plan_id not in found_ids]
    insufficient = bool(results) and (bool(missing_plans) or declared_quality == "insufficient")
    if insufficient and missing_plans:
        insufficiency_reason = "No evidence was returned for every requested plan"
    elif insufficient:
        insufficiency_reason = (
            evidence_reason or "Retrieved evidence does not clearly answer the query"
        )
    else:
        insufficiency_reason = None
    result = SearchPlansResponse(
        outcome="insufficient" if insufficient else "candidates",
        business_intent=business_intent,
        results=agent_results,
        plans_found=plans_found,
        plans_searched_for=plans_searched_for,
        coverage_complete=not missing_plans,
        missing_plans=missing_plans,
        insufficiency_reason=insufficiency_reason,
        no_results_reason=data.get("no_results_reason"),
        retrieval_method=(str(data.get("strategy")) if data.get("strategy") else None),
        retrieval_time_ms=_retrieval_time_ms(data),
        escalation=(
            {
                "type": True,
                "identification": "rag_insufficient_context",
                "description": "Plan search returned ambiguous or insufficient evidence",
            }
            if insufficient
            else {"type": False}
        ),
    )
    _log_retrieval(result, request_id, started, len(selected_plans))
    return result


def _retrieval_time_ms(data: dict[str, Any]) -> int | None:
    metrics = data.get("metrics")
    value = metrics.get("total_time_ms") if isinstance(metrics, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return int(value)


def _log_retrieval(
    result: SearchPlansResponse,
    request_id: str,
    started: float,
    plan_count: int,
) -> None:
    logger.info(
        "shopper_retrieval_completed",
        extra={
            "request_id": request_id,
            "retrieval_scope": "plan",
            "business_intent": result.business_intent,
            "outcome": result.outcome,
            "plan_count": plan_count,
            "result_count": len(result.results),
            "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
        },
    )
