"""Local-only, single-turn shopper guardrails implemented as a pre-invoke plugin.

The plugin classifies the current message, resolves deterministic policy against trusted context,
and materializes one complete route. Unexpected failures collapse to a dependency-free terminal
result with no tools.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, cast

from ibm_watsonx_orchestrate.agent_builder.connections import (
    ConnectionType,
    ExpectedCredentials,
)
from ibm_watsonx_orchestrate.agent_builder.tools import tool
from ibm_watsonx_orchestrate.agent_builder.tools.types import (
    AgentPreInvokePayload,
    AgentPreInvokeResult,
    PluginContext,
    PythonToolKind,
)
from ibm_watsonx_orchestrate.run import connections
from ibm_watsonx_orchestrate.run.context import AgentRun

from shared.audit import emit_audit_event
from shared.classifier import (
    ClassificationTelemetry,
    ClassifierError,
    failure_telemetry,
)
from shared.classifier import (
    classify as _classify_request,
)
from shared.classifier_contract import GuardrailDecision
from shared.context import (
    PII_DESCRIPTION,
    PII_IDENTIFICATION,
    PII_WARNING_MESSAGE,
    SEARCH_CONTROL_CONTEXT_KEY,
    TURN_RESULT_CONTEXT_KEY,
    authoritative_plans,
    contains_pii_phi,
    resolve_current_plan,
)
from shared.guardrails import greeting_response, plan_product_unavailable_response
from shared.response_addenda import RESPONSE_ADDENDA_CONTEXT_KEY
from shared.response_composer import CompositionResult, compose_response
from shared.routing import (
    GENERAL_SEARCH_TOOL,
    GENERIC_PROCESSING_ERROR_MESSAGE,
    PLAN_DETAILS_TOOL,
    PLAN_SEARCH_TOOL,
    SEARCH_TOOLS,
    SEARCH_TOOLS_WITHOUT_PLAN_DETAILS,
    PlanReference,
    RouteDecision,
    SearchTurnControl,
    ShopperRoutingRequest,
    resolve_policy,
    terminal_route,
)

CLASSIFIER_APP_ID = "elevance-wxo-inference"
CLASSIFIER_CONNECTION_TYPE = cast(ConnectionType, cast(object, ConnectionType.KEY_VALUE))
CONTROL_TAG = "shopper_control"
_UNAVAILABLE_CARRIER_NAME = (
    r"(?:aetna|cigna|humana|kaiser(?: permanente)?|unitedhealthcare|united healthcare|uhc|"
    r"molina|oscar(?: health)?|ambetter)"
)
_UNAVAILABLE_CARRIER_PRODUCT_NAME = (
    r"(?:aetna|cigna|humana|kaiser(?: permanente)?|unitedhealthcare|united healthcare|uhc|"
    r"molina|oscar health|ambetter)"
)
_UNAVAILABLE_CARRIER_PRODUCT_PATTERN = re.compile(
    rf"\b{_UNAVAILABLE_CARRIER_NAME}\b(?:\s+[a-z0-9&'-]+){{0,4}}\s+"
    rf"(?:plan|coverage|insurance)\b|\b(?:plan|coverage|insurance)\s+"
    rf"(?:from|through|with)\s+{_UNAVAILABLE_CARRIER_NAME}\b|"
    rf"\b{_UNAVAILABLE_CARRIER_NAME}\b(?:\s+[a-z0-9&'-]+){{0,4}}\s+"
    rf"(?:covers?|includes?|offers?|costs?|deductible|copay|coinsurance|premium)\b|"
    rf"\b(?:tell me about|compare)\b.{{0,160}}\b{_UNAVAILABLE_CARRIER_PRODUCT_NAME}\b"
    rf"(?!\s+(?:(?:permanente|health)\s+)?(?:facility|hospital|clinic|doctor|provider)\b)",
    flags=re.IGNORECASE,
)
_FACTUAL_RATIONALE_CLARIFICATION = (
    "Which specific benefits, services, or costs would you like to review for that plan?"
)
_PERSONALIZED_OUTCOME_PATTERN = re.compile(
    r"\b(?:why|what caused|explain why)\b.{0,120}\bmy\b.{0,120}"
    r"\b(?:bill|charge|claim|cost|coverage|eligibility|premium|rate)\b",
    flags=re.IGNORECASE,
)
_NO_ACTION_RENEWAL_PATTERN = re.compile(
    r"\b(?:if i (?:do not|don't) (?:make )?(?:any )?changes|if i (?:take )?no action|"
    r"without (?:making )?changes|make no changes)\b.{0,120}"
    r"\b(?:open|annual) enrollment\b|\b(?:open|annual) enrollment\b.{0,120}"
    r"\b(?:do not|don't) (?:make )?(?:any )?changes\b",
    flags=re.IGNORECASE,
)
_NO_ACTION_RENEWAL_MESSAGE = (
    "The available information does not establish what happens to your current Medicare coverage "
    "if you make no changes during Open Enrollment. Check your renewal notice or contact Member "
    "Services for the plan-specific outcome."
)
_PERSONAL_ELIGIBILITY_PATTERN = re.compile(
    r"\b(?:do i|would i|am i|how (?:can|would) i (?:know )?(?:if|whether) i)\b.{0,100}"
    r"\b(?:qualify|eligible)\b|\bwould\b.{0,100}\b(?:move|moving|marriage|birth|loss)\b"
    r".{0,100}\bcount\b",
    flags=re.IGNORECASE,
)
_PERSONAL_ELIGIBILITY_MESSAGE = (
    "I can't determine whether you qualify from this information. Eligibility rules and deadlines "
    "depend on your circumstances; verify them through the official enrollment application or a "
    "licensed agent."
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeterministicGeneralRule:
    """One named full-message rule that may safely bypass the classifier."""

    name: str
    pattern: re.Pattern[str]
    rationale: str

    def matches(self, normalized_query: str) -> bool:
        return self.pattern.fullmatch(normalized_query) is not None


def _compile_text_pattern(pattern: str) -> re.Pattern[str]:
    """Keep ``re.compile`` on its string-pattern overload for static analyzers."""

    return re.compile(pattern)


_OBVIOUS_GREETING_PATTERN = _compile_text_pattern(
    r"(?:(?:(?:hi|hello|hey)(?: there)?|hiya|howdy|greetings|"
    r"good (?:morning|afternoon|evening|day)|morning|afternoon|evening)"
    r"(?: (?:how are you(?: doing)?|how s it going)(?: today)?)?|"
    r"how are you(?: doing)?(?: today)?|how s it going)"
)


DETERMINISTIC_GENERAL_RULES: tuple[DeterministicGeneralRule, ...] = (
    DeterministicGeneralRule(
        name="coverage_change_timing_education",
        pattern=_compile_text_pattern(
            r"when (?:can|may|do) i (?:change|switch) (?:my )?(?:plan|coverage)"
        ),
        rationale="A full-message timing question is education rather than an enrollment action.",
    ),
    DeterministicGeneralRule(
        name="insurance_term_definition",
        pattern=_compile_text_pattern(
            r"(?:what (?:is|are)|define|explain|how (?:does|do)) (?:an? )?"
            r"(?:deductible|coinsurance|co ?pay(?:ment)?|premium|network|formulary|hmo|ppo|epo)"
            r"(?: work| mean| in simple terms)?"
        ),
        rationale="A full-message insurance-term definition is plan-independent education.",
    ),
    DeterministicGeneralRule(
        name="renewal_definition",
        pattern=_compile_text_pattern(
            r"(?:what is|what does|define|explain|how does) "
            r"(?:health insurance |plan |coverage )?renewal"
            r"(?: work| mean| in simple terms)?"
        ),
        rationale="A full-message renewal definition is plan-independent education.",
    ),
)

_FACTUAL_PLAN_RATIONALE_PATTERN = _compile_text_pattern(
    r"(?:(?:what|which) (?:are )?(?:the )?(?:main )?(?:factual )?"
    r"(?:reasons|features|advantages|tradeoffs) (?:that )?"
    r"(?:someone|people|a shopper) (?:might|may|could|would) "
    r"(?:choose|consider)|why (?:might|may|could|would) "
    r"(?:someone|people|a shopper) (?:choose|consider)) [a-z0-9 ]{1,180}"
)

_SELECTION_CONTINUATION_PATTERN = _compile_text_pattern(
    r"(?:both(?: plans?)?|either(?: one| plan)?|(?:the )?(?:first|second)(?: one| plan)?|"
    r"all(?: of)? (?:them|these|those)(?: plans?)?)"
)


def _deterministic_general_rule(query: str) -> DeterministicGeneralRule | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", query.casefold()).strip()
    return next(
        (rule for rule in DETERMINISTIC_GENERAL_RULES if rule.matches(normalized)),
        None,
    )


def _is_obvious_greeting(query: str) -> bool:
    """Recognize only an unmistakable greeting that occupies the full message."""

    normalized = re.sub(r"[^a-z0-9]+", " ", query.casefold()).strip()
    return _OBVIOUS_GREETING_PATTERN.fullmatch(normalized) is not None


def _is_factual_plan_rationale(query: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", query.casefold()).strip()
    return _FACTUAL_PLAN_RATIONALE_PATTERN.fullmatch(normalized) is not None


def _is_selection_continuation(query: str) -> bool:
    """Recognize only a complete bare plan-selection reply."""

    normalized = re.sub(r"[^a-z0-9]+", " ", query.casefold()).strip()
    return _SELECTION_CONTINUATION_PATTERN.fullmatch(normalized) is not None


def _context_dict(plugin_context: PluginContext, payload: AgentPreInvokePayload) -> dict[str, Any]:
    """Merge documented plugin context locations without trusting nested instructions."""

    merged: dict[str, Any] = {}

    def include(value: Any) -> None:
        if not isinstance(value, dict):
            return
        for nested_key in ("context", "context_variables", "request_context"):
            nested = value.get(nested_key)
            if isinstance(nested, dict):
                merged.update(nested)
        nested_keys = {"context", "context_variables", "request_context"}
        merged.update({key: item for key, item in value.items() if key not in nested_keys})

    include(plugin_context.state)
    include(payload.context)
    return merged


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    text = getattr(content, "text", None)
    return text if isinstance(text, str) else ""


def _current_query(payload: AgentPreInvokePayload) -> str:
    for message in reversed(payload.messages or []):
        if str(getattr(message, "role", "")).casefold().endswith("user"):
            text = _message_text(message).strip()
            if text:
                return text
    return ""


def _connection() -> dict[str, Any]:
    return dict(connections.key_value(CLASSIFIER_APP_ID))


def _log_classifier_telemetry(telemetry: ClassificationTelemetry, *, success: bool) -> None:
    log_method: Callable[..., None] = logger.info if success else logger.warning
    event = {
        "event": "shopper_classifier_completed" if success else "shopper_classifier_failed",
        **telemetry.log_fields(),
    }
    # The local WXO runtime formatter drops arbitrary ``logging.extra`` fields. Serializing this
    # closed, content-free payload keeps the telemetry observable without logging model content.
    log_method(
        json.dumps(event, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
    )


def _log_composition_result(result: CompositionResult) -> None:
    log_method: Callable[..., None] = logger.warning if result.used_fallback else logger.info
    event = {
        "event": (
            "shopper_response_composer_fallback"
            if result.used_fallback
            else "shopper_response_composer_completed"
        ),
        **result.log_fields(),
    }
    log_method(json.dumps(event, ensure_ascii=True, separators=(",", ":"), sort_keys=True))


def _classify(query: str, market_segment: str) -> GuardrailDecision:
    try:
        config = _connection()
    except Exception as exc:
        telemetry = failure_telemetry("configuration_error")
        _log_classifier_telemetry(telemetry, success=False)
        raise ClassifierError(telemetry) from exc

    try:
        result = _classify_request(
            query,
            market_segment,
            config,
            GuardrailDecision,
        )
    except ClassifierError as exc:
        _log_classifier_telemetry(exc.telemetry, success=False)
        raise
    _log_classifier_telemetry(result.telemetry, success=True)
    return result.decision


def _compose_route_response(route: RouteDecision, query: str) -> RouteDecision:
    """Resolve optional terminal composition without changing route policy."""

    spec = route.response_composition
    if spec is None:
        return route
    try:
        config = _connection()
    except Exception:
        result = CompositionResult(
            message=spec.fallback_response,
            template_id=spec.template_id,
            used_fallback=True,
            attempts=0,
            failure_category="configuration_error",
        )
    else:
        try:
            result = compose_response(spec, query, config)
        except Exception:
            result = CompositionResult(
                message=spec.fallback_response,
                template_id=spec.template_id,
                used_fallback=True,
                attempts=0,
                failure_category="internal_error",
            )
    _log_composition_result(result)
    return replace(route, message=result.message, response_composition=None)


def _renderer_prompt(message: str) -> str:
    control = {"route": "terminal", "message": message}
    return (
        "You are a response renderer. Read the trusted shopper_control JSON below. Output its "
        "message value exactly, with no additions, markdown, tool calls, or explanation.\n"
        f"<{CONTROL_TAG}>{json.dumps(control, ensure_ascii=False)}</{CONTROL_TAG}>"
    )


_EMERGENCY_RENDERER_PROMPT = _renderer_prompt(GENERIC_PROCESSING_ERROR_MESSAGE)


def _validate_policy_tools(route: RouteDecision) -> None:
    """Assert that controlled turns expose the bounded search capability set."""

    tools = set(route.tools)
    if not tools.issubset({PLAN_DETAILS_TOOL, PLAN_SEARCH_TOOL, GENERAL_SEARCH_TOOL}):
        raise ValueError("route selected an unknown tool")
    if route.kind == "terminal" and tools:
        raise ValueError("terminal route selected tools")
    if route.kind == "search_turn" and not isinstance(route.control, SearchTurnControl):
        raise ValueError("search turn requires typed neutral search control")
    if route.kind == "search_turn":
        expected = (
            set(SEARCH_TOOLS)
            if route.control.structured_plan_quote_available
            else set(SEARCH_TOOLS_WITHOUT_PLAN_DETAILS)
        )
        if tools != expected:
            raise ValueError("search turn tools must match structured quote availability")


def _materialize_route(
    payload: AgentPreInvokePayload,
    route: RouteDecision,
    *,
    metadata: dict[str, Any],
    runtime: AgentRun,
) -> AgentPreInvokeResult:
    """Apply one complete route without reinterpreting classifier evidence or trusted context."""

    if route.response_composition is not None:
        raise ValueError("route response composition must be resolved before materialization")
    _validate_policy_tools(route)
    result_metadata = dict(metadata)
    result_metadata["route"] = route.kind
    if route.guardrail_code:
        result_metadata["guardrail"] = route.guardrail_code
    if route.reason:
        result_metadata["reason"] = route.reason

    modified = payload.model_copy(deep=True)
    modified.context = dict(modified.context or {})
    modified.context[RESPONSE_ADDENDA_CONTEXT_KEY] = "{}"

    if route.kind == "terminal":
        normalized_escalation = dict(route.escalation or {"type": False})
        audit_marker: dict[str, Any] = {"audit_completed": False}
        if route.audit_required:
            receipt = emit_audit_event(
                normalized_escalation,
                runtime,
                request_id=str(result_metadata.get("request_id", "")),
                event_logger=logger,
            )
            normalized_escalation = dict(receipt.escalation)
            audit_marker = {"audit_completed": receipt.logged}
            result_metadata["audit_completed"] = receipt.logged
            result_metadata["audit_identification"] = str(
                receipt.escalation.get("identification", "")
            )

        modified.tools = []
        modified.context[TURN_RESULT_CONTEXT_KEY] = json.dumps(
            {
                "schema_version": 1,
                "route": "terminal",
                "business_intent": route.business_intent,
                "escalation": normalized_escalation,
                **audit_marker,
            },
            ensure_ascii=False,
        )
        modified.system_prompt = _renderer_prompt(route.message or GENERIC_PROCESSING_ERROR_MESSAGE)
        if modified.messages:
            modified.messages[-1].content.text = route.message or GENERIC_PROCESSING_ERROR_MESSAGE
        # This event is intentionally content-free: request and plan-derived values must not be
        # propagated to an info-level logging sink. Detailed safety events use the audit channel.
        logger.info("shopper_guardrail_blocked")
        return AgentPreInvokeResult(
            continue_processing=False,
            modified_payload=modified,
            metadata=result_metadata,
        )

    if route.control is None:
        raise ValueError("controlled route is missing typed control")
    if (
        route.control.selection_only
        and len(route.control.selected_plans) == 1
        and modified.messages
        and modified.messages[-1].role == "user"
    ):
        canonical_name = route.control.selected_plans[0].plan_name
        original_text = modified.messages[-1].content.text
        # The existing matcher permits only catalog names and generic selection words here.
        # Normalize descriptive prefixes for inference; retain the original query in control.
        if re.findall(r"[a-z0-9]+", original_text.casefold()) != re.findall(
            r"[a-z0-9]+", canonical_name.casefold()
        ):
            modified.messages[-1].content.text = canonical_name
            result_metadata["plan_selection_normalized"] = True
    control = route.control.to_wire()
    model_control = route.control.to_model_wire()
    encoded_control = json.dumps(control, ensure_ascii=False)
    encoded_model_control = json.dumps(model_control, ensure_ascii=False)
    modified.tools = list(route.tools)
    modified.context[SEARCH_CONTROL_CONTEXT_KEY] = encoded_control
    turn_result: dict[str, Any] = {
        "schema_version": 1,
        "route": route.kind,
        "escalation": {"type": False},
    }
    if route.business_intent is not None:
        turn_result["business_intent"] = route.business_intent
    modified.context[TURN_RESULT_CONTEXT_KEY] = json.dumps(turn_result, ensure_ascii=False)
    suffix = (
        f"\n\nTrusted current-turn context:\n<{CONTROL_TAG}>{encoded_model_control}</{CONTROL_TAG}>"
    )
    modified.system_prompt = f"{modified.system_prompt or ''}{suffix}"
    # Keep routine telemetry content-free so plan availability and recommendation data cannot
    # reach the logging sink, directly or as derived values.
    logger.info("shopper_turn_classified")
    return AgentPreInvokeResult(
        continue_processing=True,
        modified_payload=modified,
        metadata=result_metadata,
    )


def _emergency_terminal_result(payload: AgentPreInvokePayload) -> AgentPreInvokeResult:
    """Return one hard-coded no-tool result without audit, logging, or external dependencies."""

    modified = payload.model_copy(deep=True)
    modified.tools = []
    modified.context = dict(modified.context or {})
    modified.context[RESPONSE_ADDENDA_CONTEXT_KEY] = "{}"
    modified.context[TURN_RESULT_CONTEXT_KEY] = json.dumps(
        {
            "schema_version": 1,
            "route": "terminal",
            "business_intent": "generic_info",
            "escalation": {
                "type": True,
                "identification": "classifier_error",
                "description": "The current message could not be processed",
            },
            "audit_completed": False,
        },
        ensure_ascii=False,
    )
    modified.system_prompt = _EMERGENCY_RENDERER_PROMPT
    if modified.messages:
        modified.messages[-1].content.text = GENERIC_PROCESSING_ERROR_MESSAGE
    return AgentPreInvokeResult(
        continue_processing=False,
        modified_payload=modified,
        metadata={"route": "terminal", "reason": "pipeline_error"},
    )


def _resolve_safety(query: str, context: AgentRun | None) -> RouteDecision | None:
    if not query:
        return terminal_route(
            "I'm sorry, I couldn't process that request. Please try again.",
            reason="missing_user_message",
            escalation={
                "type": True,
                "identification": "classifier_error",
                "description": "The current message could not be classified",
            },
        )
    if contains_pii_phi(query):
        return terminal_route(
            PII_WARNING_MESSAGE,
            guardrail_code="G11",
            escalation={
                "type": True,
                "identification": PII_IDENTIFICATION,
                "description": PII_DESCRIPTION,
            },
            audit_required=True,
        )
    if _UNAVAILABLE_CARRIER_PRODUCT_PATTERN.search(query):
        return terminal_route(
            plan_product_unavailable_response(context),
            business_intent="specific_plan",
            guardrail_code="G12",
            reason="unavailable_external_carrier",
        )
    return None


@tool(
    name="shopper_guardrails",
    description="Shopper pre-invoke guardrail classifier",
    kind=PythonToolKind.AGENTPREINVOKE,
    expected_credentials=[
        ExpectedCredentials(app_id=CLASSIFIER_APP_ID, type=CLASSIFIER_CONNECTION_TYPE)
    ],
)
async def shopper_guardrails(
    plugin_context: PluginContext,
    agent_pre_invoke_payload: AgentPreInvokePayload,
) -> AgentPreInvokeResult:
    """Classify and guard one current message before the native agent handles continuity."""

    try:
        query = _current_query(agent_pre_invoke_payload)
        context = _context_dict(plugin_context, agent_pre_invoke_payload)
        runtime = AgentRun(request_context=dict(context))
        available, recommended = authoritative_plans(runtime)
        payload = agent_pre_invoke_payload.model_copy(deep=True)
        payload.context = dict(payload.context or {})
        sanitized_available_plans = json.dumps(
            available,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        raw_available_plans = context.get("application_available_plans")
        payload.context["application_available_plans"] = sanitized_available_plans
        if isinstance(raw_available_plans, str) and payload.system_prompt:
            payload.system_prompt = payload.system_prompt.replace(
                raw_available_plans,
                sanitized_available_plans,
            )
        market_segment = str(context.get("application_market_segment", "")).strip()
        metadata = {
            "architecture": "preinvoke_single_turn",
            "classification_scope": "current_message_only",
            "allowed_plan_count": len(available),
            "request_id": str(getattr(plugin_context.global_context, "request_id", "") or ""),
        }
        current = resolve_current_plan(runtime, available)
        safety_route = _resolve_safety(query, runtime)
        if safety_route is not None:
            return _materialize_route(payload, safety_route, metadata=metadata, runtime=runtime)

        if _is_obvious_greeting(query):
            metadata["deterministic_rule"] = "obvious_greeting"
            route = terminal_route(
                greeting_response(),
                business_intent="generic_info",
                guardrail_code="G05",
                reason="obvious_greeting",
            )
            return _materialize_route(payload, route, metadata=metadata, runtime=runtime)

        selection_continuation = _is_selection_continuation(query)

        if market_segment.casefold() == "medicare" and _NO_ACTION_RENEWAL_PATTERN.search(query):
            route = terminal_route(
                _NO_ACTION_RENEWAL_MESSAGE,
                business_intent="generic_info",
                reason="unestablished_no_action_renewal_outcome",
            )
            return _materialize_route(payload, route, metadata=metadata, runtime=runtime)

        if _PERSONAL_ELIGIBILITY_PATTERN.search(query):
            route = terminal_route(
                _PERSONAL_ELIGIBILITY_MESSAGE,
                business_intent="generic_info",
                reason="personal_eligibility_determination",
            )
            return _materialize_route(payload, route, metadata=metadata, runtime=runtime)

        if _is_factual_plan_rationale(query):
            route = terminal_route(
                _FACTUAL_RATIONALE_CLARIFICATION,
                business_intent="specific_plan",
                reason="unbounded_factual_plan_rationale",
            )
            return _materialize_route(payload, route, metadata=metadata, runtime=runtime)

        deterministic_rule = _deterministic_general_rule(query)
        if selection_continuation:
            decision = None
            metadata["deterministic_rule"] = "selection_continuation"
        elif deterministic_rule is not None:
            decision = None
            metadata["deterministic_rule"] = deterministic_rule.name
        else:
            try:
                decision = await asyncio.to_thread(_classify, query, market_segment)
            except ClassifierError:
                route = terminal_route(
                    GENERIC_PROCESSING_ERROR_MESSAGE,
                    reason="classifier_error",
                    escalation={
                        "type": True,
                        "identification": "classifier_error",
                        "description": "The current message could not be classified",
                    },
                )
                return _materialize_route(payload, route, metadata=metadata, runtime=runtime)
            if (
                _PERSONALIZED_OUTCOME_PATTERN.search(query)
                and not decision.personalized_explanation
            ):
                decision = decision.model_copy(update={"personalized_explanation": True})
                metadata["personalized_explanation_fallback"] = True
        current_plan = PlanReference.from_mapping(current) if current else None
        routing_request = ShopperRoutingRequest(
            query=query,
            runtime=runtime,
            market_segment=market_segment,
            available_plans=tuple(PlanReference.from_mapping(plan) for plan in available),
            recommended_plans=tuple(PlanReference.from_mapping(plan) for plan in recommended),
            current_plan=current_plan,
            deterministic_general=deterministic_rule is not None,
            deterministic_selection_continuation=selection_continuation,
        )
        route = resolve_policy(routing_request, decision)
        route = await asyncio.to_thread(_compose_route_response, route, query)
        return _materialize_route(payload, route, metadata=metadata, runtime=runtime)
    except Exception:
        return _emergency_terminal_result(agent_pre_invoke_payload)
