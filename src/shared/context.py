"""Trusted runtime-context helpers shared by the shopper agent entry points."""

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, cast

from ibm_watsonx_orchestrate.run.context import AgentRun

SEARCH_CONTROL_CONTEXT_KEY = "_shopper_search_control"
TURN_RESULT_CONTEXT_KEY = "_turn_result"
MAX_AGENT_PASSAGES = 12
MAX_PLAN_CONTEXT_ITEMS = 100
MAX_PLAN_CONTEXT_JSON_CHARS = 256_000
MAX_PLAN_ID_CHARS = 128
MAX_PLAN_NAME_CHARS = 512

PII_PATTERNS = (
    r"\b\d{3}-\d{2}-\d{4}\b",
    r"\b\d{9}\b",
    r"\b4[0-9]{12}(?:[0-9]{3})?\b",
    r"\b5[1-5][0-9]{14}\b",
    r"\b3[47][0-9]{13}\b",
    r"\bMRN(?:\s+is)?[\s#:]*\d+\b",
    r"\bmedical record\b",
    r"\bdiagnosis\b",
    r"\bSSN\b",
)
PII_IDENTIFICATION = "pii_phi_detected"
PII_DESCRIPTION = "PII or PHI pattern detected — processing stopped per G11/HIPAA"
PII_WARNING_MESSAGE = (
    "To keep your information safe, I'm not able to process messages that contain personal "
    "details like Social Security numbers, credit card numbers, or medical record information. "
    "Please remove that information from your message and try again — or if your question "
    "requires sharing personal details, please call our secure member services line where "
    "your information will be handled safely and confidentially."
)


def request_context(context: AgentRun | None) -> Mapping[str, Any]:
    """Return the request-context mapping when the runtime supplied one."""

    if context is None or context.request_context is None:
        return {}
    return context.request_context


def safe_plans(value: object) -> list[Any]:
    """Return a bounded native or JSON-encoded plan list, or an empty list on failure."""

    result: object = value
    if isinstance(result, str):
        # Bound attacker-controlled work before invoking the JSON parser. The decoded collection
        # is bounded separately because a short JSON string can still contain many scalar items.
        if len(result) > MAX_PLAN_CONTEXT_JSON_CHARS:
            return []
        try:
            result = json.loads(result)
        except (RecursionError, TypeError, ValueError):
            return []
    if not isinstance(result, list) or len(result) > MAX_PLAN_CONTEXT_ITEMS:
        return []
    return result


def contains_pii_phi(query: str) -> bool:
    """Return whether the current message contains a blocked PII/PHI pattern."""

    return any(re.search(pattern, str(query or ""), re.IGNORECASE) for pattern in PII_PATTERNS)


_PLAN_DASH_TRANSLATION = str.maketrans({character: "-" for character in "‐‑‒–—―−"})
_PLAN_QUOTE_TRANSLATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
    }
)
_SAFE_EDGE_PUNCTUATION = " \t\r\n.,;:!?\"'"


def plan_identity_key(value: Any) -> str:
    """Return a relaxed comparison key without changing the canonical display value."""

    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = normalized.translate(_PLAN_DASH_TRANSLATION).translate(_PLAN_QUOTE_TRANSLATION)
    normalized = "".join(
        "" if unicodedata.category(character) == "Cf" else character for character in normalized
    )
    normalized = "".join(" " if character.isspace() else character for character in normalized)
    normalized = re.sub(r" +", " ", normalized).strip(_SAFE_EDGE_PUNCTUATION)
    return normalized.casefold()


def authoritative_plans(
    context: AgentRun | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return bounded plan identities and recommendations constrained to that catalog."""

    ctx = request_context(context)
    available_input = safe_plans(ctx.get("application_available_plans", "[]"))
    recommended_input = safe_plans(ctx.get("application_recommended_plans", "[]"))
    # Keep bounds adjacent to the loops so both human reviewers and static analysis can verify
    # that request-controlled collection sizes cannot control unbounded work.
    if (
        len(available_input) > MAX_PLAN_CONTEXT_ITEMS
        or len(recommended_input) > MAX_PLAN_CONTEXT_ITEMS
    ):
        return [], []

    available: list[dict[str, Any]] = []
    available_by_id: dict[str, dict[str, Any]] = {}
    available_by_name: dict[str, dict[str, Any] | None] = {}
    for plan in available_input:
        if not isinstance(plan, dict):
            continue
        raw_plan_id = plan.get("plan_id")
        raw_plan_name = plan.get("plan_name")
        if not isinstance(raw_plan_id, str) or not isinstance(raw_plan_name, str):
            continue
        if len(raw_plan_id) > MAX_PLAN_ID_CHARS or len(raw_plan_name) > MAX_PLAN_NAME_CHARS:
            continue
        plan_id = raw_plan_id.strip()
        plan_name = raw_plan_name.strip()
        if not plan_id or not plan_name:
            continue
        plan_id_key = plan_identity_key(plan_id)
        if plan_id_key in available_by_id:
            continue
        # Only bounded identity fields are needed by routing. Do not propagate unrelated catalog
        # attributes into agent control, metadata, or telemetry.
        canonical_plan: dict[str, Any] = {"plan_id": plan_id, "plan_name": plan_name}
        available.append(canonical_plan)
        available_by_id[plan_id_key] = canonical_plan
        plan_name_key = plan_identity_key(plan_name)
        if plan_name_key in available_by_name:
            available_by_name[plan_name_key] = None
        else:
            available_by_name[plan_name_key] = canonical_plan

    def resolve(candidate: object) -> dict[str, Any] | None:
        if not isinstance(candidate, dict):
            return None
        raw_candidate_id = candidate.get("plan_id")
        raw_candidate_name = candidate.get("plan_name")
        candidate_id = (
            plan_identity_key(raw_candidate_id)
            if isinstance(raw_candidate_id, str) and len(raw_candidate_id) <= MAX_PLAN_ID_CHARS
            else ""
        )
        candidate_name = (
            plan_identity_key(raw_candidate_name)
            if isinstance(raw_candidate_name, str)
            and len(raw_candidate_name) <= MAX_PLAN_NAME_CHARS
            else ""
        )
        if candidate_id and candidate_id in available_by_id:
            return available_by_id[candidate_id]
        return available_by_name.get(candidate_name) if candidate_name else None

    recommended: list[dict[str, Any]] = []
    recommended_ids: set[str] = set()
    for candidate in recommended_input:
        matched = resolve(candidate)
        if matched is None:
            continue
        matched_id = plan_identity_key(matched.get("plan_id"))
        if matched_id not in recommended_ids:
            recommended.append(matched)
            recommended_ids.add(matched_id)
    return available, recommended


def resolve_current_plan(
    context: AgentRun | None,
    available: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve the current plan only when it uniquely matches the authoritative catalog."""

    current: object = request_context(context).get("user_current_plan", "")
    if isinstance(current, str):
        try:
            parsed = json.loads(current)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            current = parsed
    current_id = (
        str(current.get("plan_id", "")).strip()
        if isinstance(current, dict)
        else str(current).strip()
    )
    current_name = (
        str(current.get("plan_name", "")).strip()
        if isinstance(current, dict)
        else str(current).strip()
    )
    current_id_key = plan_identity_key(current_id)
    current_name_key = plan_identity_key(current_name)
    id_matches = [
        plan
        for plan in available
        if current_id_key and current_id_key == plan_identity_key(plan.get("plan_id"))
    ]
    if len(id_matches) == 1:
        return id_matches[0]
    name_matches = [
        plan
        for plan in available
        if current_name_key and current_name_key == plan_identity_key(plan.get("plan_name"))
    ]
    return name_matches[0] if len(name_matches) == 1 else None


def retrieval_text(value: str | None, plan_tokens: list[str], *, limit: int) -> str:
    """Normalize one retrieval field and remove identity handled by RAG filters."""

    text = re.sub(r"\s+", " ", str(value or "").strip())
    identity_aliases: set[str] = set()
    for token in plan_tokens:
        normalized = str(token or "").strip()
        if not normalized:
            continue
        identity_aliases.add(normalized)
        name_without_qualifier = re.sub(r"\s*\([^)]*\)\s*$", "", normalized).strip()
        if name_without_qualifier:
            identity_aliases.add(name_without_qualifier)
    for token in sorted(identity_aliases, key=len, reverse=True):
        words = cast(list[str], re.findall(r"[A-Za-z0-9]+", token))
        if not words:
            continue
        escaped_words: list[str] = [re.escape(word) for word in words]
        flexible_identity = r"[^A-Za-z0-9]+".join(escaped_words) + r"[\)\]\}]?"
        text = re.sub(
            rf"(?<![A-Za-z0-9]){flexible_identity}(?![A-Za-z0-9])",
            " ",
            text,
            flags=re.IGNORECASE,
        )
    text = re.sub(
        r"\b(?:my|our|the|this|that|current|existing|recommended)\s+plans?\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+([,.;:?!])", r"\1", re.sub(r"\s+", " ", text)).strip(" ,;:-")
    if not any(character.isalnum() for character in text):
        return ""
    return text[:limit]


def _decoded_control(
    runtime: AgentRun,
    missing_message: str,
) -> dict[str, Any]:
    raw = request_context(runtime).get(SEARCH_CONTROL_CONTEXT_KEY, "")
    if not raw:
        raise ValueError(missing_message)
    try:
        value: Any = raw
        for _ in range(2):
            if not isinstance(value, str):
                break
            value = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("trusted search control is invalid") from exc
    if not isinstance(value, dict) or value.get("route") != "search_turn":
        raise ValueError(missing_message)
    return value


def trusted_plan_control(runtime: AgentRun) -> dict[str, Any]:
    """Require a trusted nonterminal pre-invoke marker before plan retrieval."""

    return _decoded_control(
        runtime,
        "trusted search control is missing",
    )


def trusted_general_control(runtime: AgentRun) -> dict[str, Any]:
    """Require a trusted nonterminal pre-invoke marker before general retrieval."""

    return _decoded_control(
        runtime,
        "trusted general search control is missing",
    )
