"""Registered general-document search tool for the shopper agent."""

import asyncio
import logging
import time
from typing import Annotated, Any, Literal, cast

import requests
from ibm_watsonx_orchestrate.agent_builder.connections import (
    ConnectionType,
    ExpectedCredentials,
)
from ibm_watsonx_orchestrate.agent_builder.tools import ToolPermission, tool
from ibm_watsonx_orchestrate.run import connections
from ibm_watsonx_orchestrate.run.context import AgentRun
from pydantic import BaseModel, Field

from shared.audit import emit_audit_event, rag_audit_payload
from shared.context import (
    MAX_AGENT_PASSAGES,
    trusted_general_control,
)
from shared.effective_date import is_personal_effective_date_query
from shared.rag import (
    RAG_APP_ID,
    RetrievalSearch,
    build_endpoint_url,
    effective_year_from_date,
    normalize_request_id,
)

RAG_CONNECTION_TYPE = cast(ConnectionType, cast(object, ConnectionType.KEY_VALUE))

logger = logging.getLogger(__name__)

GENERAL_SEARCH_DESCRIPTION = (
    "Search trusted general health-insurance documents for factual plan-independent definitions, "
    "plan types, processes, and ways to manage health-care costs. Use this tool before answering "
    "those factual questions rather than relying on model memory. Coverage and cost questions "
    "about available plans, including all plans, belong to the plan tools. Do not broaden a "
    "catalog benefit question into an insurance-wide question. Returned passages are candidate "
    "evidence; passages about related concepts do not establish the requested fact."
)
GENERAL_QUERIES_DESCRIPTION = (
    "One to five self-contained questions about the shopper's current plan-independent request."
)
CANONICAL_NO_EVIDENCE_RESPONSE = "The available documents do not establish the requested fact."
GENERAL_CANDIDATE_RESPONSE_INSTRUCTIONS = (
    "For a personal rating question, answer whether the requested factor affects this "
    "shopper's rate, not whether it appears in a general list of premium factors. Before "
    "asserting an effect, quote the rule and establish its applicability to the shopper's "
    "state, insurance product, and each activity asked about. A general mention of tobacco "
    "does not establish treatment of vaping or e-cigarettes. If the results provide only "
    "general cost factors, the personal rate effect is unsupported: return "
    "required_response_if_unsupported for that part. Do not answer yes or substitute "
    "'generally increases', 'can affect', or 'may raise' for the missing applicable rule. "
    "Do not infer no effect either. For a general educational question about premium "
    "factors, report supported general information without claiming personal applicability.\n\n"
    "Review the original_user_query and executed_searches below. The search terms are retrieval "
    "aids, not a replacement for the user's question. The results below are UNVALIDATED candidate "
    "chunks, not established answers. Determine whether their text directly supports the user's "
    "requested fact and material conditions. Related topics, document titles, ranking scores, and "
    "absence of a rule do not establish that rule or its opposite. Do not fill evidence gaps from "
    "model memory or extend a rule to products or conditions the chunks do not address. "
    "For eligibility or application questions, distinguish who qualifies from when enrollment "
    "is allowed. Report a qualification requirement or exception only when a retrieved sentence "
    "explicitly assigns it to the requested plan type or program, and quote that supporting "
    "sentence in Details. Enrollment periods and catalog availability alone do not establish "
    "eligibility. If eligibility is not established, say so; do not turn enrollment timing or "
    "a listed product into a claim that the shopper qualifies or can apply. If supported, "
    "answer using only those supported facts. If no requested part is supported, return "
    "required_response_if_unsupported exactly, without Summary answer or Details headings. If only "
    "part is supported, answer that part and identify the unsupported part without guessing."
)
MEDICARE_CANDIDATE_RESPONSE_INSTRUCTIONS = (
    GENERAL_CANDIDATE_RESPONSE_INSTRUCTIONS
    + " Check Medicare eligibility separately for Part C, Part D, and Medicare Supplement. "
    'An "A or B" source rule must remain "A or B," even if a neighboring program requires both. '
    "Never combine these products into a shared enrollment requirement."
)


class GeneralDocumentMetadata(BaseModel):
    """Trusted metadata retained from a general-corpus passage."""

    document_id: str
    document_type: str | None = None
    chunk_id: str | None = None
    section_name: str | None = None
    start_page: int | None = None
    source_url: str | None = None


class GeneralDocumentPassage(BaseModel):
    """One general-corpus passage exposed to the answer model and public normalizer."""

    rank: int = Field(ge=1)
    score: float = Field(ge=0.0, le=1.0)
    text: str
    metadata: GeneralDocumentMetadata


class GeneralSearchResponse(BaseModel):
    """Source-owned result for plan-independent shopper education."""

    response_instructions: str | None = None
    original_user_query: str = ""
    executed_searches: list[str] = Field(default_factory=list)
    candidate_validation_status: Literal["UNVALIDATED", "UNAVAILABLE"] = "UNAVAILABLE"
    outcome: Literal["candidates", "insufficient", "error"] = Field(
        description=(
            "candidates means retrieval returned possible evidence; it does not establish that "
            "the passages answer the question. insufficient and error contain no evidence and "
            "must not be supplemented from conversation history"
        )
    )
    business_intent: Literal["generic_info"] = "generic_info"
    requested_fact: str = Field(
        default="",
        description="Trusted current-turn fact that candidate passages must directly establish",
    )
    evidence_status: Literal["unreviewed_candidates", "unavailable"] = "unavailable"
    required_response_if_unsupported: str = Field(
        default=CANONICAL_NO_EVIDENCE_RESPONSE,
        description=(
            "For outcome=candidates only: use this response after reviewing the candidate "
            "passages in this same tool response and finding that they do not directly "
            "establish requested_fact; passages about related categories are insufficient. If "
            "another current tool result supports an independently requested part, use this "
            "response only for the unsupported part"
        ),
    )
    results: list[GeneralDocumentPassage] = Field(default_factory=list)
    plans_found: list[str] = Field(default_factory=list)
    plans_searched_for: list[str] = Field(default_factory=list)
    retrieval_method: str | None = None
    retrieval_time_ms: int | None = None
    insufficiency_reason: str | None = None
    error_message: str | None = None
    no_results_reason: str | None = None
    required_response: str | None = Field(
        default=None,
        description=(
            "When present, evidence for this tool request is unavailable. This fallback is scoped "
            "to this tool request and must never discard an independently requested part supported "
            "by another current tool result. When no such supported part exists, the final response "
            "must equal this text verbatim without headings or additions"
        ),
    )
    escalation: dict[str, Any] = Field(default_factory=lambda: {"type": False})
    audit_completed: bool = False


def _error(message: str) -> GeneralSearchResponse:
    return GeneralSearchResponse(
        outcome="error",
        error_message=message,
        required_response=CANONICAL_NO_EVIDENCE_RESPONSE,
        escalation={
            "type": True,
            "identification": "Triggered by Guardrails - RAG Error",
            "description": "General document search returned an error or empty result",
        },
    )


def _normalized_queries(queries: list[str]) -> list[str]:
    """Validate and deduplicate the agent's bounded general-information expansion."""

    if not isinstance(queries, list) or not 1 <= len(queries) <= 5:
        raise ValueError("queries must contain 1-5 strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for query in queries:
        if not isinstance(query, str):
            raise ValueError("queries must contain 1-5 strings")
        value = query.strip()
        if not value or len(value) > 1000:
            raise ValueError("each query must contain 1-1000 characters")
        key = value.casefold()
        if key not in seen:
            normalized.append(value)
            seen.add(key)
    return normalized


def _passages(results: list[Any], brand: str) -> list[GeneralDocumentPassage]:
    """Validate brand-scoped results and retain only agent-relevant metadata."""

    passages: list[GeneralDocumentPassage] = []
    for result in results[:MAX_AGENT_PASSAGES]:
        if not isinstance(result, dict):
            raise ValueError("result is not an object")
        rank = result.get("rank")
        score = result.get("score")
        text = result.get("text")
        metadata = result.get("metadata")
        document_id = metadata.get("document_id") if isinstance(metadata, dict) else None
        prop_brand = metadata.get("prop_brand") if isinstance(metadata, dict) else None
        if (
            not isinstance(rank, int)
            or rank < 1
            or isinstance(score, bool)
            or not isinstance(score, int | float)
            or not 0 <= float(score) <= 1
            or not isinstance(text, str)
            or not text.strip()
            or not isinstance(document_id, str)
            or not document_id.strip()
            or prop_brand != brand
        ):
            raise ValueError("result is missing trusted document evidence")
        passages.append(
            GeneralDocumentPassage(
                rank=rank,
                score=float(score),
                text=text,
                metadata=GeneralDocumentMetadata(
                    document_id=document_id.strip(),
                    document_type=(
                        str(metadata["document_type"]) if metadata.get("document_type") else None
                    ),
                    chunk_id=(
                        str(metadata["chunk_id"]) if metadata.get("chunk_id") is not None else None
                    ),
                    section_name=(
                        str(metadata["section_name"]) if metadata.get("section_name") else None
                    ),
                    start_page=(
                        metadata.get("start_page")
                        if isinstance(metadata.get("start_page"), int)
                        else None
                    ),
                    source_url=(
                        str(metadata["source_url"]) if metadata.get("source_url") else None
                    ),
                ),
            )
        )
    return passages


def _retrieval_time_ms(data: dict[str, Any]) -> int | None:
    metrics = data.get("metrics")
    value = metrics.get("total_time_ms") if isinstance(metrics, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return int(value)


def _policy_insufficiency_reason(query: str) -> str | None:
    """Enforce deterministic policy without judging passage semantics by wording."""

    if is_personal_effective_date_query(query):
        return (
            "A personal coverage start date cannot be established without the applicable "
            "enrollment period and circumstances"
        )
    return None


def _log(result: GeneralSearchResponse, request_id: str, started: float) -> None:
    logger.info(
        "shopper_retrieval_completed",
        extra={
            "request_id": request_id,
            "retrieval_scope": "general",
            "business_intent": "generic_info",
            "outcome": result.outcome,
            "result_count": len(result.results),
            "duration_ms": max(0, int((time.perf_counter() - started) * 1000)),
        },
    )


def _finish(
    result: GeneralSearchResponse,
    runtime: AgentRun,
    request_id: str,
    started: float,
) -> GeneralSearchResponse:
    """Audit and log a general retrieval before returning it to the agent."""

    payload = rag_audit_payload(result.outcome, retrieval_scope="general")
    receipt = emit_audit_event(
        payload,
        runtime,
        request_id=request_id,
        event_logger=logger,
    )
    result.escalation = dict(receipt.escalation)
    result.audit_completed = receipt.logged
    _log(result, request_id, started)
    return result


@tool(
    name="search_general_documents",
    description=GENERAL_SEARCH_DESCRIPTION,
    permission=ToolPermission.READ_ONLY,
    expected_credentials=[ExpectedCredentials(app_id=RAG_APP_ID, type=RAG_CONNECTION_TYPE)],
)
async def search_general_documents(
    queries: Annotated[
        list[str],
        Field(min_length=1, max_length=5, description=GENERAL_QUERIES_DESCRIPTION),
    ],
    context: AgentRun,
) -> GeneralSearchResponse:
    """Retrieve candidate passages from the trusted general-document corpus."""

    started = time.perf_counter()
    runtime = cast(AgentRun | None, context)
    if runtime is None or runtime.request_context is None:
        return _error("agent runtime context is required")
    ctx = runtime.request_context
    request_id = normalize_request_id(ctx.get("request_id", ""))
    try:
        control = trusted_general_control(runtime)
        current_user_query = control.get("current_user_query")
        normalized_queries = _normalized_queries(queries)
        normalized_searches = [
            RetrievalSearch(semantic_query=query) for query in normalized_queries
        ]
        normalized_query = " | ".join(normalized_queries)[:2000]
        effective_year = effective_year_from_date(ctx.get("user_requested_eff_date", ""))
    except (TypeError, ValueError) as exc:
        result = _error(str(exc))
        return _finish(result, runtime, request_id, started)

    segment = str(ctx.get("application_market_segment", "") or "").strip().lower()
    if segment == "medicare":
        collection = "MOLS"
        endpoint_key = "RAG_MOLS_GENERAL_ENDPOINT"
    elif segment == "ind":
        collection = "IOLS"
        endpoint_key = "RAG_IOLS_GENERAL_ENDPOINT"
    else:
        result = _error("application_market_segment must be IND or Medicare")
        return _finish(result, runtime, request_id, started)

    language = str(ctx.get("user_language", "en") or "en").strip()
    if not language or len(language) > 10:
        result = _error("user_language must contain 1-10 characters")
        return _finish(result, runtime, request_id, started)
    brand = str(ctx.get("user_brand", "") or "").strip()
    user_type = (
        "member"
        if str(ctx.get("prospect_type", "") or "").strip().casefold() == "member"
        else "prospect"
    )
    payload = {
        "query": normalized_query,
        "searches": [search.model_dump() for search in normalized_searches],
        "effective_year": effective_year,
        "language": language,
        "user_type": user_type,
        "brand": brand,
    }
    connection = await asyncio.to_thread(connections.key_value, RAG_APP_ID)
    base_url = str(connection.get("RAG_API_BASE_URL", "") or "").strip()
    if not base_url:
        result = _error("RAG_API_BASE_URL is not configured")
        return _finish(result, runtime, request_id, started)
    api_key = str(connection.get("RAG_API_KEY", "") or "").strip()
    if not api_key:
        result = _error("RAG_API_KEY is not configured")
        return _finish(result, runtime, request_id, started)
    endpoint_path = str(connection.get(endpoint_key, "") or "").strip()
    if not endpoint_path:
        result = _error(f"{endpoint_key} is not configured")
        return _finish(result, runtime, request_id, started)
    try:
        url = build_endpoint_url(base_url, endpoint_path)
        response = await asyncio.to_thread(
            requests.post,
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-API-Key": api_key,
            },
            json=payload,
            timeout=30,
        )
    except (ValueError, requests.exceptions.RequestException):
        result = _error("RAG API request failed")
        return _finish(result, runtime, request_id, started)

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
        result = _error(f"RAG API returned non-JSON HTTP {response.status_code}")
        return _finish(result, runtime, request_id, started)
    if not isinstance(data, dict):
        result = _error("RAG API returned an invalid response object")
        return _finish(result, runtime, request_id, started)
    if not 200 <= response.status_code < 300:
        result = _error(f"RAG API returned HTTP {response.status_code}")
        return _finish(result, runtime, request_id, started)

    raw_results = data.get("results")
    metadata = data.get("metadata")
    has_results = data.get("has_results")
    declared_quality = metadata.get("evidence_quality") if isinstance(metadata, dict) else None
    evidence_reason = metadata.get("evidence_reason") if isinstance(metadata, dict) else None
    if (
        not isinstance(raw_results, list)
        or not isinstance(has_results, bool)
        or has_results != bool(raw_results)
        or data.get("collection") != collection
        or not isinstance(metadata, dict)
        or declared_quality not in {None, "sufficient", "insufficient"}
        or (evidence_reason is not None and not isinstance(evidence_reason, str))
    ):
        result = _error("RAG API response contract validation failed")
        return _finish(result, runtime, request_id, started)

    if not raw_results:
        result = _error("RAG API returned no matching general documents")
        result.no_results_reason = data.get("no_results_reason")
        result.retrieval_method = str(data.get("strategy")) if data.get("strategy") else None
        result.retrieval_time_ms = _retrieval_time_ms(data)
        return _finish(result, runtime, request_id, started)

    try:
        passages = _passages(raw_results, brand)
    except ValueError as exc:
        result = _error(f"RAG API response contract validation failed — {exc}")
        return _finish(result, runtime, request_id, started)
    policy_reason = (
        _policy_insufficiency_reason(current_user_query)
        if isinstance(current_user_query, str) and current_user_query.strip()
        else None
    )
    insufficient = declared_quality == "insufficient" or policy_reason is not None
    requested_fact = (
        current_user_query.strip()
        if isinstance(current_user_query, str) and current_user_query.strip()
        else normalized_query
    )
    result = GeneralSearchResponse(
        response_instructions=(
            None
            if insufficient
            else MEDICARE_CANDIDATE_RESPONSE_INSTRUCTIONS
            if segment == "medicare"
            else GENERAL_CANDIDATE_RESPONSE_INSTRUCTIONS
        ),
        original_user_query=requested_fact,
        executed_searches=normalized_queries,
        candidate_validation_status="UNAVAILABLE" if insufficient else "UNVALIDATED",
        outcome="insufficient" if insufficient else "candidates",
        requested_fact=requested_fact,
        evidence_status="unavailable" if insufficient else "unreviewed_candidates",
        results=[] if insufficient else passages,
        required_response=CANONICAL_NO_EVIDENCE_RESPONSE if insufficient else None,
        retrieval_method=str(data.get("strategy")) if data.get("strategy") else None,
        retrieval_time_ms=_retrieval_time_ms(data),
        insufficiency_reason=(
            str(policy_reason or evidence_reason or "Retrieved evidence is insufficient")
            if insufficient
            else None
        ),
        escalation=(
            {
                "type": True,
                "identification": "rag_insufficient_context",
                "description": "General search returned ambiguous or insufficient evidence",
            }
            if insufficient
            else {"type": False}
        ),
    )
    return _finish(result, runtime, request_id, started)
