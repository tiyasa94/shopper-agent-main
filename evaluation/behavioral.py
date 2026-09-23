"""Run and compare agent candidates against the curated evaluation corpus."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import secrets
import statistics
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from evaluation.clients.shopper_api import (
    PUBLIC_API_RESPONSE_KEY,
    PUBLIC_API_TRANSPORT_ATTEMPTS_KEY,
    ShopperApiClient,
    is_generic_wxo_execution_failure,
)
from evaluation.clients.wxo import (
    GENERAL_SEARCH_TOOL,
    PLAN_DETAILS_TOOL,
    SEARCH_TOOL,
    AgentRunResult,
    WxoClient,
    _sanitized,
)
from evaluation.contexts import context_to_wxo
from evaluation.io import sha256 as _sha256
from evaluation.io import utc_now as _utc_now

ROOT = Path(__file__).parents[1]
DEFAULT_CORPUS = ROOT / "tests/evaluation/behavioral_questions.yaml"
DEFAULT_AGENT = "Elevance_Health_Shopper_Portal"
RAG_TOOLS = (SEARCH_TOOL, GENERAL_SEARCH_TOOL)
RESULTS_JSONL = "results.jsonl"


def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        cast(Callable[[], None], close)()


RESULTS_CSV = "results.csv"
MANIFEST_JSON = "manifest.json"
SUMMARY_JSON = "summary.json"

CSV_COLUMNS = [
    "Candidate",
    "Trial",
    "Conversation ID",
    "Turn ID",
    "Market",
    "Context",
    "Current Plan",
    "Available Plans",
    "Question",
    "Reference Answer",
    "Expected Behavior",
    "Expected Route",
    "Expected Plan IDs",
    "Expected Detail Types",
    "Source Intent",
    "Source RAG Expected",
    "Response",
    "Actual Business Intent",
    "Turn Route",
    "Tool Trace",
    "RAG Called",
    "RAG Tool",
    "RAG Status",
    "RAG Coverage Complete",
    "RAG Missing Plans",
    "RAG Evidence Count",
    "Plans Found",
    "Plans Searched For",
    "Search Query",
    "Search Plan IDs",
    "Retrieved RAG Context (Excerpt)",
    "Plan Details Called",
    "Plan Details Observable",
    "Plan Details Plan IDs",
    "Plan Detail Types",
    "Plan Details Outcome",
    "Plan Details Coverage Complete",
    "Plan Details Missing Plans",
    "Plan Details Fallback Types",
    "Structured Plan Details (Excerpt)",
    "Audit Called",
    "Audit Source",
    "Audit Identification",
    "Audit Type",
    "Sensitive Value Echoed",
    "Serialized Envelope Exposed",
    "Transport Attempts",
    "Duration Seconds",
    "Run ID",
    "Status",
    "Error",
]


def load_corpus(path: Path) -> dict[str, Any]:
    corpus = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(corpus, dict) or corpus.get("schema_version") != 1:
        raise ValueError(f"Unsupported evaluation corpus schema in {path}")
    if not isinstance(corpus.get("contexts"), dict) or not isinstance(
        corpus.get("conversations"), list
    ):
        raise ValueError(f"Evaluation corpus is missing contexts or conversations: {path}")
    return corpus


def _conversation_features(conversation: dict[str, Any]) -> set[tuple[str, str]]:
    market = str(conversation.get("market", ""))
    features = {(market, "market")}
    for turn in conversation.get("turns", []):
        features.add((market, f"intent:{turn.get('source_intent')}"))
        features.add((market, f"rag:{bool(turn.get('source_rag_expected'))}"))
    if len(conversation.get("turns", [])) > 1:
        features.add((market, "multi_turn"))
    return features


def _greedy_feature_selection(
    conversations: list[dict[str, Any]], size: int
) -> list[dict[str, Any]]:
    remaining = list(conversations)
    selected: list[dict[str, Any]] = []
    covered: set[tuple[str, str]] = set()
    while remaining and len(selected) < size:
        best = max(
            remaining,
            key=lambda item: (
                len(_conversation_features(item) - covered),
                len(item.get("turns", [])) > 1,
                -conversations.index(item),
            ),
        )
        selected.append(best)
        covered.update(_conversation_features(best))
        remaining.remove(best)
    return selected


def _pilot_selection(conversations: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    """Balance markets, then cover source intents, RAG labels, and multi-turn cases."""
    market_names = list(dict.fromkeys(str(item.get("market", "")) for item in conversations))
    if not market_names:
        return []
    quota, remainder = divmod(size, len(market_names))
    by_market: list[list[dict[str, Any]]] = []
    for index, market in enumerate(market_names):
        market_conversations = [item for item in conversations if item.get("market") == market]
        by_market.append(
            _greedy_feature_selection(market_conversations, quota + (index < remainder))
        )
    selected: list[dict[str, Any]] = []
    for position in range(max((len(group) for group in by_market), default=0)):
        selected.extend(group[position] for group in by_market if position < len(group))
    if len(selected) < min(size, len(conversations)):
        remaining = [item for item in conversations if item not in selected]
        selected.extend(remaining[: size - len(selected)])
    return selected


def select_conversations(
    corpus: dict[str, Any],
    *,
    statuses: set[str],
    markets: set[str],
    conversation_ids: set[str],
    suite: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    conversations = [
        conversation
        for conversation in corpus["conversations"]
        if conversation.get("evaluation_status") in statuses
        and (not markets or conversation.get("market") in markets)
        and (not conversation_ids or conversation.get("id") in conversation_ids)
    ]
    if conversation_ids:
        found = {item["id"] for item in conversations}
        missing = conversation_ids - found
        if missing:
            raise ValueError(
                "Requested conversations were not selected (missing or filtered by status/market): "
                + ", ".join(sorted(missing))
            )
    if suite == "pilot":
        conversations = _pilot_selection(conversations, limit or 12)
    elif limit is not None:
        conversations = conversations[:limit]
    return conversations


def _first_response(result: AgentRunResult, tool_name: str) -> Any:
    responses = result.responses_for(tool_name)
    return responses[0].content if responses else None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _plan_ids(args: dict[str, Any]) -> list[str]:
    raw = args.get("plan_ids", args.get("plans_referenced", []))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            value = item.get("plan_id") or item.get("id") or item.get("plan_name")
        else:
            value = item
        if value is not None:
            values.append(str(value))
    return values


def _sensitive_echoed(query: str, response: str) -> bool:
    import re

    patterns = (
        r"\b\d{3}-\d{2}-\d{4}\b",
        r"\b\d{9}\b",
        r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13})\b",
        r"\bMRN[\s#:]*\d+\b",
    )
    sensitive_values = {
        match.group(0)
        for pattern in patterns
        for match in re.finditer(pattern, query, flags=re.IGNORECASE)
    }
    return any(value.casefold() in response.casefold() for value in sensitive_values)


def _serialized_envelope_exposed(response: str) -> bool:
    """Detect internal control payloads presented as the public response."""
    normalized = str(response or "").strip().casefold()
    if not normalized.startswith("{"):
        return False
    has_envelope_fields = any(
        marker in normalized
        for marker in ('"outcome"', "'outcome'", '"answer_facts"', "'answer_facts'")
    )
    has_internal_metadata = any(marker in normalized for marker in ('"plan_ids"', "'plan_ids'"))
    return has_envelope_fields and has_internal_metadata


def _truncate_strings(value: Any, limit: int = 1600) -> Any:
    if isinstance(value, dict):
        return {str(key): _truncate_strings(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_strings(item, limit) for item in value]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit].rstrip() + "… [truncated; full value in results.jsonl]"
    return value


def _rag_context_excerpt(value: Any) -> Any:
    if isinstance(value, list):
        return _truncate_strings(value[:8])
    return _truncate_strings(value)


def _tool_search_query(args: dict[str, Any]) -> str:
    """Render the canonical plan or general tool query list."""

    direct = args.get("search_query", args.get("query", ""))
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    queries = args.get("plan_independent_queries", args.get("queries"))
    if isinstance(queries, list):
        return "; ".join(str(query).strip() for query in queries if str(query).strip())
    return ""


def observations(result: AgentRunResult, query: str) -> dict[str, Any]:
    public_payload = result.context.get(PUBLIC_API_RESPONSE_KEY)
    if isinstance(public_payload, dict):
        observed = _public_api_observations(public_payload, query)
        observed["transport_attempts"] = int(
            result.context.get(PUBLIC_API_TRANSPORT_ATTEMPTS_KEY, 1)
        )
        return observed

    rag_calls = [call for call in result.tool_calls if call.name in RAG_TOOLS]
    rag_call = rag_calls[0] if rag_calls else None
    rag = _json_object(_first_response(result, rag_call.name)) if rag_call else {}
    plan_details_calls = result.calls_for(PLAN_DETAILS_TOOL)
    plan_details_call = plan_details_calls[0] if plan_details_calls else None
    plan_details = (
        _json_object(_first_response(result, PLAN_DETAILS_TOOL)) if plan_details_call else {}
    )
    plan_detail_types = plan_details_call.args.get("detail_types") if plan_details_call else None
    plans_searched_for = list(
        dict.fromkeys(
            _string_list(rag.get("plans_searched_for"))
            + _string_list(plan_details.get("plans_searched_for"))
        )
    )
    turn = _json_object(result.context.get("_turn_result"))
    rag_audit_completed = rag.get("audit_completed") is True
    plan_details_audit_completed = plan_details.get("audit_completed") is True
    turn_audit_completed = turn.get("audit_completed") is True
    if rag_audit_completed:
        audit = _json_object(rag.get("escalation"))
        audit_source = "search"
    elif plan_details_audit_completed:
        audit = _json_object(plan_details.get("escalation"))
        audit_source = "structured_plan_details"
    elif turn_audit_completed:
        audit = _json_object(turn.get("escalation"))
        audit_source = "guardrail"
    else:
        audit = {}
        audit_source = ""

    return {
        "tool_trace": [call.name for call in result.tool_calls],
        "route": str(turn.get("route", "")),
        "business_intent": str(
            rag.get("business_intent")
            or plan_details.get("business_intent")
            or turn.get("business_intent")
            or ""
        ),
        "rag_called": bool(rag_calls),
        "rag_call_count": len(rag_calls),
        "rag_tool": rag_call.name if rag_call else "",
        "search_query": _tool_search_query(rag_call.args) if rag_call else "",
        "search_plan_ids": _plan_ids(rag_call.args) if rag_call else [],
        "rag_status": str(rag.get("outcome", rag.get("status", ""))),
        "rag_coverage_complete": rag.get("coverage_complete"),
        "rag_missing_plans": rag.get("missing_plans", []),
        "rag_evidence_count": (
            len(rag.get("results", [])) if isinstance(rag.get("results", []), list) else 0
        ),
        "plans_found": rag.get("plans_found", []),
        "plans_searched_for": plans_searched_for,
        "rag_context_excerpt": _rag_context_excerpt(_sanitized(rag.get("results", []))),
        "plan_details_called": bool(plan_details_calls),
        "plan_details_observable": True,
        "plan_details_call_count": len(plan_details_calls),
        "plan_details_plan_ids": (_plan_ids(plan_details_call.args) if plan_details_call else []),
        "plan_details_all_available_plans": (
            plan_details_call.args.get("all_available_plans")
            if plan_details_call
            and isinstance(plan_details_call.args.get("all_available_plans"), bool)
            else None
        ),
        "plan_detail_types": (
            list(plan_detail_types) if isinstance(plan_detail_types, list) else []
        ),
        "plan_details_outcome": str(plan_details.get("outcome", "")),
        "plan_details_coverage_complete": plan_details.get("coverage_complete"),
        "plan_details_missing_plans": plan_details.get("missing_plans", []),
        "plan_details_fallback_types": plan_details.get("fallback_detail_types", []),
        "plan_details_excerpt": _truncate_strings(_sanitized(plan_details.get("plans", []))),
        "audit_called": (
            rag_audit_completed or plan_details_audit_completed or turn_audit_completed
        ),
        "audit_source": audit_source,
        "audit_identification": str(audit.get("identification", "")),
        "audit_type": audit.get("type"),
        "sensitive_value_echoed": _sensitive_echoed(query, result.response_text),
        "serialized_envelope_exposed": _serialized_envelope_exposed(result.response_text),
        "transport_attempts": 1,
    }


def _public_api_observations(payload: dict[str, Any], query: str) -> dict[str, Any]:
    """Derive only metadata the public API actually exposes; do not invent a tool trace."""
    rag = payload.get("rag_context")
    contexts = rag.get("contexts", []) if isinstance(rag, dict) else []
    if not isinstance(contexts, list):
        contexts = []
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    plan_details = metadata.get("plan_details")
    plan_details_observable = isinstance(plan_details, dict)
    plan_details = plan_details if isinstance(plan_details, dict) else {}
    raw_call_count = plan_details.get("call_count")
    plan_details_call_count = (
        raw_call_count
        if isinstance(raw_call_count, int) and not isinstance(raw_call_count, bool)
        else 0
    )
    raw_called = plan_details.get("called")
    plan_details_called = raw_called if isinstance(raw_called, bool) else None
    coverage_complete = plan_details.get("coverage_complete")
    user_query = payload.get("user_query")
    user_query = user_query if isinstance(user_query, dict) else {}
    escalation = metadata.get("escalation")
    escalation = escalation if isinstance(escalation, dict) else {}
    identification = str(escalation.get("identification") or "")
    plans_searched = metadata.get("plans_searched_for")
    plans_searched = plans_searched if isinstance(plans_searched, list) else []
    business_intent = str(user_query.get("business_intent") or "")
    status_by_audit = {
        "rag_response": "success",
        "rag_insufficient_context": "insufficient",
        "rag_retrieval_error": "error",
        "Triggered by Guardrails - RAG Error": "error",
    }
    structured_lookup = identification.startswith("structured_plan_")
    rag_called = bool(
        contexts
        or identification.startswith("rag_")
        or identification in status_by_audit
        or (plans_searched and not structured_lookup)
    )
    return {
        "tool_trace": [],
        # The public API does not expose the internal turn route. Keep this
        # unknown instead of inferring a private execution detail from RAG data.
        "route": "",
        "business_intent": business_intent,
        "rag_called": rag_called,
        "rag_call_count": int(rag_called),
        "rag_tool": "public RAG context" if rag_called else "",
        "search_query": "",
        "search_plan_ids": [],
        "rag_status": status_by_audit.get(identification, ""),
        "rag_coverage_complete": None,
        "rag_missing_plans": [],
        "rag_evidence_count": len(contexts),
        "plans_found": metadata.get("plans_found", []),
        "plans_searched_for": plans_searched,
        "rag_context_excerpt": _rag_context_excerpt(_sanitized(contexts)),
        "plan_details_called": plan_details_called,
        "plan_details_observable": plan_details_observable,
        "plan_details_call_count": plan_details_call_count,
        "plan_details_plan_ids": _string_list(plan_details.get("plan_ids")),
        "plan_details_all_available_plans": None,
        "plan_detail_types": _string_list(plan_details.get("detail_types")),
        "plan_details_outcome": str(plan_details.get("outcome") or ""),
        "plan_details_coverage_complete": (
            coverage_complete if isinstance(coverage_complete, bool) else None
        ),
        "plan_details_missing_plans": _string_list(plan_details.get("missing_plans")),
        "plan_details_fallback_types": _string_list(plan_details.get("fallback_detail_types")),
        "plan_details_excerpt": [],
        "audit_called": bool(identification),
        "audit_source": "public_metadata" if identification else "",
        "audit_identification": identification,
        "audit_type": escalation.get("type"),
        "sensitive_value_echoed": _sensitive_echoed(query, str(payload.get("response", {}))),
        "serialized_envelope_exposed": _serialized_envelope_exposed(
            str(payload.get("response", {}))
        ),
    }


def _record_key(record: dict[str, Any]) -> tuple[int, str, str]:
    return (
        int(record["trial"]),
        str(record["conversation_id"]),
        str(record["turn_id"]),
    )


def _load_records(path: Path) -> dict[tuple[int, str, str], dict[str, Any]]:
    if not path.exists():
        return {}
    records: dict[tuple[int, str, str], dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            records[_record_key(record)] = record
    return records


def _append_record(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_sanitized(record), ensure_ascii=False, default=str) + "\n")


def _csv_row(record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result", {})
    observed = record.get("observations", {})
    return {
        "Candidate": record.get("candidate", ""),
        "Trial": record.get("trial", ""),
        "Conversation ID": record.get("conversation_id", ""),
        "Turn ID": record.get("turn_id", ""),
        "Market": record.get("market", ""),
        "Context": record.get("context_name", ""),
        "Current Plan": json.dumps(record.get("current_plan", ""), ensure_ascii=False),
        "Available Plans": json.dumps(record.get("available_plans", []), ensure_ascii=False),
        "Question": record.get("question", ""),
        "Reference Answer": record.get("reference_answer", ""),
        "Expected Behavior": record.get("expected_behavior", ""),
        "Expected Route": record.get("expected_route", ""),
        "Expected Plan IDs": json.dumps(record.get("expected_plan_ids", [])),
        "Expected Detail Types": json.dumps(record.get("expected_detail_types", [])),
        "Source Intent": record.get("source_intent", ""),
        "Source RAG Expected": record.get("source_rag_expected", ""),
        "Response": result.get("response_text", ""),
        "Actual Business Intent": observed.get("business_intent", ""),
        "Turn Route": observed.get("route", observed.get("flow_type", "")),
        "Tool Trace": " -> ".join(observed.get("tool_trace", [])),
        "RAG Called": observed.get("rag_called", ""),
        "RAG Tool": observed.get("rag_tool", ""),
        "RAG Status": observed.get("rag_status", ""),
        "RAG Coverage Complete": observed.get("rag_coverage_complete", ""),
        "RAG Missing Plans": json.dumps(observed.get("rag_missing_plans", [])),
        "RAG Evidence Count": observed.get("rag_evidence_count", ""),
        "Plans Found": json.dumps(observed.get("plans_found", []), ensure_ascii=False),
        "Plans Searched For": json.dumps(
            observed.get("plans_searched_for", []), ensure_ascii=False
        ),
        "Search Query": observed.get("search_query", ""),
        "Search Plan IDs": json.dumps(observed.get("search_plan_ids", [])),
        "Retrieved RAG Context (Excerpt)": json.dumps(
            observed.get("rag_context_excerpt", observed.get("rag_context", [])),
            ensure_ascii=False,
        ),
        "Plan Details Called": observed.get("plan_details_called", ""),
        "Plan Details Observable": observed.get("plan_details_observable", ""),
        "Plan Details Plan IDs": json.dumps(observed.get("plan_details_plan_ids", [])),
        "Plan Detail Types": json.dumps(observed.get("plan_detail_types", [])),
        "Plan Details Outcome": observed.get("plan_details_outcome", ""),
        "Plan Details Coverage Complete": observed.get("plan_details_coverage_complete", ""),
        "Plan Details Missing Plans": json.dumps(observed.get("plan_details_missing_plans", [])),
        "Plan Details Fallback Types": json.dumps(observed.get("plan_details_fallback_types", [])),
        "Structured Plan Details (Excerpt)": json.dumps(
            observed.get("plan_details_excerpt", []), ensure_ascii=False
        ),
        "Audit Called": observed.get("audit_called", ""),
        "Audit Source": observed.get("audit_source", ""),
        "Audit Identification": observed.get("audit_identification", ""),
        "Audit Type": observed.get("audit_type", ""),
        "Sensitive Value Echoed": observed.get("sensitive_value_echoed", ""),
        "Serialized Envelope Exposed": observed.get("serialized_envelope_exposed", ""),
        "Transport Attempts": observed.get("transport_attempts", ""),
        "Duration Seconds": result.get("duration_seconds", ""),
        "Run ID": result.get("run_id", ""),
        "Status": record.get("status", ""),
        "Error": record.get("error", ""),
    }


def _write_csv(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(_csv_row(record) for record in records)


def _business_intent_status(
    record: dict[str, Any],
) -> Literal["aligned", "mismatched", "not_applicable", "missing"]:
    """Classify intent observability without scoring clarification turns as failures."""

    expected = str(record.get("source_intent") or "")
    expected_route = str(record.get("expected_route") or "")
    observed = str(record.get("observations", {}).get("business_intent") or "")
    if not expected or expected_route in {"clarify_plan", "clarify_benefit"}:
        return "not_applicable"
    if not observed:
        return "missing"
    return "aligned" if observed == expected else "mismatched"


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(records)
    execution_failures = [record for record in values if _is_execution_failure_record(record)]
    completed = [
        record
        for record in values
        if record.get("status") == "completed" and not _is_execution_failure_record(record)
    ]
    observations_ = [record.get("observations", {}) for record in completed]
    business_intent_statuses = [_business_intent_status(record) for record in completed]
    durations = [
        float(record["result"]["duration_seconds"])
        for record in completed
        if record.get("result", {}).get("duration_seconds") is not None
    ]
    durations_sorted = sorted(durations)
    if durations_sorted:
        position = (len(durations_sorted) - 1) * 0.95
        lower = int(position)
        upper = min(lower + 1, len(durations_sorted) - 1)
        fraction = position - lower
        p95_duration = (
            durations_sorted[lower] + (durations_sorted[upper] - durations_sorted[lower]) * fraction
        )
    else:
        p95_duration = None
    return {
        "turns": len(values),
        "completed": len(completed),
        "errors": sum(record.get("status") == "error" for record in values),
        "execution_failures": len(execution_failures),
        "skipped_prior_error": sum(
            record.get("status") == "skipped_prior_error" for record in values
        ),
        "rag_called": sum(item.get("rag_called") is True for item in observations_),
        "rag_calls_audited": sum(
            item.get("rag_called") is True and item.get("audit_called") is True
            for item in observations_
        ),
        "repeated_rag_calls": sum(int(item.get("rag_call_count", 0)) > 1 for item in observations_),
        "plan_details_called": sum(
            item.get("plan_details_called") is True for item in observations_
        ),
        "repeated_plan_details_calls": sum(
            int(item.get("plan_details_call_count", 0)) > 1 for item in observations_
        ),
        "audit_called": sum(item.get("audit_called") is True for item in observations_),
        "empty_responses": sum(
            not str(record.get("result", {}).get("response_text", "")).strip()
            for record in completed
        ),
        "sensitive_value_echoed": sum(
            item.get("sensitive_value_echoed") is True for item in observations_
        ),
        "serialized_envelope_exposed": sum(
            item.get("serialized_envelope_exposed") is True for item in observations_
        ),
        "business_intent_aligned": business_intent_statuses.count("aligned"),
        "business_intent_mismatched": business_intent_statuses.count("mismatched"),
        "business_intent_not_applicable": business_intent_statuses.count("not_applicable"),
        "business_intent_missing": business_intent_statuses.count("missing"),
        "rag_expectation_aligned": sum(
            isinstance(record.get("source_rag_expected"), bool)
            and record.get("source_rag_expected")
            == (record.get("observations", {}).get("rag_called") is True)
            for record in completed
        ),
        "mean_duration_seconds": round(statistics.fmean(durations), 3) if durations else None,
        "median_duration_seconds": round(statistics.median(durations), 3) if durations else None,
        "p95_duration_seconds": round(p95_duration, 3) if p95_duration is not None else None,
        "transport_retries": sum(
            max(0, int(record.get("observations", {}).get("transport_attempts", 1)) - 1)
            for record in values
            if record.get("result")
        ),
    }


def _is_execution_failure_record(record: dict[str, Any]) -> bool:
    return record.get("status") == "execution_failure" or is_generic_wxo_execution_failure(
        str(record.get("result", {}).get("response_text", ""))
    )


def _base_record(
    candidate: str,
    trial: int,
    conversation: dict[str, Any],
    turn: dict[str, Any],
    context_name: str,
    source_context: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "candidate": candidate,
        "trial": trial,
        "conversation_id": conversation["id"],
        "turn_id": turn["id"],
        "market": conversation.get("market"),
        "context_name": context_name,
        "current_plan": source_context.get("user_current_plan", ""),
        "available_plans": source_context.get("application_available_plans", []),
        "question": turn.get("question", ""),
        "reference_answer": turn.get("reference_answer"),
        "expected_behavior": turn.get("expected_behavior"),
        "expected_route": turn.get("expected_route"),
        "expected_plan_ids": turn.get("expected_plan_ids", []),
        "expected_detail_types": turn.get("expected_detail_types", []),
        "source_intent": turn.get("source_intent"),
        "source_rag_expected": turn.get("source_rag_expected"),
        "source_row": turn.get("source_row"),
    }


def _manifest(args: argparse.Namespace, corpus_path: Path, selected: list[dict]) -> dict:
    return {
        "schema_version": 1,
        "run_instance_id": secrets.token_hex(8),
        "candidate": args.candidate,
        "agent_name": args.agent,
        "agent_id": args.agent_id,
        "source_ref": args.source_ref,
        "source_revision": args.source_revision,
        "source_fingerprint": args.source_fingerprint,
        "rag_base_url": args.rag_base_url,
        "corpus_path": str(corpus_path.resolve()),
        "corpus_sha256": _sha256(corpus_path),
        "suite": args.suite,
        "statuses": args.status,
        "markets": args.market,
        "trials": args.trials,
        "warmup": bool(getattr(args, "warmup", False)),
        "concurrency": int(getattr(args, "concurrency", 1)),
        "turn_delay_seconds": float(getattr(args, "turn_delay", 0.0)),
        "conversation_ids": [item["id"] for item in selected],
        "conversation_count": len(selected),
        "turn_count_per_trial": sum(len(item["turns"]) for item in selected),
        "transport": args.transport,
        "endpoint_url": args.url,
        "local_wxo_url": args.url,
        "python": platform.python_version(),
        "started_at": _utc_now(),
        "completed_at": None,
    }


def run_candidate(args: argparse.Namespace) -> int:
    turn_delay = float(getattr(args, "turn_delay", 0.0))
    concurrency = int(getattr(args, "concurrency", 1))
    corpus_path = args.corpus.resolve()
    corpus = load_corpus(corpus_path)
    selected = select_conversations(
        corpus,
        statuses=set(args.status),
        markets=set(args.market),
        conversation_ids=set(args.conversation_id),
        suite=args.suite,
        limit=args.limit_conversations,
    )
    if not selected:
        raise ValueError("No conversations matched the requested selection")

    output = args.output.resolve()
    manifest_path = output / MANIFEST_JSON
    results_path = output / RESULTS_JSONL
    if output.exists() and any(output.iterdir()) and not args.resume:
        raise FileExistsError(f"Output directory is not empty; use --resume: {output}")
    output.mkdir(parents=True, exist_ok=True)

    if args.resume:
        if not manifest_path.exists():
            raise FileNotFoundError(f"Cannot resume without {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("candidate") != args.candidate:
            raise ValueError("Resume candidate does not match the existing manifest")
        if manifest.get("corpus_sha256") != _sha256(corpus_path):
            raise ValueError("Resume corpus does not match the existing manifest")
        if manifest.get("conversation_ids") != [item["id"] for item in selected]:
            raise ValueError("Resume conversation selection does not match the existing manifest")
        if manifest.get("trials") != args.trials:
            raise ValueError("Resume trial count does not match the existing manifest")
        if manifest.get("source_revision") != args.source_revision:
            raise ValueError("Resume source revision does not match the existing manifest")
        if manifest.get("source_fingerprint") != args.source_fingerprint:
            raise ValueError("Resume source fingerprint does not match the existing manifest")
        if manifest.get("rag_base_url") != args.rag_base_url:
            raise ValueError("Resume RAG base URL does not match the existing manifest")
        if float(manifest.get("turn_delay_seconds", 0.0)) != turn_delay:
            raise ValueError("Resume turn delay does not match the existing manifest")
    else:
        manifest = _manifest(args, corpus_path, selected)
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    # Concurrency controls execution only, so a resumed run may safely use a
    # different value than the run that created the original manifest.
    manifest["concurrency"] = concurrency

    prior = _load_records(results_path)
    replay_conversations: set[tuple[int, str]] = set()
    if args.resume and prior:
        for trial in range(1, args.trials + 1):
            for conversation in selected:
                keys = [(trial, conversation["id"], turn["id"]) for turn in conversation["turns"]]
                existing = [prior[key] for key in keys if key in prior]
                if existing and (
                    len(existing) != len(keys)
                    or any(record.get("status") != "completed" for record in existing)
                ):
                    replay_conversations.add((trial, conversation["id"]))
    allow_remote = bool(args.allow_remote)

    def build_client():
        if args.transport == "shopper-api":
            return ShopperApiClient(
                args.url,
                api_key=os.getenv(args.shopper_api_key_env, ""),
                timeout=args.timeout,
                allow_remote=allow_remote,
            )
        return WxoClient(
            args.url,
            args.agent,
            timeout=args.timeout,
            allow_remote=allow_remote,
            api_key=os.getenv("WXO_API_KEY") if allow_remote else None,
            agent_id=args.agent_id,
        )

    total = manifest["turn_count_per_trial"] * args.trials
    completed_count = 0
    requests_started = 0
    progress_lock = threading.Lock()
    record_lock = threading.Lock()
    rate_limit_lock = threading.Lock()
    client_lock = threading.Lock()
    client_state = threading.local()
    worker_clients: list[Any] = []

    def worker_client():
        client = getattr(client_state, "client", None)
        if client is None:
            client = build_client()
            client_state.client = client
            with client_lock:
                worker_clients.append(client)
        return client

    def wait_for_request_slot() -> None:
        nonlocal requests_started
        with rate_limit_lock:
            if requests_started and turn_delay:
                time.sleep(turn_delay)
            requests_started += 1

    if getattr(args, "warmup", False) and not args.resume:
        warmup_conversation = selected[0]
        warmup_turn = warmup_conversation["turns"][0]
        warmup_context_name = warmup_turn.get("context", warmup_conversation["context"])
        warmup_source_context = corpus["contexts"][warmup_context_name]
        warmup_context = (
            warmup_source_context
            if args.transport == "shopper-api"
            else context_to_wxo(warmup_source_context, [])
        )
        print(f"Warming {args.candidate} transport (not recorded)...", flush=True)
        warmup_client = build_client()
        try:
            warmup_client.run(
                "evaluation-warmup",
                warmup_turn["question"],
                warmup_context,
                session_id=f"warmup-{manifest['run_instance_id']}",
            )
            requests_started += 1
        finally:
            _close_client(warmup_client)

    print(
        f"Evaluating {args.candidate}: {len(selected)} conversations, {total} turns "
        f"({args.trials} trial(s), concurrency={concurrency}) -> {output}",
        flush=True,
    )

    def run_conversation(trial: int, conversation: dict[str, Any]) -> None:
        nonlocal completed_count
        history: list[dict[str, str]] = []
        prior_failed = False
        replay_conversation = (trial, conversation["id"]) in replay_conversations
        session_id = f"eval-{manifest['run_instance_id']}-t{trial}-{conversation['id']}"[:120]
        for turn in conversation["turns"]:
            with progress_lock:
                completed_count += 1
                progress = completed_count
            key = (trial, conversation["id"], turn["id"])
            context_name = turn.get("context", conversation["context"])
            if key in prior and not replay_conversation:
                record = prior[key]
                status = record.get("status")
                response = record.get("result", {}).get("response_text", "")
                history.append({"role": "user", "content": str(_sanitized(turn["question"]))})
                if response:
                    history.append({"role": "assistant", "content": response})
                prior_failed = prior_failed or status in {
                    "error",
                    "execution_failure",
                    "skipped_prior_error",
                }
                print(f"[{progress}/{total}] {turn['id']}: resumed", flush=True)
                continue

            source_context = corpus["contexts"][context_name]
            record = _base_record(
                args.candidate,
                trial,
                conversation,
                turn,
                context_name,
                source_context,
            )
            if prior_failed:
                record.update(
                    {
                        "status": "skipped_prior_error",
                        "error": "Not run because an earlier turn in this conversation failed",
                        "result": {},
                        "observations": {},
                    }
                )
            else:
                try:
                    context = (
                        source_context
                        if args.transport == "shopper-api"
                        else context_to_wxo(source_context, history)
                    )
                    wait_for_request_slot()
                    result = worker_client().run(
                        turn["id"], turn["question"], context, session_id=session_id
                    )
                    execution_failure = is_generic_wxo_execution_failure(result.response_text)
                    if execution_failure:
                        prior_failed = True
                    record.update(
                        {
                            "status": "execution_failure" if execution_failure else "completed",
                            "error": (
                                "WXO returned its generic execution-failure response"
                                if execution_failure
                                else ""
                            ),
                            "result": asdict(result),
                            "observations": observations(result, turn["question"]),
                        }
                    )
                except Exception as exc:  # checkpoint long, expensive runs
                    prior_failed = True
                    record.update(
                        {
                            "status": "error",
                            "error": f"{type(exc).__name__}: {exc}",
                            "result": {},
                            "observations": {},
                        }
                    )

            safe_record = _sanitized(record)
            with record_lock:
                _append_record(results_path, safe_record)
                prior[key] = safe_record
            response = safe_record.get("result", {}).get("response_text", "")
            history.append({"role": "user", "content": str(_sanitized(turn["question"]))})
            if response:
                history.append({"role": "assistant", "content": response})
            print(
                f"[{progress}/{total}] {turn['id']}: {record['status']} "
                f"{record.get('observations', {}).get('tool_trace', [])}",
                flush=True,
            )

    try:
        jobs = [
            (trial, conversation)
            for trial in range(1, args.trials + 1)
            for conversation in selected
        ]
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(run_conversation, trial, conversation)
                for trial, conversation in jobs
            ]
            for future in as_completed(futures):
                future.result()
    finally:
        for client in worker_clients:
            _close_client(client)

    records = [prior[key] for key in sorted(prior)]
    _write_csv(output / RESULTS_CSV, records)
    summary = summarize(records)
    (output / SUMMARY_JSON).write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    manifest["completed_at"] = _utc_now()
    manifest["summary"] = summary
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Candidate results: {output / RESULTS_CSV}", flush=True)
    return 0


def _candidate_data(path: Path) -> tuple[dict[str, Any], dict[tuple, dict[str, Any]]]:
    manifest_path = path / MANIFEST_JSON
    results_path = path / RESULTS_JSONL
    if not manifest_path.exists() or not results_path.exists():
        raise FileNotFoundError(
            f"Candidate directory must contain manifest.json and results.jsonl: {path}"
        )
    return json.loads(manifest_path.read_text()), _load_records(results_path)


def candidate_cache_is_reusable(
    path: Path,
    *,
    source_revision: str,
    rag_base_url: str,
    suite: str,
    corpus: Path,
    source_fingerprint: str | None = None,
    endpoint_url: str | None = None,
    transport: str | None = None,
    agent_id: str | None = None,
    turn_delay_seconds: float | None = None,
) -> tuple[bool, str]:
    """Validate identity and completeness before reusing an expensive candidate run."""
    try:
        manifest, records = _candidate_data(path)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        return False, str(exc)

    expected: dict[str, Any] = {
        "source_revision": source_revision,
        "rag_base_url": rag_base_url,
        "suite": suite,
        "corpus_sha256": _sha256(corpus.resolve()),
    }
    optional_identity = {
        "source_fingerprint": source_fingerprint,
        "endpoint_url": endpoint_url,
        "transport": transport,
        "agent_id": agent_id,
        "turn_delay_seconds": turn_delay_seconds,
    }
    expected.update({key: value for key, value in optional_identity.items() if value is not None})
    for field, value in expected.items():
        if manifest.get(field) != value:
            return False, f"cached {field} does not match"

    expected_turns = int(manifest.get("turn_count_per_trial", 0)) * int(manifest.get("trials", 0))
    summary = summarize(records.values())
    if not manifest.get("completed_at"):
        return False, "cached run is not marked complete"
    if expected_turns < 1 or summary["turns"] != expected_turns:
        return False, "cached run does not contain every expected turn"
    if summary["errors"] or summary["execution_failures"] or summary["skipped_prior_error"]:
        return False, "cached run contains errors or skipped turns (including execution failures)"
    if summary["completed"] != expected_turns:
        return False, "cached run is incomplete"
    return True, "cache is complete and compatible"


def validate_cache(args: argparse.Namespace) -> int:
    reusable, reason = candidate_cache_is_reusable(
        args.candidate_dir.resolve(),
        source_revision=args.source_revision,
        rag_base_url=args.rag_base_url,
        suite=args.suite,
        corpus=args.corpus,
        source_fingerprint=args.source_fingerprint,
        endpoint_url=args.endpoint_url,
        transport=args.transport,
        agent_id=args.agent_id,
        turn_delay_seconds=args.turn_delay_seconds,
    )
    print(reason)
    return 0 if reusable else 1


def _comparison_fields(prefix: str) -> list[str]:
    return [
        f"{prefix} Response",
        f"{prefix} Business Intent",
        f"{prefix} Turn Route",
        f"{prefix} Tool Trace",
        f"{prefix} RAG Called",
        f"{prefix} RAG Status",
        f"{prefix} RAG Evidence Count",
        f"{prefix} Plans Found",
        f"{prefix} Plans Searched For",
        f"{prefix} RAG Context (Excerpt)",
        f"{prefix} Audit Source",
        f"{prefix} Audit Identification",
        f"{prefix} Sensitive Value Echoed",
        f"{prefix} Serialized Envelope Exposed",
        f"{prefix} Duration Seconds",
        f"{prefix} Status",
        f"{prefix} Error",
        f"{prefix} Correctness (1-5)",
        f"{prefix} Groundedness (1-5)",
        f"{prefix} Guardrail Behavior (1-5)",
    ]


def _paired_columns() -> list[str]:
    return [
        "Trial",
        "Conversation ID",
        "Turn ID",
        "Market",
        "Context",
        "Current Plan",
        "Available Plans",
        "Question",
        "Reference Answer",
        "Expected Behavior",
        "Expected Route",
        "Expected Plan IDs",
        "Source Intent",
        "Source RAG Expected",
        *_comparison_fields("Candidate A"),
        *_comparison_fields("Candidate B"),
        "Preferred Candidate (A/B/Tie)",
        "Reviewer Notes",
    ]


def _comparison_values(prefix: str, record: dict[str, Any]) -> dict[str, Any]:
    result = record.get("result", {})
    observed = record.get("observations", {})
    blinded_trace = [
        "plan_retrieval" if name in RAG_TOOLS else name for name in observed.get("tool_trace", [])
    ]
    return {
        f"{prefix} Response": result.get("response_text", ""),
        f"{prefix} Business Intent": observed.get("business_intent", ""),
        f"{prefix} Turn Route": observed.get("route", observed.get("flow_type", "")),
        f"{prefix} Tool Trace": " -> ".join(blinded_trace),
        f"{prefix} RAG Called": observed.get("rag_called", ""),
        f"{prefix} RAG Status": observed.get("rag_status", ""),
        f"{prefix} RAG Evidence Count": observed.get(
            "rag_evidence_count", len(observed.get("rag_context", []))
        ),
        f"{prefix} Plans Found": json.dumps(observed.get("plans_found", []), ensure_ascii=False),
        f"{prefix} Plans Searched For": json.dumps(
            observed.get("plans_searched_for", []), ensure_ascii=False
        ),
        f"{prefix} RAG Context (Excerpt)": json.dumps(
            _rag_context_excerpt(
                observed.get("rag_context_excerpt", observed.get("rag_context", []))
            ),
            ensure_ascii=False,
        ),
        f"{prefix} Audit Source": observed.get("audit_source", ""),
        f"{prefix} Audit Identification": observed.get("audit_identification", ""),
        f"{prefix} Sensitive Value Echoed": observed.get("sensitive_value_echoed", ""),
        f"{prefix} Serialized Envelope Exposed": observed.get("serialized_envelope_exposed", ""),
        f"{prefix} Duration Seconds": result.get("duration_seconds", ""),
        f"{prefix} Status": record.get("status", "missing"),
        f"{prefix} Error": record.get("error", ""),
        f"{prefix} Correctness (1-5)": "",
        f"{prefix} Groundedness (1-5)": "",
        f"{prefix} Guardrail Behavior (1-5)": "",
    }


def _summary_markdown(summary_a: dict, summary_b: dict) -> str:
    rows = [
        ("Turns", "turns"),
        ("Completed", "completed"),
        ("Errors", "errors"),
        ("Execution failures", "execution_failures"),
        ("Skipped after error", "skipped_prior_error"),
        ("RAG called", "rag_called"),
        ("RAG calls audited", "rag_calls_audited"),
        ("Repeated RAG calls", "repeated_rag_calls"),
        ("Audit called", "audit_called"),
        ("Empty responses", "empty_responses"),
        ("Sensitive value echoed", "sensitive_value_echoed"),
        ("Serialized envelope exposed", "serialized_envelope_exposed"),
        ("Business intent aligned", "business_intent_aligned"),
        ("Business intent mismatched", "business_intent_mismatched"),
        ("Business intent not applicable", "business_intent_not_applicable"),
        ("Business intent missing", "business_intent_missing"),
        ("Expected RAG behavior aligned", "rag_expectation_aligned"),
        ("Median duration (s)", "median_duration_seconds"),
        ("Mean duration (s)", "mean_duration_seconds"),
        ("P95 duration (s)", "p95_duration_seconds"),
        ("Transport retries", "transport_retries"),
    ]
    lines = [
        "# Blinded candidate comparison",
        "",
        "Candidate identities are stored separately in `candidate-key.json`.",
        "Reference answers are review aids, not exact-match oracles.",
        "Source intents and RAG expectations are annotations.",
        "",
        "| Metric | Candidate A | Candidate B |",
        "| --- | ---: | ---: |",
    ]
    lines.extend(
        f"| {label} | {summary_a.get(key, '')} | {summary_b.get(key, '')} |" for label, key in rows
    )
    lines.extend(
        [
            "",
            "Review `review-blinded.csv` turn by turn. Score correctness, groundedness, and",
            "guardrail behavior from 1 (poor) to 5 (excellent), then choose A, B, or Tie.",
            "",
        ]
    )
    return "\n".join(lines)


def compare_candidates(args: argparse.Namespace) -> int:
    manifest_left, left = _candidate_data(args.baseline.resolve())
    manifest_right, right = _candidate_data(args.challenger.resolve())
    if manifest_left["corpus_sha256"] != manifest_right["corpus_sha256"]:
        raise ValueError("Candidate runs used different corpus versions")
    if manifest_left.get("conversation_ids") != manifest_right.get("conversation_ids"):
        raise ValueError("Candidate runs used different conversation selections")
    if manifest_left.get("trials") != manifest_right.get("trials"):
        raise ValueError("Candidate runs used different trial counts")

    seed = args.blind_seed or secrets.token_hex(16)
    swap = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % 2 == 1
    if swap:
        manifest_a, records_a, path_a = manifest_right, right, args.challenger
        manifest_b, records_b, path_b = manifest_left, left, args.baseline
    else:
        manifest_a, records_a, path_a = manifest_left, left, args.baseline
        manifest_b, records_b, path_b = manifest_right, right, args.challenger

    keys = sorted(set(records_a) | set(records_b))
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Comparison output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for key in keys:
        record_a = records_a.get(key, {})
        record_b = records_b.get(key, {})
        source = record_a or record_b
        row = {
            "Trial": source.get("trial", key[0]),
            "Conversation ID": source.get("conversation_id", key[1]),
            "Turn ID": source.get("turn_id", key[2]),
            "Market": source.get("market", ""),
            "Context": source.get("context_name", ""),
            "Current Plan": json.dumps(source.get("current_plan", ""), ensure_ascii=False),
            "Available Plans": json.dumps(source.get("available_plans", []), ensure_ascii=False),
            "Question": source.get("question", ""),
            "Reference Answer": source.get("reference_answer", ""),
            "Expected Behavior": source.get("expected_behavior", ""),
            "Expected Route": source.get("expected_route", ""),
            "Expected Plan IDs": json.dumps(source.get("expected_plan_ids", [])),
            "Source Intent": source.get("source_intent", ""),
            "Source RAG Expected": source.get("source_rag_expected", ""),
            **_comparison_values("Candidate A", record_a),
            **_comparison_values("Candidate B", record_b),
            "Preferred Candidate (A/B/Tie)": "",
            "Reviewer Notes": "",
        }
        rows.append(_sanitized(row))

    review_path = output / "review-blinded.csv"
    with review_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_paired_columns(), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    summary_a = summarize(records_a.values())
    summary_b = summarize(records_b.values())
    key = {
        "schema_version": 1,
        "blind_seed": seed,
        "candidate_a": {
            "candidate": manifest_a["candidate"],
            "source_revision": manifest_a.get("source_revision"),
            "rag_base_url": manifest_a.get("rag_base_url"),
            "results": str(path_a.resolve()),
        },
        "candidate_b": {
            "candidate": manifest_b["candidate"],
            "source_revision": manifest_b.get("source_revision"),
            "rag_base_url": manifest_b.get("rag_base_url"),
            "results": str(path_b.resolve()),
        },
    }
    (output / "candidate-key.json").write_text(json.dumps(key, indent=2, sort_keys=True) + "\n")
    (output / "summary.json").write_text(
        json.dumps({"candidate_a": summary_a, "candidate_b": summary_b}, indent=2) + "\n"
    )
    (output / "summary.md").write_text(_summary_markdown(summary_a, summary_b))
    print(f"Blinded review: {review_path}")
    print(f"Candidate key: {output / 'candidate-key.json'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run one imported local candidate")
    run_parser.set_defaults(func=run_candidate)
    run_parser.add_argument("--candidate", required=True)
    run_parser.add_argument("--source-ref", required=True)
    run_parser.add_argument("--source-revision", required=True)
    run_parser.add_argument("--source-fingerprint", default="")
    run_parser.add_argument("--rag-base-url", required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    run_parser.add_argument("--suite", choices=("pilot", "full"), default="pilot")
    run_parser.add_argument(
        "--status",
        action="append",
        choices=("eligible", "needs_review", "excluded_prompt_overlap"),
        default=None,
    )
    run_parser.add_argument(
        "--market", action="append", choices=("Individual", "Medicare"), default=[]
    )
    run_parser.add_argument("--conversation-id", action="append", default=[])
    run_parser.add_argument("--limit-conversations", type=int)
    run_parser.add_argument("--trials", type=int, default=1)
    run_parser.add_argument(
        "--warmup",
        action="store_true",
        help="Run one unrecorded request before timing the selected suite",
    )
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "Run this many conversations concurrently (1-5); turns within each "
            "conversation remain sequential"
        ),
    )
    run_parser.add_argument(
        "--turn-delay",
        type=float,
        default=0.0,
        help="Delay between live requests in seconds (0-10); cached turns are not delayed",
    )
    run_parser.add_argument("--url", default=os.getenv("LOCAL_WXO_URL", "http://localhost:4321"))
    run_parser.add_argument("--transport", choices=("wxo", "shopper-api"), default="wxo")
    run_parser.add_argument("--shopper-api-key-env", default="SHOPPER_API_KEY")
    run_parser.add_argument("--agent", default=os.getenv("LOCAL_WXO_AGENT_NAME", DEFAULT_AGENT))
    run_parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Explicitly allow an IBM Cloud WXO endpoint; requires WXO_API_KEY",
    )
    run_parser.add_argument(
        "--agent-id",
        default=os.getenv("LOCAL_WXO_AGENT_ID"),
        help="Pin evaluation to an exact imported agent ID",
    )
    run_parser.add_argument(
        "--timeout", type=int, default=int(os.getenv("LOCAL_WXO_E2E_TIMEOUT", "120"))
    )

    compare_parser = subparsers.add_parser("compare", help="Build a blinded paired report")
    compare_parser.set_defaults(func=compare_candidates)
    compare_parser.add_argument("--baseline", type=Path, required=True)
    compare_parser.add_argument("--challenger", type=Path, required=True)
    compare_parser.add_argument("--output", type=Path, required=True)
    compare_parser.add_argument("--blind-seed")

    cache_parser = subparsers.add_parser(
        "validate-cache", help="Validate a completed candidate directory for safe reuse"
    )
    cache_parser.set_defaults(func=validate_cache)
    cache_parser.add_argument("--candidate-dir", type=Path, required=True)
    cache_parser.add_argument("--source-revision", required=True)
    cache_parser.add_argument("--rag-base-url", required=True)
    cache_parser.add_argument("--suite", choices=("pilot", "full"), required=True)
    cache_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    cache_parser.add_argument("--source-fingerprint")
    cache_parser.add_argument("--endpoint-url")
    cache_parser.add_argument("--transport", choices=("wxo", "shopper-api"))
    cache_parser.add_argument("--agent-id")
    cache_parser.add_argument("--turn-delay-seconds", type=float)

    from evaluation.judge import configure_parser as configure_judge_parser

    judge_parser = subparsers.add_parser(
        "judge", help="Semantically grade a completed behavioral run with watsonx.ai"
    )
    configure_judge_parser(judge_parser)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "run":
        args.status = args.status or ["eligible"]
        if args.trials < 1:
            raise ValueError("--trials must be at least 1")
        if args.limit_conversations is not None and args.limit_conversations < 1:
            raise ValueError("--limit-conversations must be at least 1")
        if not 0 <= args.turn_delay <= 10:
            raise ValueError("--turn-delay must be between 0 and 10 seconds")
        if not 1 <= args.concurrency <= 5:
            raise ValueError("--concurrency must be between 1 and 5")
    if args.command == "judge" and args.limit is not None and args.limit < 1:
        raise ValueError("--limit must be at least 1")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
