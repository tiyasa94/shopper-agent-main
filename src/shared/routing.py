"""Pure route policy for the shopper pre-invoke plugin."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from ibm_watsonx_orchestrate.run.context import AgentRun

from shared.classifier_contract import GuardrailDecision
from shared.context import request_context
from shared.effective_date import (
    is_personal_effective_date_query,
    requested_effective_date_message,
    validated_requested_effective_date,
)
from shared.guardrails import resolve_guardrail
from shared.response_composer import ResponseCompositionSpec

PLAN_SEARCH_TOOL = "search_plans"
GENERAL_SEARCH_TOOL = "search_general_documents"
PLAN_DETAILS_TOOL = "get_plan_details"
SEARCH_TOOLS = (PLAN_DETAILS_TOOL, PLAN_SEARCH_TOOL, GENERAL_SEARCH_TOOL)
SEARCH_TOOLS_WITHOUT_PLAN_DETAILS = (PLAN_SEARCH_TOOL, GENERAL_SEARCH_TOOL)
STRUCTURED_PLAN_QUOTE_UNAVAILABLE_REASON = "incomplete_application_context"
STRUCTURED_PLAN_QUOTE_RECOVERY = "complete_information_on_website"
STRUCTURED_PLAN_QUOTE_USER_MESSAGE = (
    "To show personalized premiums and plan costs, please complete the required information "
    "on the website and try again."
)
STRUCTURED_PLAN_QUOTE_AVAILABLE_INSTRUCTION = (
    "Current quote context is sufficient. Use get_plan_details for plan-specific structured "
    "facts; never give website-completion guidance."
)
STRUCTURED_PLAN_QUOTE_UNAVAILABLE_INSTRUCTION = (
    "After plan scope is resolved, current quote context is insufficient for plan-specific "
    "structured facts. Do not use search_plans as a substitute; tell the shopper to complete "
    "the required information on the website and try again. Do not ask for personal information "
    "in chat."
)
GENERIC_PROCESSING_ERROR_MESSAGE = (
    "I'm sorry, I couldn't process that request right now. Please try again."
)
BusinessIntent = Literal["specific_plan", "broad_plans", "generic_info"]
RouteKind = Literal["terminal", "search_turn"]


@dataclass(frozen=True)
class PlanReference:
    """The only plan identity fields exposed through trusted agent control."""

    plan_id: str
    plan_name: str

    @classmethod
    def from_mapping(cls, plan: Mapping[str, object]) -> "PlanReference":
        return cls(
            plan_id=str(plan.get("plan_id", "")).strip(),
            plan_name=str(plan.get("plan_name", "")).strip(),
        )

    def to_wire(self) -> dict[str, str]:
        return {"plan_id": self.plan_id, "plan_name": self.plan_name}


@dataclass(frozen=True)
class PlanContextEntry:
    """One normalized entry in the trusted plan identity lookup."""

    plan_id: str
    plan_name: str
    current: bool
    recommended: bool

    def to_wire(self) -> dict[str, str | bool]:
        return {
            "plan_id": self.plan_id,
            "plan_name": self.plan_name,
            "current": self.current,
            "recommended": self.recommended,
        }


@dataclass(frozen=True)
class SearchTurnControl:
    """Trusted neutral control for one agent-selected retrieval turn."""

    route: Literal["search_turn"] = field(default="search_turn", init=False)
    current_user_query: str = ""
    plan_lookup: tuple[PlanContextEntry, ...] = ()
    canonical_current_plan: PlanReference | None = None
    selection_only: bool = False
    selected_plans: tuple[PlanReference, ...] = ()
    current_turn_search_required: Literal[True] = field(default=True, init=False)
    structured_plan_quote_available: bool = False

    def to_wire(self) -> dict[str, object]:
        value: dict[str, object] = {
            "route": self.route,
            "current_user_query": self.current_user_query,
            "plan_lookup": [plan.to_wire() for plan in self.plan_lookup],
            "selection_only": self.selection_only,
            "current_turn_search_required": self.current_turn_search_required,
            "structured_plan_quote_available": self.structured_plan_quote_available,
        }
        if not self.structured_plan_quote_available:
            value.update(
                {
                    "structured_plan_quote_unavailable_reason": (
                        STRUCTURED_PLAN_QUOTE_UNAVAILABLE_REASON
                    ),
                    "structured_plan_quote_recovery": STRUCTURED_PLAN_QUOTE_RECOVERY,
                    "structured_plan_quote_user_message": STRUCTURED_PLAN_QUOTE_USER_MESSAGE,
                }
            )
        if self.canonical_current_plan is not None:
            value["canonical_current_plan"] = self.canonical_current_plan.to_wire()
        return value

    def to_model_wire(self) -> dict[str, object]:
        """Expose turn policy while keeping the tool authorization catalog internal."""

        value = self.to_wire()
        value.pop("current_user_query")
        value.pop("plan_lookup")
        value["plan_catalog_available"] = bool(self.plan_lookup)
        value["structured_plan_quote_instruction"] = (
            STRUCTURED_PLAN_QUOTE_AVAILABLE_INSTRUCTION
            if self.structured_plan_quote_available
            else STRUCTURED_PLAN_QUOTE_UNAVAILABLE_INSTRUCTION
        )
        return value


RouteControl = SearchTurnControl


@dataclass(frozen=True)
class RouteDecision:
    """One complete policy outcome ready for payload materialization."""

    kind: RouteKind
    business_intent: BusinessIntent | None
    tools: tuple[str, ...] = ()
    control: RouteControl | None = None
    message: str | None = None
    guardrail_code: str | None = None
    reason: str | None = None
    escalation: Mapping[str, object] | None = None
    audit_required: bool = False
    response_composition: ResponseCompositionSpec | None = None

    def __post_init__(self) -> None:
        if self.audit_required:
            required_escalation_fields = {"type", "identification", "description"}
            if self.escalation is None or not required_escalation_fields.issubset(self.escalation):
                raise ValueError("an audited route requires a complete escalation payload")
        if self.kind == "terminal":
            if not self.message:
                raise ValueError("a terminal route requires a message")
            if self.tools:
                raise ValueError("a terminal route cannot expose tools")
            if self.control is not None:
                raise ValueError("a terminal route cannot carry search control")
            if (
                self.response_composition is not None
                and self.message != self.response_composition.fallback_response
            ):
                raise ValueError("a terminal composition requires its canned fallback message")
            return
        if self.response_composition is not None:
            raise ValueError("a controlled route cannot carry response composition")
        if self.message is not None:
            raise ValueError("a controlled route cannot carry a terminal message")
        if self.control is None:
            raise ValueError("a controlled route requires typed search control")
        if not isinstance(self.control, SearchTurnControl):
            raise ValueError("a controlled route requires typed search control")
        if self.control.route != self.kind:
            raise ValueError("route control disagrees with policy kind")
        if self.kind != "search_turn":
            raise ValueError("a controlled route must be a neutral search turn")


@dataclass(frozen=True)
class ShopperRoutingRequest:
    """Trusted request state consumed by deterministic route policy."""

    query: str
    runtime: AgentRun
    market_segment: str
    available_plans: tuple[PlanReference, ...]
    recommended_plans: tuple[PlanReference, ...]
    current_plan: PlanReference | None
    deterministic_general: bool = False
    deterministic_selection_continuation: bool = False


def terminal_route(
    message: str,
    *,
    business_intent: BusinessIntent = "generic_info",
    guardrail_code: str | None = None,
    reason: str | None = None,
    escalation: Mapping[str, object] | None = None,
    audit_required: bool = False,
    response_composition: ResponseCompositionSpec | None = None,
) -> RouteDecision:
    """Construct a terminal route through one invariant-checked factory."""

    return RouteDecision(
        kind="terminal",
        business_intent=business_intent,
        message=message,
        guardrail_code=guardrail_code,
        reason=reason,
        escalation=escalation,
        audit_required=audit_required,
        response_composition=response_composition,
    )


def _structured_plan_quote_available(request: ShopperRoutingRequest) -> bool:
    """Return whether trusted context contains a usable market-matched plan quote."""

    ctx = request_context(request.runtime)
    value = ctx.get("application_plan_quote_inputs")
    if not isinstance(value, dict) or not value:
        return False
    if not all(
        isinstance(ctx.get(key), str) and bool(ctx[key].strip())
        for key in ("user_brand", "user_state_code", "user_requested_eff_date")
    ):
        return False
    segment = request.market_segment.strip().casefold()
    applicants = value.get("applicants")
    if not isinstance(applicants, list) or not applicants:
        return False
    if not all(value.get(key) for key in ("zip_code", "county_code", "county_name")):
        return False
    return segment == "ind" or (segment == "medicare" and len(applicants) == 1)


def _current_message_plan_matches(
    query: str,
    plans: tuple[PlanReference, ...],
) -> tuple[str, ...]:
    """Return unique canonical plan names stated in full in the current message.

    Matching uses alphanumeric token sequences so Unicode spaces and dashes do not change the
    result. When one canonical name prefixes another at the same position, only the longest name
    is retained. Duplicate canonical names remain unresolved and are intentionally omitted.
    """

    query_tokens = tuple(re.findall(r"[a-z0-9]+", query.casefold()))
    if not query_tokens:
        return ()

    plans_by_tokens: dict[tuple[str, ...], list[PlanReference]] = {}
    for plan in plans:
        aliases = {
            plan.plan_name,
            re.sub(r"\s*\([^)]*\)\s*$", "", plan.plan_name).strip(),
        }
        for alias in aliases:
            name_tokens = tuple(re.findall(r"[a-z0-9]+", alias.casefold()))
            if name_tokens:
                plans_by_tokens.setdefault(name_tokens, []).append(plan)

    occurrences: list[tuple[int, int, str]] = []
    for name_tokens, matching_plans in plans_by_tokens.items():
        if len(matching_plans) != 1:
            continue
        width = len(name_tokens)
        for start in range(len(query_tokens) - width + 1):
            if query_tokens[start : start + width] == name_tokens:
                prefix = query_tokens[max(0, start - 2) : start]
                if {"not", "except", "excluding", "without"}.intersection(prefix):
                    continue
                occurrences.append((start, start + width, matching_plans[0].plan_name))

    retained = [
        occurrence
        for occurrence in occurrences
        if not any(
            other_start == occurrence[0] and other_end > occurrence[1]
            for other_start, other_end, _ in occurrences
        )
    ]
    ordered_names: list[str] = []
    for _, _, plan_name in sorted(retained):
        if plan_name not in ordered_names:
            ordered_names.append(plan_name)
    return tuple(ordered_names)


def _is_named_plan_selection_only(query: str, matched_plan_names: tuple[str, ...]) -> bool:
    """Return whether exact current-catalog names consume the shopper's whole reply."""

    if not matched_plan_names:
        return False
    normalized = re.sub(r"[^a-z0-9]+", " ", query.casefold()).strip()
    aliases: set[str] = set()
    for plan_name in matched_plan_names:
        for alias in (plan_name, re.sub(r"\s*\([^)]*\)\s*$", "", plan_name).strip()):
            normalized_alias = re.sub(r"[^a-z0-9]+", " ", alias.casefold()).strip()
            if normalized_alias:
                aliases.add(normalized_alias)
    for alias in sorted(aliases, key=len, reverse=True):
        normalized = re.sub(rf"\b{re.escape(alias)}\b", " ", normalized)
    residual = set(re.findall(r"[a-z0-9]+", normalized))
    return residual <= {"and", "medicare", "medsup", "or", "plan", "supplement", "the"}


def build_search_control(request: ShopperRoutingRequest) -> SearchTurnControl:
    """Build neutral trusted search control for the current turn."""

    structured_plan_quote_available = _structured_plan_quote_available(request)
    recommended_ids = {plan.plan_id for plan in request.recommended_plans}
    current_id = request.current_plan.plan_id if request.current_plan is not None else None
    current_message_plan_matches = _current_message_plan_matches(
        request.query, request.available_plans
    )
    selection_only = _is_named_plan_selection_only(request.query, current_message_plan_matches)

    return SearchTurnControl(
        current_user_query=request.query,
        plan_lookup=tuple(
            PlanContextEntry(
                plan_id=plan.plan_id,
                plan_name=plan.plan_name,
                current=plan.plan_id == current_id,
                recommended=plan.plan_id in recommended_ids,
            )
            for plan in request.available_plans
        ),
        canonical_current_plan=request.current_plan,
        selection_only=selection_only,
        selected_plans=tuple(
            plan
            for plan in request.available_plans
            if selection_only and plan.plan_name in current_message_plan_matches
        ),
        structured_plan_quote_available=structured_plan_quote_available,
    )


def _search_route(request: ShopperRoutingRequest) -> RouteDecision:
    """Expose only the retrieval capabilities supported by current application context."""

    control = build_search_control(request)
    tools = (
        SEARCH_TOOLS
        if control.structured_plan_quote_available
        else SEARCH_TOOLS_WITHOUT_PLAN_DETAILS
    )
    return RouteDecision(
        kind="search_turn",
        business_intent=None,
        tools=tools,
        control=control,
    )


def resolve_policy(
    request: ShopperRoutingRequest,
    decision: GuardrailDecision | None,
) -> RouteDecision:
    """Resolve classifier evidence and trusted context through deterministic business policy."""

    if request.deterministic_general:
        if request.deterministic_selection_continuation:
            raise ValueError("deterministic general and selection routing cannot both be active")
        if decision is not None:
            raise ValueError("a deterministic general route cannot include classifier evidence")
        return _search_route(request)
    if request.deterministic_selection_continuation:
        if decision is not None:
            raise ValueError("selection continuation cannot include classifier evidence")
        return _search_route(request)
    if decision is None:
        raise ValueError("classifier evidence is required outside a deterministic route")

    resolution = resolve_guardrail(decision, request.runtime)
    if resolution is not None:
        return terminal_route(
            resolution.message,
            business_intent=resolution.business_intent,
            guardrail_code=resolution.code,
            escalation=resolution.audit_payload,
            audit_required=resolution.audit_payload is not None,
            response_composition=resolution.response_composition,
        )

    effective_date_signal = decision.enroll_now_effective_date_question or (
        is_personal_effective_date_query(request.query)
    )
    if request.market_segment.strip().casefold() == "medicare" and effective_date_signal:
        requested_effective_date = request_context(request.runtime).get("user_requested_eff_date")
        return terminal_route(
            requested_effective_date_message(requested_effective_date),
            reason=(
                "context_requested_effective_date"
                if validated_requested_effective_date(requested_effective_date) is not None
                else "unestablished_personal_effective_date"
            ),
        )

    return _search_route(request)
