"""Canonical terminal responses for the shopper pre-invoke plugin."""

from dataclasses import dataclass
from typing import Any, Literal

from ibm_watsonx_orchestrate.run.context import AgentRun

from shared.context import authoritative_plans, request_context
from shared.response_composer import GENERATED_TEXT_MARKER, ResponseCompositionSpec

POLICY_OVERRIDE_MESSAGE = (
    "I can help with health-plan questions using the available information, "
    "but I can’t skip the checks needed to answer them. Please ask your health-plan question."
)

MEDICAID_UNSUPPORTED_MESSAGE = (
    "I can’t answer questions about Medicaid. I can help with other health-plan questions."
)

INDIVIDUAL_AVAILABLE_PLANS_MESSAGE = (
    "Your personalized plan options are shown on this page based on the information you've "
    "provided. You can view and compare them here for the most current details."
)
MEDICARE_AVAILABLE_PLANS_MESSAGE = (
    f"{INDIVIDUAL_AVAILABLE_PLANS_MESSAGE} For some plans, including certain Special Needs Plans "
    "(SNPs), additional eligibility confirmation may be required."
)
GENERIC_MEDSUPP_QUOTE_DISCLAIMER = (
    "Medicare Supplement plan availability and premiums may be based on generic rates because "
    "your birth date or gender is missing; your actual options and rates may differ."
)
PLAN_CATALOG_UNAVAILABLE_MESSAGE = (
    "Plan-specific information is not available yet. Please continue the application until plan "
    "options appear. I can answer general health-insurance questions in the meantime."
)
PLAN_PRODUCT_UNAVAILABLE_MESSAGE = (
    "I can provide plan-specific information only for plans available in this application. "
    "One or more plans in your request is not available here."
)
USER_BRAND_DISPLAY_NAMES = {
    "ABC": "Anthem Blue Cross",
    "ABCBS": "Anthem Blue Cross & Blue Shield",
    "HBKS": "Healthy Blue of Kansas",
    "HBLA": "Healthy Blue of Louisiana",
    "HBMO": "Healthy Blue of Missouri",
    "SIMPLY": "Simply Healthcare (Fla.)",
    "WLP": "Wellpoint",
}

_G08_COMPOSITION_INSTRUCTIONS = "Write exactly one concise first-person sentence that starts with 'I understand' and acknowledges the shopper's goal of choosing a suitable health plan. When the request mentions personal medical conditions, care needs, medications, or other health circumstances, acknowledge them collectively using exactly the phrase 'your health needs'; never repeat, name, or paraphrase the specific details. Do not state the recommendation boundary or choose, recommend, rank, or advise on a plan. Do not provide plan facts or contact instructions, include PII, PHI, or sensitive medical or financial details, or repeat any canned template text."
_G09_COMPOSITION_INSTRUCTIONS = "Write one concise first-person sentence that directly acknowledges the current request and says the assistant cannot perform the requested enrollment, switch, cancellation, reinstatement, dependent, or coverage-change action. Mention only the action boundary that applies. You may refer to non-sensitive plan or action details from the request, but do not provide plan advice or facts, contact instructions, PII, PHI, or sensitive medical or financial details. Do not repeat any canned template text."
_G06_COMPOSITION_INSTRUCTIONS = "Write exactly one concise first-person sentence that starts exactly with 'I understand you're asking about' and neutrally acknowledges only the general subject of the current request. Do not answer the request, provide facts or advice, mention contact instructions, include PII, PHI, or sensitive medical or financial details, or repeat any canned template text."
_G10_COMPOSITION_INSTRUCTIONS = "Write exactly one concise first-person sentence that starts exactly with 'I understand you're asking about' and neutrally acknowledges only the general subject of the current request. Do not repeat, paraphrase, endorse, reject, or answer any comparison, stereotype, or generalization about a group of people. Do not provide facts or advice, mention contact instructions, include PII, PHI, or sensitive medical or financial details, or repeat any canned template text."
_G14_COMPOSITION_INSTRUCTIONS = "Write exactly one concise, empathetic first-person sentence that starts exactly with 'I understand you're concerned about' and acknowledges only the general type of actual outcome in the current request, such as a denied claim, unexpected bill, or eligibility or coverage outcome. Do not explain, infer, speculate about, or list possible causes; provide facts or advice; ask a question; mention contact instructions; repeat a medical condition or financial amount; include PII, PHI, or sensitive medical or financial details; or repeat any canned template text."


def validate_supported_state(context: AgentRun | None) -> str:
    """
    G01: Validate that a supported US state code is present in session context.

    Args:
        context (AgentRun): Agent runtime context — injected automatically, do not pass

    Returns:
        str: 'true' if state is valid, 'false' if missing or unsupported
    """
    user_state_code = request_context(context).get("user_state_code", "")
    valid_states = {
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    }
    return "true" if (user_state_code or "").strip().upper() in valid_states else "false"


def location_unavailable_response(context: AgentRun | None) -> str:
    """
    G01: Return a location-not-supported message.
    Args:
        context (AgentRun): Agent runtime context — injected automatically, do not pass

    Returns:
        str: User-facing message explaining plan unavailability in this location
    """
    user_state_code = request_context(context).get("user_state_code", "")
    loc = user_state_code.strip() or "your area"
    return (
        f"I'm sorry, but Elevance Health plans do not appear to be available in {loc} "
        "at this time. Please visit HealthCare.gov or your state marketplace to explore "
        "other health insurance options in your area."
    )


def available_plans_response(context: AgentRun | None) -> str:
    """
    G02: Summarize the authoritative plan catalog and guide the shopper to the page.

    Args:
        context (AgentRun): Agent runtime context — injected automatically, do not pass

    Returns:
        str: Market-specific plan-count summary, or a gentle no-count fallback
    """
    ctx = request_context(context)
    market_segment = str(ctx.get("application_market_segment", "")).strip().casefold()
    is_medicare = market_segment == "medicare"
    available, _ = authoritative_plans(context)
    plan_count = len(available)

    if not plan_count:
        return (
            MEDICARE_AVAILABLE_PLANS_MESSAGE if is_medicare else INDIVIDUAL_AVAILABLE_PLANS_MESSAGE
        )

    quote = ctx.get("application_plan_quote_inputs")
    applicants = quote.get("applicants") if isinstance(quote, dict) else None
    applicant = applicants[0] if isinstance(applicants, list) and len(applicants) == 1 else None
    state_code = str(ctx.get("user_state_code", "")).strip().upper()
    state_suffix = f"_{state_code}"
    has_medsupp = bool(state_code) and any(
        (plan_id := str(plan["plan_id"]).upper()).endswith(state_suffix)
        and plan_id[: -len(state_suffix)].isdigit()
        for plan in available
    )
    generic_medsupp_quote = (
        is_medicare
        and has_medsupp
        and isinstance(applicant, dict)
        and (not applicant.get("date_of_birth") or not applicant.get("gender"))
    )

    if plan_count == 1:
        summary = "Based on the information you've provided, 1 plan is available in your ZIP code."
        comparison = "view and compare this plan"
    else:
        summary = (
            f"Based on the information you've provided, {plan_count} plans are "
            "available in your ZIP code."
        )
        comparison = "view and compare these plans"

    if generic_medsupp_quote:
        summary = f"{summary} You can {comparison} using the current details shown on this page."
    else:
        summary = (
            f"{summary} You can {comparison} on this page for the most current, personalized "
            "details."
        )

    if is_medicare:
        summary = (
            f"{summary} For some plans, including certain Special Needs Plans (SNPs), additional "
            "eligibility confirmation may be required."
        )

    if generic_medsupp_quote:
        return f"{summary} {GENERIC_MEDSUPP_QUOTE_DISCLAIMER}"
    return summary


def plan_product_unavailable_response(context: AgentRun | None) -> str:
    """G12: Limit plan-specific help to the trusted application brand when known."""
    brand_code = str(request_context(context).get("user_brand", "")).strip().upper()
    brand_name = USER_BRAND_DISPLAY_NAMES.get(brand_code)
    if not brand_name:
        return PLAN_PRODUCT_UNAVAILABLE_MESSAGE
    return (
        f"I can provide plan-specific information only for {brand_name} plans available in "
        "this application. One or more plans in your request is not available here."
    )


def live_agent_response(context: AgentRun | None) -> str:
    """
    G03: Render a live agent escalation response without mutating runtime context.

    Args:
        context (AgentRun): Agent runtime context — injected automatically, do not pass

    Returns:
        str: User-facing live-agent contact message
    """
    ctx = request_context(context)
    prospect_type = ctx.get("prospect_type", "")
    if prospect_type == "member":
        message = (
            "Of course — I'll connect you with a member services representative "
            "who can help you directly. Please call the number on the back of your "
            "member ID card."
        )
    else:
        message = (
            "Absolutely — a licensed agent would be happy to help you. "
            "Please call us at the number shown at the top of this page."
        )
    return message


def greeting_response() -> str:
    """
    G05: Return a warm welcome message redirecting to insurance topics.

    Returns:
        str: Friendly welcome with invitation to ask about health plans
    """
    return (
        "Hello! Welcome to the Elevance Health Shopper Portal. "
        "I'm here to help you understand your health insurance options. "
        "You can ask me about plan coverage, costs, deductibles, copays, network details, "
        "or enrollment timing. What would you like to know?"
    )


def off_topic_response() -> str:
    """
    G06: Return a scope-redirection message for off-topic queries.

    Returns:
        str: Message explaining the chatbot's scope
    """
    return (
        "I'm specifically designed to help with health insurance questions — "
        "things like plan coverage, costs, benefits, deductibles, and enrollment. "
        "I'm not able to help with that particular topic, but I'm happy to answer "
        "any questions you have about your health plan options!"
    )


def provider_lookup_response(context: AgentRun | None) -> str:
    """
    G07: Return a limitation message for provider/pharmacy lookup requests.

    Returns:
        str: User-facing provider-directory limitation message
    """
    message = (
        "Finding the right in-network provider is important, and I want to make sure "
        "you get accurate, up-to-date results. Because provider and pharmacy directories "
        "are updated in real time, I'm not able to search them here — but you can check "
        "current network participation through the provider directory"
    )
    if request_context(context).get("prospect_type", "") == "member":
        return (
            f"{message} on your plan's member portal, or by calling the member services "
            "number on your ID card."
        )
    return f"{message}, or by calling the number shown at the top of this page."


def recommendation_decline_response(context: AgentRun | None) -> str:
    """G08: Return the code-owned recommendation boundary and supported help."""
    ctx = request_context(context)
    prospect_type = ctx.get("prospect_type", "")
    supported_help = (
        "I can't recommend which plan you should choose, but I can help you compare documented "
        "plan details such as benefits, costs, prescription coverage, and network rules. "
    )
    if prospect_type == "member":
        return (
            f"{supported_help}If you'd like personalized assistance, please call the number on "
            "the back of your member ID card or use the 'Request a Callback' option and a "
            "member services representative will reach out."
        )
    return (
        f"{supported_help}If you'd like personalized assistance, you can contact one of our "
        "Agents using the phone number displayed at the top of the page or Request a callback "
        "at a time that's convenient for you."
    )


def enrollment_options_response(context: AgentRun | None) -> str:
    """G09: Return the code-owned enrollment contact options."""
    ctx = request_context(context)
    application_market_segment = ctx.get("application_market_segment", "")
    prospect_type = ctx.get("prospect_type", "")
    if (application_market_segment or "").lower() == "medicare":
        return (
            "I'm happy to continue answering general questions. If you'd like personalized "
            "assistance, you can:\n\n"
            "  - Call our Licensed Agents at the number shown on top of the page.\n"
            "  - Chat with a Live Agent\n"
            "  - Request a callback by sharing your contact information"
        )
    if prospect_type == "member":
        return (
            "I'm happy to continue answering general questions. If you'd like personalized "
            "assistance, you can contact one of our Health Plan Advisors using the phone "
            "number displayed at the top of the page or schedule an appointment at a time "
            "that's convenient for you."
        )
    return (
        "I’m happy to continue answering general questions. If you'd like personalized "
        "assistance, you can contact one of our Agents using the phone number displayed "
        "at the top of the page or schedule a callback at a time that's convenient for you."
    )


def personalized_explanation_response(context: AgentRun | None) -> str:
    """G14: Explain the actual-outcome boundary and supported assistance."""

    supported_help = (
        "I can't determine why your specific personal outcome occurred. I can help explain "
        "documented plan coverage, costs, benefits, or general health-insurance information. "
    )
    if request_context(context).get("prospect_type", "") == "member":
        return (
            f"{supported_help}For the exact reason, please review any notice or Explanation of "
            "Benefits you received or contact Member Services using the number on your member ID "
            "card."
        )
    return (
        f"{supported_help}For the exact reason, please review any notice you received or contact a "
        "licensed agent using the number shown at the top of this page."
    )


def _composition_spec(
    *,
    template_id: str,
    canned_response: str,
    instructions: str,
    required_generated_prefix: str | None = None,
) -> ResponseCompositionSpec:
    return ResponseCompositionSpec(
        template_id=template_id,
        template=f"{GENERATED_TEXT_MARKER} {canned_response}",
        instructions=instructions,
        fallback_response=canned_response,
        required_generated_prefix=required_generated_prefix,
    )


def recommendation_response_composition(context: AgentRun | None) -> ResponseCompositionSpec:
    """G08: Describe the optional customized sentence before the canned response."""

    return _composition_spec(
        template_id="G08_recommendation_decline_v1",
        canned_response=recommendation_decline_response(context),
        instructions=_G08_COMPOSITION_INSTRUCTIONS,
        required_generated_prefix="I understand",
    )


def off_topic_response_composition() -> ResponseCompositionSpec:
    """G06: Describe the customized acknowledgement before the canned response."""

    return _composition_spec(
        template_id="G06_off_topic_v1",
        canned_response=off_topic_response(),
        instructions=_G06_COMPOSITION_INSTRUCTIONS,
        required_generated_prefix="I understand you're asking about",
    )


def instructional_bias_response_composition() -> ResponseCompositionSpec:
    """G10: Describe the neutral acknowledgement before the canned response."""

    return _composition_spec(
        template_id="G10_instructional_bias_v1",
        canned_response=off_topic_response(),
        instructions=_G10_COMPOSITION_INSTRUCTIONS,
        required_generated_prefix="I understand you're asking about",
    )


def enrollment_response_composition(context: AgentRun | None) -> ResponseCompositionSpec:
    """G09: Describe the optional customized sentence before the canned response."""

    return _composition_spec(
        template_id="G09_enrollment_options_v1",
        canned_response=enrollment_options_response(context),
        instructions=_G09_COMPOSITION_INSTRUCTIONS,
    )


def personalized_explanation_response_composition(
    context: AgentRun | None,
) -> ResponseCompositionSpec:
    """G14: Describe the safe acknowledgement before the actual-outcome response."""

    return _composition_spec(
        template_id="G14_personalized_explanation_v1",
        canned_response=personalized_explanation_response(context),
        instructions=_G14_COMPOSITION_INSTRUCTIONS,
        required_generated_prefix="I understand you're concerned about",
    )


@dataclass(frozen=True)
class GuardrailResolution:
    """One deterministic terminal response selected from a classifier decision."""

    code: str
    message: str
    business_intent: Literal["specific_plan", "broad_plans", "generic_info"] = "generic_info"
    audit_payload: dict[str, Any] | None = None
    response_composition: ResponseCompositionSpec | None = None


def resolve_guardrail(
    decision: Any,
    context: AgentRun | None,
) -> GuardrailResolution | None:
    """Resolve classifier-backed guardrails in the established priority order."""
    if decision.policy_override:
        return GuardrailResolution("G16", POLICY_OVERRIDE_MESSAGE)
    if decision.medicaid_related:
        return GuardrailResolution("G15", MEDICAID_UNSUPPORTED_MESSAGE)
    if decision.location_request and validate_supported_state(context) == "false":
        return GuardrailResolution("G01", location_unavailable_response(context))
    if decision.availability_request:
        return GuardrailResolution("G02", available_plans_response(context))
    if decision.greeting_only:
        return GuardrailResolution("G05", greeting_response())
    if decision.live_agent:
        return GuardrailResolution(
            "G03",
            live_agent_response(context),
            audit_payload={
                "type": True,
                "identification": "live_agent_request",
                "description": "User explicitly requested a live agent (G03)",
            },
        )
    if decision.instructional_bias:
        composition = instructional_bias_response_composition()
        return GuardrailResolution(
            "G10",
            composition.fallback_response,
            response_composition=composition,
        )
    if decision.off_topic:
        composition = off_topic_response_composition()
        return GuardrailResolution(
            "G06",
            composition.fallback_response,
            response_composition=composition,
        )
    if decision.personalized_explanation:
        composition = personalized_explanation_response_composition(context)
        return GuardrailResolution(
            "G14",
            composition.fallback_response,
            business_intent="specific_plan",
            response_composition=composition,
        )
    if decision.recommendation:
        composition = recommendation_response_composition(context)
        return GuardrailResolution(
            "G08",
            composition.fallback_response,
            audit_payload={
                "type": True,
                "identification": "plan_recommendation_request",
                "description": "User requested a personalized plan recommendation (G08)",
            },
            response_composition=composition,
        )
    if decision.provider_lookup:
        return GuardrailResolution("G07", provider_lookup_response(context))
    if decision.enrollment_action:
        composition = enrollment_response_composition(context)
        return GuardrailResolution(
            "G09",
            composition.fallback_response,
            audit_payload={
                "type": True,
                "identification": "enrollment_action_request",
                "description": (
                    "User requested enrollment or account action — routed to contact options (G09)"
                ),
            },
            response_composition=composition,
        )
    return None
