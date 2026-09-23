import json
from dataclasses import FrozenInstanceError
from itertools import combinations

import pytest
from ibm_watsonx_orchestrate.run.context import AgentRun

from shared.classifier_contract import GuardrailDecision
from shared.guardrails import (
    enrollment_options_response,
    live_agent_response,
    provider_lookup_response,
    recommendation_decline_response,
)
from shared.response_composer import GENERATED_TEXT_MARKER, ResponseCompositionSpec
from shared.routing import (
    GENERAL_SEARCH_TOOL,
    PLAN_SEARCH_TOOL,
    SEARCH_TOOLS,
    SEARCH_TOOLS_WITHOUT_PLAN_DETAILS,
    STRUCTURED_PLAN_QUOTE_AVAILABLE_INSTRUCTION,
    STRUCTURED_PLAN_QUOTE_RECOVERY,
    STRUCTURED_PLAN_QUOTE_UNAVAILABLE_INSTRUCTION,
    STRUCTURED_PLAN_QUOTE_UNAVAILABLE_REASON,
    STRUCTURED_PLAN_QUOTE_USER_MESSAGE,
    PlanContextEntry,
    PlanReference,
    RouteDecision,
    SearchTurnControl,
    ShopperRoutingRequest,
    resolve_policy,
)

PLAN = {"plan_id": "H4161-009-000", "plan_name": "Anthem Prime (HMO-POS)"}

PRIORITY_SIGNALS = (
    ("policy_override", "G16"),
    ("medicaid_related", "G15"),
    ("location_request", "G01"),
    ("availability_request", "G02"),
    ("greeting_only", "G05"),
    ("live_agent", "G03"),
    ("instructional_bias", "G10"),
    ("off_topic", "G06"),
    ("personalized_explanation", "G14"),
    ("recommendation", "G08"),
    ("provider_lookup", "G07"),
    ("enrollment_action", "G09"),
)


def _runtime(
    *,
    plans=None,
    market_segment="IND",
    requested_effective_date="2027-02-01",
    plan_quote_context="",
) -> AgentRun:
    plans = [PLAN] if plans is None else plans
    return AgentRun(
        request_context={
            "application_available_plans": json.dumps(plans),
            "application_market_segment": market_segment,
            "user_brand": "ABCBS",
            "user_requested_eff_date": requested_effective_date,
            "user_state_code": "ZZ",
            "prospect_type": "prospect",
            "application_plan_quote_inputs": plan_quote_context,
        }
    )


def _request(
    *,
    plans=None,
    deterministic_general=False,
    deterministic_selection_continuation=False,
    query="Shopper request",
    market_segment="IND",
    requested_effective_date="2027-02-01",
    plan_quote_context="",
) -> ShopperRoutingRequest:
    plans = [PLAN] if plans is None else plans
    return ShopperRoutingRequest(
        query=query,
        runtime=_runtime(
            plans=plans,
            market_segment=market_segment,
            requested_effective_date=requested_effective_date,
            plan_quote_context=plan_quote_context,
        ),
        market_segment=market_segment,
        available_plans=tuple(PlanReference.from_mapping(plan) for plan in plans),
        recommended_plans=(),
        current_plan=None,
        deterministic_general=deterministic_general,
        deterministic_selection_continuation=deterministic_selection_continuation,
    )


def _decision(**overrides) -> GuardrailDecision:
    values: dict[str, object] = {
        "location_request": False,
        "availability_request": False,
        "greeting_only": False,
        "live_agent": False,
        "policy_override": False,
        "medicaid_related": False,
        "off_topic": False,
        "instructional_bias": False,
        "recommendation": False,
        "provider_lookup": False,
        "enrollment_action": False,
        "enroll_now_effective_date_question": False,
        "personalized_explanation": False,
    }
    values.update(overrides)
    return GuardrailDecision.model_validate(values)


def _conflicting_decision(first: str, second: str) -> GuardrailDecision:
    values: dict[str, object] = {first: True, second: True}
    return _decision(**values)


@pytest.mark.parametrize(
    ("higher", "lower", "expected_code"),
    [
        (PRIORITY_SIGNALS[first][0], PRIORITY_SIGNALS[second][0], PRIORITY_SIGNALS[first][1])
        for first, second in combinations(range(len(PRIORITY_SIGNALS)), 2)
    ],
)
def test_every_conflicting_signal_pair_resolves_to_documented_priority(
    higher,
    lower,
    expected_code,
):
    route = resolve_policy(_request(), _conflicting_decision(higher, lower))

    assert route.kind == "terminal"
    assert route.guardrail_code == expected_code
    assert route.tools == ()


def test_recommendation_and_enrollment_use_one_g08_composition_in_priority_order():
    route = resolve_policy(
        _request(),
        _decision(recommendation=True, enrollment_action=True),
    )

    assert route.guardrail_code == "G08"
    assert route.response_composition is not None
    assert route.response_composition.template_id == "G08_recommendation_decline_v1"
    assert route.message == route.response_composition.fallback_response
    assert route.audit_required is True
    assert route.escalation == {
        "type": True,
        "identification": "plan_recommendation_request",
        "description": "User requested a personalized plan recommendation (G08)",
    }


def test_nonterminal_requests_without_structured_quote_hide_plan_details():
    catalog_route = resolve_policy(_request(), _decision())
    empty_catalog_route = resolve_policy(_request(plans=[]), _decision())

    for route in (catalog_route, empty_catalog_route):
        assert route.kind == "search_turn"
        assert route.business_intent is None
        assert route.tools == SEARCH_TOOLS_WITHOUT_PLAN_DETAILS
        assert isinstance(route.control, SearchTurnControl)

    assert catalog_route.control.to_model_wire()["plan_catalog_available"] is True
    assert empty_catalog_route.control.to_model_wire()["plan_catalog_available"] is False
    control = catalog_route.control.to_model_wire()
    assert control["structured_plan_quote_available"] is False
    assert (
        control["structured_plan_quote_unavailable_reason"]
        == STRUCTURED_PLAN_QUOTE_UNAVAILABLE_REASON
    )
    assert control["structured_plan_quote_recovery"] == STRUCTURED_PLAN_QUOTE_RECOVERY
    assert control["structured_plan_quote_user_message"] == STRUCTURED_PLAN_QUOTE_USER_MESSAGE
    assert control["structured_plan_quote_instruction"] == (
        STRUCTURED_PLAN_QUOTE_UNAVAILABLE_INSTRUCTION
    )


def test_premium_amount_request_without_structured_quote_exposes_recovery_control():
    route = resolve_policy(
        _request(query="What is the monthly premium for Anthem Prime (HMO-POS)?"),
        _decision(),
    )

    assert route.kind == "search_turn"
    assert route.business_intent is None
    assert route.tools == SEARCH_TOOLS_WITHOUT_PLAN_DETAILS
    assert route.control.to_model_wire()["structured_plan_quote_user_message"] == (
        STRUCTURED_PLAN_QUOTE_USER_MESSAGE
    )


@pytest.mark.parametrize(
    "query",
    [
        "What is the medical deductible for Anthem Prime (HMO-POS)?",
        "What is Anthem Prime (HMO-POS)'s annual out-of-pocket maximum?",
        "How much is urgent care on Anthem Prime (HMO-POS)?",
        "What optional plan options are shown for Anthem Prime (HMO-POS)?",
    ],
)
def test_structured_plan_detail_request_without_quote_exposes_recovery_control(query):
    route = resolve_policy(_request(query=query), _decision())

    assert route.kind == "search_turn"
    assert route.business_intent is None
    assert route.tools == SEARCH_TOOLS_WITHOUT_PLAN_DETAILS
    assert route.control.to_model_wire()["structured_plan_quote_user_message"] == (
        STRUCTURED_PLAN_QUOTE_USER_MESSAGE
    )


@pytest.mark.parametrize(
    "query",
    [
        "Which plan has the lowest medical deductible?",
        "How much could I pay in a year, max, for these plans?",
    ],
)
def test_exhaustive_language_without_quote_is_left_to_the_agent(query):
    route = resolve_policy(
        _request(query=query),
        _decision(),
    )

    assert route.kind == "search_turn"
    assert route.business_intent is None
    assert route.tools == SEARCH_TOOLS_WITHOUT_PLAN_DETAILS
    assert isinstance(route.control, SearchTurnControl)
    assert route.control.structured_plan_quote_available is False


def test_six_named_plans_do_not_hide_structured_quote_recovery():
    plans = [
        {"plan_id": f"P{index}", "plan_name": f"Catalog Choice {index}"} for index in range(1, 7)
    ]
    names = ", ".join(plan["plan_name"] for plan in plans)

    route = resolve_policy(
        _request(
            plans=plans,
            query=f"Compare the medical deductible for {names}.",
        ),
        _decision(),
    )

    assert route.kind == "search_turn"
    assert route.tools == SEARCH_TOOLS_WITHOUT_PLAN_DETAILS
    assert route.control.to_model_wire()["structured_plan_quote_user_message"] == (
        STRUCTURED_PLAN_QUOTE_USER_MESSAGE
    )


def test_plan_independent_definition_with_incomplete_quote_remains_searchable():
    route = resolve_policy(
        _request(query="What is a deductible?", deterministic_general=True),
        None,
    )

    assert route.kind == "search_turn"
    assert route.tools == SEARCH_TOOLS_WITHOUT_PLAN_DETAILS


def test_complete_structured_quote_keeps_premium_request_nonterminal():
    route = resolve_policy(
        _request(
            query="What is the monthly premium for Anthem Prime (HMO-POS)?",
            plan_quote_context={
                "zip_code": "90001",
                "county_code": "037",
                "county_name": "LOS ANGELES",
                "applicants": [{"applicant_type": "PRIMARY"}],
            },
        ),
        _decision(),
    )

    assert route.kind == "search_turn"
    assert route.tools == SEARCH_TOOLS


def test_exact_effective_date_match_remains_a_classifier_false_negative_fallback():
    route = resolve_policy(
        _request(
            query="When would my coverage start if I enroll today?",
            market_segment="Medicare",
        ),
        _decision(
            enroll_now_effective_date_question=False,
        ),
    )

    assert route.kind == "terminal"
    assert route.reason == "context_requested_effective_date"
    assert route.message == (
        "Based on your requested effective date, if you enroll today, your coverage is expected "
        "to begin on 2027-02-01, subject to eligibility verification, application review, and "
        "any required approvals."
    )


def test_classifier_effective_date_paraphrase_uses_context_response():
    route = resolve_policy(
        _request(
            query="If I sign up now, when do my benefits kick in?",
            market_segment="Medicare",
        ),
        _decision(
            enroll_now_effective_date_question=True,
        ),
    )

    assert route.kind == "terminal"
    assert route.reason == "context_requested_effective_date"
    assert route.message and "2027-02-01" in route.message


def test_effective_date_signal_uses_safe_fallback_for_invalid_context_date():
    route = resolve_policy(
        _request(
            query="If I sign up now, when do my benefits kick in?",
            market_segment="Medicare",
            requested_effective_date="not-a-date",
        ),
        _decision(
            enroll_now_effective_date_question=True,
        ),
    )

    assert route.kind == "terminal"
    assert route.reason == "unestablished_personal_effective_date"
    assert route.message and "does not establish" in route.message


@pytest.mark.parametrize(
    ("guardrail_field", "guardrail_code"),
    [
        ("recommendation", "G08"),
        ("provider_lookup", "G07"),
        ("enrollment_action", "G09"),
    ],
)
def test_guardrails_take_precedence_over_effective_date_signal(guardrail_field, guardrail_code):
    route = resolve_policy(
        _request(
            query="If I enroll today, when will coverage start, and can you help with this?",
            market_segment="Medicare",
        ),
        _decision(
            enroll_now_effective_date_question=True,
            **{guardrail_field: True},
        ),
    )

    assert route.kind == "terminal"
    assert route.guardrail_code == guardrail_code


def test_deterministic_selection_continuation_routes_without_classifier_evidence():
    route = resolve_policy(
        _request(query="both", deterministic_selection_continuation=True),
        None,
    )

    assert route.kind == "search_turn"
    assert route.business_intent is None
    assert route.tools == SEARCH_TOOLS_WITHOUT_PLAN_DETAILS
    assert isinstance(route.control, SearchTurnControl)
    assert "selected_plans" not in route.control.to_wire()


def test_deterministic_selection_continuation_keeps_plan_search_available_without_catalog():
    route = resolve_policy(
        _request(
            query="both",
            plans=[],
            deterministic_selection_continuation=True,
        ),
        None,
    )

    assert route.kind == "search_turn"
    assert route.business_intent is None
    assert route.tools == SEARCH_TOOLS_WITHOUT_PLAN_DETAILS
    assert isinstance(route.control, SearchTurnControl)
    assert route.control.to_model_wire()["plan_catalog_available"] is False


def test_route_decision_enforces_terminal_and_audit_invariants():
    composition = ResponseCompositionSpec(
        template_id="test_v1",
        template=f"Fallback {GENERATED_TEXT_MARKER}",
        instructions="Write one sentence.",
        fallback_response="Fallback",
    )
    with pytest.raises(ValueError, match="requires a message"):
        RouteDecision(kind="terminal", business_intent="generic_info")
    with pytest.raises(ValueError, match="complete escalation"):
        RouteDecision(
            kind="terminal",
            business_intent="generic_info",
            message="terminal",
            audit_required=True,
        )
    with pytest.raises(ValueError, match="cannot expose tools"):
        RouteDecision(
            kind="terminal",
            business_intent="generic_info",
            message="terminal",
            tools=(PLAN_SEARCH_TOOL,),
        )
    with pytest.raises(ValueError, match="typed search control"):
        RouteDecision(
            kind="search_turn",
            business_intent="generic_info",
            tools=(GENERAL_SEARCH_TOOL,),
            control={"route": "search_turn"},  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="canned fallback message"):
        RouteDecision(
            kind="terminal",
            business_intent="generic_info",
            message="Different",
            response_composition=composition,
        )
    with pytest.raises(ValueError, match="controlled route cannot carry response composition"):
        RouteDecision(
            kind="search_turn",
            business_intent="generic_info",
            tools=(GENERAL_SEARCH_TOOL,),
            control=SearchTurnControl(),
            response_composition=composition,
        )


def test_policy_returns_typed_immutable_control_values():
    route = resolve_policy(_request(), _decision())

    assert isinstance(route.control, SearchTurnControl)
    with pytest.raises(FrozenInstanceError):
        route.control.current_user_query = "mutated"  # type: ignore[misc,union-attr]


@pytest.mark.parametrize("guardrail_field", ["live_agent", "enrollment_action"])
def test_guardrail_policy_resolution_does_not_mutate_runtime_context(guardrail_field):
    request = _request()
    before = dict(request.runtime.request_context)
    decision = _decision(**{guardrail_field: True})

    route = resolve_policy(request, decision)

    assert route.kind == "terminal"
    assert route.audit_required is True
    assert request.runtime.request_context == before


def test_structured_individual_quote_is_exposed_as_available():
    route = resolve_policy(
        _request(
            plan_quote_context={
                "zip_code": "90001",
                "county_code": "037",
                "county_name": "LOS ANGELES",
                "applicants": [{"applicant_type": "PRIMARY"}],
            }
        ),
        _decision(),
    )

    assert route.kind == "search_turn"
    assert route.tools == SEARCH_TOOLS
    assert route.guardrail_code is None
    assert route.control is not None
    assert route.control.to_model_wire()["structured_plan_quote_available"] is True
    assert route.control.to_model_wire()["structured_plan_quote_instruction"] == (
        STRUCTURED_PLAN_QUOTE_AVAILABLE_INSTRUCTION
    )
    assert "structured_plan_quote_unavailable_reason" not in route.control.to_model_wire()
    assert "structured_plan_quote_recovery" not in route.control.to_model_wire()
    assert "structured_plan_quote_user_message" not in route.control.to_model_wire()


def test_structured_medicare_quote_is_exposed_as_available():
    route = resolve_policy(
        _request(
            market_segment="Medicare",
            plan_quote_context={
                "zip_code": "53202",
                "county_code": "079",
                "county_name": "MILWAUKEE",
                "applicants": [{"applicant_type": "PRIMARY"}],
            },
        ),
        _decision(),
    )

    assert route.control is not None
    assert route.control.to_model_wire()["structured_plan_quote_available"] is True


@pytest.mark.parametrize(
    ("market_segment", "plan_quote_context"),
    [
        ("IND", "not-json"),
        ("IND", {}),
        ("IND", {"zip_code": "90001"}),
        (
            "Medicare",
            {
                "zip_code": "53202",
                "county_code": "079",
                "county_name": "MILWAUKEE",
                "applicants": [],
            },
        ),
    ],
)
def test_invalid_or_market_mismatched_quote_is_not_exposed_as_available(
    market_segment,
    plan_quote_context,
):
    route = resolve_policy(
        _request(
            market_segment=market_segment,
            plan_quote_context=plan_quote_context,
        ),
        _decision(),
    )

    assert route.control is not None
    assert route.control.to_wire()["structured_plan_quote_available"] is False
    assert route.tools == SEARCH_TOOLS_WITHOUT_PLAN_DETAILS


def test_structured_quote_requires_common_context_and_complete_location():
    missing_common = _request(
        plan_quote_context={
            "zip_code": "90001",
            "county_code": "037",
            "county_name": "LOS ANGELES",
            "applicants": [{"applicant_type": "PRIMARY"}],
        }
    )
    missing_common.runtime.request_context["user_brand"] = ""
    missing_location = _request(
        plan_quote_context={
            "zip_code": "90001",
            "county_code": "",
            "county_name": "LOS ANGELES",
            "applicants": [{"applicant_type": "PRIMARY"}],
        }
    )

    for request in (missing_common, missing_location):
        route = resolve_policy(request, _decision())
        assert route.control is not None
        assert route.control.to_wire()["structured_plan_quote_available"] is False


def test_member_guardrail_messages_use_member_service_language():
    runtime = _runtime()
    runtime.request_context["prospect_type"] = "member"

    assert "member services" in live_agent_response(runtime)
    recommendation_message = recommendation_decline_response(runtime)
    assert "compare documented plan details" in recommendation_message
    assert "member ID card" in recommendation_message
    enrollment_message = enrollment_options_response(runtime)
    assert "Health Plan Advisors" in enrollment_message
    assert "schedule an appointment" in enrollment_message


@pytest.mark.parametrize("market_segment", ["Medicare", "IND"])
def test_prospect_live_agent_message_uses_page_phone_number_only(market_segment):
    runtime = _runtime()
    runtime.request_context["application_market_segment"] = market_segment

    message = live_agent_response(runtime)

    assert "number shown at the top of this page" in message
    assert "Schedule a Callback" not in message
    assert "Request a call back" not in message


@pytest.mark.parametrize("market_segment", ["Medicare", "IND"])
def test_member_live_agent_message_uses_id_card_phone_number_only(market_segment):
    runtime = _runtime()
    runtime.request_context["application_market_segment"] = market_segment
    runtime.request_context["prospect_type"] = "member"

    message = live_agent_response(runtime)

    assert "number on the back of your member ID card" in message
    assert "Schedule a Callback" not in message
    assert "Request a call back" not in message


@pytest.mark.parametrize("market_segment", ["Medicare", "IND"])
def test_prospect_provider_lookup_message_uses_page_phone_number(market_segment):
    message = provider_lookup_response(_runtime(market_segment=market_segment))

    assert "provider directory" in message
    assert "number shown at the top of this page" in message
    assert "ID card" not in message
    assert "member portal" not in message


@pytest.mark.parametrize("market_segment", ["Medicare", "IND"])
def test_member_provider_lookup_message_uses_member_resources(market_segment):
    runtime = _runtime(market_segment=market_segment)
    runtime.request_context["prospect_type"] = "member"

    message = provider_lookup_response(runtime)

    assert "provider directory" in message
    assert "member portal" in message
    assert "member services number on your ID card" in message
    assert "number shown at the top of this page" not in message


def _search_control():
    return SearchTurnControl(
        current_user_query="Shopper request",
        plan_lookup=(
            PlanContextEntry(
                plan_id=PLAN["plan_id"],
                plan_name=PLAN["plan_name"],
                current=False,
                recommended=False,
            ),
        ),
    )


def test_search_control_serializes_only_current_turn_policy_fields():
    control = _search_control()

    assert PlanReference.from_mapping(PLAN).to_wire() == PLAN
    assert control.route == "search_turn"
    assert "history_required" not in control.to_wire()
    assert control.to_wire()["current_user_query"] == "Shopper request"
    assert control.to_wire()["current_turn_search_required"] is True
    assert "plan_lookup" in control.to_wire()
    assert "plan_lookup" not in control.to_model_wire()
    assert "current_user_query" not in control.to_model_wire()
    assert "current_message_plan_matches" not in control.to_wire()
    assert "selected_plans" not in control.to_wire()
    assert "required_plan_search" not in control.to_model_wire()
    assert "explicit_current_plan_match" not in control.to_wire()
    assert "canonical_current_plan" not in control.to_model_wire()


def test_general_control_keeps_current_query_internal():
    control = SearchTurnControl(current_user_query="When can I enroll?")

    assert control.to_wire()["current_user_query"] == "When can I enroll?"
    assert "current_user_query" not in control.to_model_wire()


def test_plan_control_does_not_expose_or_store_matcher_derived_plan_hints():
    plan_g = {"plan_id": "2983_CA", "plan_name": "Plan G"}

    route = resolve_policy(
        _request(plans=[PLAN, plan_g], query="Does Plan G cover what Plan G lists?"),
        _decision(),
    )

    assert isinstance(route.control, SearchTurnControl)
    assert "current_message_plan_matches" not in route.control.to_wire()
    assert "selected_plans" not in route.control.to_wire()
    assert "required_plan_search" not in route.control.to_model_wire()
    assert route.control.selection_only is False


def test_plan_control_normalizes_unicode_for_selection_only_detection():
    route = resolve_policy(
        _request(query="Anthem\u202fPrime (HMO\u2011POS)"),
        _decision(),
    )

    assert isinstance(route.control, SearchTurnControl)
    assert route.control.selection_only is True


def test_plan_control_marks_exact_named_selection_as_selection_only():
    plan_g = {"plan_id": "2983_CA", "plan_name": "Plan G"}
    route = resolve_policy(
        _request(
            plans=[PLAN, plan_g],
            query="Anthem Prime (HMO-POS) and Plan G",
        ),
        _decision(),
    )

    assert isinstance(route.control, SearchTurnControl)
    assert route.control.selection_only is True
    assert route.control.to_model_wire()["selection_only"] is True


def test_plan_control_accepts_leading_article_in_named_selection():
    route = resolve_policy(
        _request(query="The Anthem Prime (HMO-POS) plan."),
        _decision(),
    )

    assert isinstance(route.control, SearchTurnControl)
    assert route.control.selection_only is True


def test_plan_control_accepts_medicare_supplement_descriptor_as_selection_only():
    plan_n = {"plan_id": "2989_CA", "plan_name": "Plan N"}
    route = resolve_policy(
        _request(
            plans=[PLAN, plan_n],
            query="Medicare Supplement Plan - Plan N",
        ),
        _decision(),
    )

    assert isinstance(route.control, SearchTurnControl)
    assert route.control.selection_only is True
    assert [plan.to_wire() for plan in route.control.selected_plans] == [plan_n]


def test_plan_control_does_not_mark_a_current_fact_question_as_selection_only():
    route = resolve_policy(
        _request(
            query="What is the deductible for Anthem Prime (HMO-POS)?",
            plan_quote_context={
                "zip_code": "00000",
                "county_code": "001",
                "county_name": "EXAMPLE",
                "applicants": [{"applicant_type": "PRIMARY"}],
            },
        ),
        _decision(),
    )

    assert isinstance(route.control, SearchTurnControl)
    assert route.control.selection_only is False
    assert route.control.selected_plans == ()


def test_personalized_outcome_uses_g14_composed_terminal_response():
    current = PlanReference.from_mapping(PLAN)
    request = _request(query="Why was my specialist bill $487?")
    request = ShopperRoutingRequest(
        query=request.query,
        runtime=request.runtime,
        market_segment=request.market_segment,
        available_plans=request.available_plans,
        recommended_plans=request.recommended_plans,
        current_plan=current,
    )

    route = resolve_policy(request, _decision(personalized_explanation=True))

    assert route.kind == "terminal"
    assert route.guardrail_code == "G14"
    assert route.business_intent == "specific_plan"
    assert route.tools == ()
    assert route.response_composition is not None
    assert route.response_composition.template_id == "G14_personalized_explanation_v1"
    assert route.message == route.response_composition.fallback_response


def test_plan_control_does_not_claim_ambiguous_duplicate_name_match():
    duplicate = {"plan_id": "H4161-010-000", "plan_name": PLAN["plan_name"]}

    route = resolve_policy(
        _request(plans=[PLAN, duplicate], query="What does Anthem Prime (HMO-POS) cover?"),
        _decision(),
    )

    assert isinstance(route.control, SearchTurnControl)
    assert route.control.selection_only is False


def test_plan_control_ignores_empty_queries_and_catalog_names_without_words():
    empty_query = resolve_policy(_request(query=""), _decision())
    punctuation_name = resolve_policy(
        _request(plans=[{"plan_id": "punctuation", "plan_name": "()"}], query="coverage"),
        _decision(),
    )

    assert isinstance(empty_query.control, SearchTurnControl)
    assert empty_query.control.selection_only is False
    assert isinstance(punctuation_name.control, SearchTurnControl)
    assert punctuation_name.control.selection_only is False


def test_plan_control_ignores_negated_names_and_handles_empty_short_aliases():
    negated = resolve_policy(
        _request(query="Not Anthem Prime (HMO-POS)"),
        _decision(),
    )
    parenthetical_only = resolve_policy(
        _request(plans=[{"plan_id": "hmo", "plan_name": "(HMO)"}], query="HMO"),
        _decision(),
    )

    assert isinstance(negated.control, SearchTurnControl)
    assert negated.control.selection_only is False
    assert isinstance(parenthetical_only.control, SearchTurnControl)
    assert parenthetical_only.control.selection_only is True


def test_plan_control_keeps_longest_exact_name_at_the_same_position():
    base = {"plan_id": "one", "plan_name": "Wellpoint Lung Care"}
    variant = {"plan_id": "two", "plan_name": "Wellpoint Lung Care 2"}

    route = resolve_policy(
        _request(plans=[base, variant], query="Wellpoint Lung Care 2"),
        _decision(),
    )

    assert isinstance(route.control, SearchTurnControl)
    assert route.control.selection_only is True


def test_route_decision_rejects_every_invalid_control_relationship():
    with pytest.raises(ValueError, match="terminal route cannot carry search control"):
        RouteDecision(
            kind="terminal",
            business_intent="generic_info",
            message="terminal",
            control=SearchTurnControl(),
        )

    with pytest.raises(ValueError, match="controlled route cannot carry"):
        RouteDecision(
            kind="search_turn",
            business_intent="generic_info",
            message="terminal",
            control=SearchTurnControl(),
        )

    with pytest.raises(ValueError, match="requires typed search control"):
        RouteDecision(kind="search_turn", business_intent="generic_info")

    search_control = _search_control()
    object.__setattr__(search_control, "route", "plan_turn")
    with pytest.raises(ValueError, match="disagrees with policy kind"):
        RouteDecision(
            kind="search_turn",
            business_intent=None,
            control=search_control,
        )

    with pytest.raises(ValueError, match="must be a neutral search turn"):
        RouteDecision(
            kind="plan_turn",
            business_intent=None,
            control=search_control,
        )


def test_policy_requires_exactly_one_classification_source():
    with pytest.raises(ValueError, match="cannot both be active"):
        resolve_policy(
            _request(
                deterministic_general=True,
                deterministic_selection_continuation=True,
            ),
            None,
        )

    with pytest.raises(ValueError, match="cannot include classifier evidence"):
        resolve_policy(
            _request(deterministic_general=True),
            _decision(),
        )

    with pytest.raises(ValueError, match="classifier evidence is required"):
        resolve_policy(_request(), None)

    with pytest.raises(ValueError, match="cannot include classifier evidence"):
        resolve_policy(
            _request(deterministic_selection_continuation=True),
            _decision(
                off_topic=True,
            ),
        )


def test_route_decision_rejects_terminal_composition_without_fallback_message():
    from shared.response_composer import GENERATED_TEXT_MARKER, ResponseCompositionSpec

    spec = ResponseCompositionSpec(
        template_id="test_template",
        template=f"Hello {GENERATED_TEXT_MARKER}",
        instructions="Generate greeting",
        fallback_response="Default greeting",
    )

    with pytest.raises(ValueError, match="terminal composition requires its canned fallback"):
        RouteDecision(
            kind="terminal",
            business_intent="generic_info",
            message="Different message",
            response_composition=spec,
        )


def test_route_decision_rejects_controlled_route_with_response_composition():
    from shared.response_composer import GENERATED_TEXT_MARKER, ResponseCompositionSpec

    spec = ResponseCompositionSpec(
        template_id="test_template",
        template=f"Hello {GENERATED_TEXT_MARKER}",
        instructions="Generate greeting",
        fallback_response="Default greeting",
    )

    with pytest.raises(ValueError, match="controlled route cannot carry response composition"):
        RouteDecision(
            kind="search_turn",
            business_intent="generic_info",
            control=SearchTurnControl(),
            response_composition=spec,
        )
