import asyncio
import json
import logging
from unittest.mock import patch

import pytest
from ibm_watsonx_orchestrate.run.context import AgentRun

from shared.context import SEARCH_CONTROL_CONTEXT_KEY
from tools import plan_details
from tools.plan_details import DetailType, get_plan_details

_ASYNC_GET_PLAN_DETAILS = get_plan_details.fn.__wrapped__
SESSION_ID = "00000000-0000-4000-8000-000000000001"


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers if headers is not None else {"X-Request-ID": "rag-request-id"}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeAsyncClient:
    def __init__(self, response, captured):
        self.response = response
        self.captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, url, **kwargs):
        self.captured.update({"url": url, **kwargs})
        return self.response


def _context(plans, *, market="MOLS", quote=None, state="WI"):
    quote = quote or {
        "zip_code": "53910",
        "county_code": "55001",
        "county_name": "ADAMS",
        "applicants": [{"applicant_type": "PRIMARY"}],
    }
    control = {
        "route": "search_turn",
        "plan_lookup": [{**plan, "current": False, "recommended": False} for plan in plans],
    }
    return AgentRun(
        request_context={
            "application_available_plans": json.dumps(plans),
            "application_market_segment": "IND" if market == "IOLS" else "Medicare",
            "application_plan_quote_inputs": quote,
            "user_brand": "ABCBS",
            "user_state_code": state,
            "user_requested_eff_date": "2026-11-01",
            "session_id": SESSION_ID,
            "request_id": "plan-details-request",
            SEARCH_CONTROL_CONTEXT_KEY: json.dumps(control),
        }
    )


def _source_response(plans, *, market="MOLS"):
    return {
        "market": market,
        "plan_count": len(plans),
        "plans": plans,
    }


def _call(
    context,
    response,
    *,
    plan_ids,
    detail_types,
    all_available_plans=False,
):
    captured = {}
    fake_client = FakeAsyncClient(response, captured)
    connection = {
        "RAG_API_BASE_URL": "https://rag.example.test",
        "RAG_API_KEY": "secret",
        "RAG_PLANS_ENDPOINT": "/plans/quote",
    }
    with (
        patch.object(plan_details.connections, "key_value", return_value=connection),
        patch.object(plan_details.httpx, "AsyncClient", return_value=fake_client),
    ):
        result = asyncio.run(
            _ASYNC_GET_PLAN_DETAILS(
                plan_ids=plan_ids,
                detail_types=detail_types,
                context=context,
                all_available_plans=all_available_plans,
            )
        )
    return result, captured


def test_tool_contract_has_closed_detail_enum_and_response_fields():
    assert list(DetailType) == [
        DetailType.PREMIUM,
        DetailType.MEDICAL_DEDUCTIBLE,
        DetailType.PHARMACY_DEDUCTIBLE,
        DetailType.OUT_OF_POCKET_MAXIMUM,
        DetailType.PRIMARY_CARE,
        DetailType.SPECIALIST,
        DetailType.URGENT_CARE,
        DetailType.EMERGENCY_ROOM,
        DetailType.PLAN_OPTIONS,
        DetailType.DRUG_TIERS,
        DetailType.ESSENTIAL_EXTRAS,
    ]
    assert "definitions" in plan_details.PlanDetailsResponse.model_fields
    assert "required_response_addendum" in plan_details.PlanDetailsResponse.model_fields
    assert "error_code" in plan_details.PlanDetailsResponse.model_fields
    assert "http_status" in plan_details.PlanDetailsResponse.model_fields
    assert "retryable" in plan_details.PlanDetailsResponse.model_fields


@pytest.mark.parametrize("available", [True, False])
def test_essential_extras_uses_only_named_program_codes_and_existing_fallback(available):
    catalog = {"plan_id": "H4036-026-000", "plan_name": "Anthem Medicare Advantage (PPO)"}
    program = [
        {"code": "Essential_Extras", "value": "Covered", "covered": False},
        {
            "code": "Essential_Extras_Options",
            "value": "Assistive Devices-$500 annual allowance; Transportation-60 one-way trips",
            "covered": False,
        },
        {"code": "Essential_Extras_Selections", "value": "Pick 1", "covered": False},
    ]
    unrelated = [
        {"code": "Transportation", "value": "12 routine trips"},
        {"code": "Everyday_Options_Allowance", "value": "$100"},
    ]
    source = {
        **catalog,
        "contract_code": catalog["plan_id"],
        "coverage_type": "MAPD",
        "benefits": [
            {**row, "name": row["code"].replace("_", " ")}
            for row in (program if available else []) + unrelated
        ],
        "options": [{"name": "Paid package", "rate": 25}],
    }
    result, _ = _call(
        _context([catalog]),
        FakeResponse(_source_response([source])),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.ESSENTIAL_EXTRAS],
    )
    if available:
        assert result.coverage_complete is True
        assert result.fallback_detail_types == []
        assert [row.code for row in result.plans[0].details[0].benefits] == [
            row["code"] for row in program
        ]
        assert [row.value for row in result.plans[0].details[0].benefits] == [
            row["value"] for row in program
        ]
    else:
        assert result.coverage_complete is False
        assert result.plans[0].details == []
        assert result.fallback_detail_types == [DetailType.ESSENTIAL_EXTRAS]
        assert "Call search_plans before finalizing" in result.response_instructions
        assert catalog["plan_id"] in result.response_instructions


def test_premium_contract_stays_market_neutral():
    schema = plan_details.PlanPremium.model_json_schema()
    definition = plan_details.DETAIL_TYPE_DEFINITIONS[DetailType.PREMIUM]
    agent_facing_contract = json.dumps(schema) + definition

    assert "IOLS" not in agent_facing_contract
    assert "MOLS" not in agent_facing_contract


@pytest.mark.parametrize(
    ("question", "detail_type", "full_catalog", "guided"),
    [
        ("What is the premium?", DetailType.PREMIUM, False, False),
        ("Does premium change Essential Extras choices?", DetailType.PREMIUM, False, True),
        ("What extra benefits are available?", DetailType.PREMIUM, False, False),
        ("Compare Essential Extras across all plans.", DetailType.ESSENTIAL_EXTRAS, True, True),
    ],
)
def test_essential_extras_guidance_scope(question, detail_type, full_catalog, guided):
    catalog = {"plan_id": "H4036-026-000", "plan_name": "Anthem Medicare Advantage (PPO)"}
    context = _context([catalog])
    control = json.loads(context.request_context[SEARCH_CONTROL_CONTEXT_KEY])
    control["current_user_query"] = question
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps(control)
    source = {
        **catalog,
        "contract_code": catalog["plan_id"],
        "coverage_type": "MAPD",
        "premium": {"total": 10},
        "benefits": [{"code": "Essential_Extras", "name": "Essential Extras", "value": "Covered"}],
    }
    result, _ = _call(
        context,
        FakeResponse(_source_response([source])),
        plan_ids=[] if full_catalog else [catalog["plan_id"]],
        detail_types=[detail_type],
        all_available_plans=full_catalog,
    )
    assert result.outcome == "complete"
    assert bool(result.response_instructions) is guided
    if full_catalog:
        assert "do not search documents" in result.response_instructions
        assert "call search_plans" not in result.response_instructions
    elif guided:
        assert "Call get_plan_details for essential_extras" in result.response_instructions
        assert "call search_plans" in result.response_instructions
        assert result.plans[0].details[0].premium.total == 10


def test_premium_definition_depends_on_validated_market():
    individual = plan_details._definitions([DetailType.PREMIUM], plan_details.PlansMarket.IOLS)
    medicare = plan_details._definitions([DetailType.PREMIUM], plan_details.PlansMarket.MOLS)

    assert individual.detail_types[0].definition != medicare.detail_types[0].definition


def test_tool_contract_has_all_catalog_option_and_no_plan_count_cap():
    schema = get_plan_details.__tool_spec__.input_schema.model_dump(
        by_alias=True,
        exclude_none=True,
    )

    plan_ids = schema["properties"]["plan_ids"]
    all_available_plans = schema["properties"]["all_available_plans"]
    assert "maxItems" not in plan_ids
    assert "minItems" not in plan_ids
    assert all_available_plans["type"] == "boolean"
    assert all_available_plans["default"] is False
    assert "all_available_plans" not in schema["required"]


def test_explicit_plan_selection_has_no_tool_specific_count_limit():
    catalog = [
        {
            "plan_id": f"H1000-{index:03d}-000",
            "plan_name": f"Plan {index}",
        }
        for index in range(1, 22)
    ]
    source_plans = [
        {
            "plan_id": f"source-{index}",
            "contract_code": plan["plan_id"],
            "coverage_type": "MAPD",
            "plan_name": plan["plan_name"],
            "premium": {"total": float(index)},
        }
        for index, plan in enumerate(catalog, start=1)
    ]
    requested_ids = [plan["plan_id"] for plan in reversed(catalog)]

    result, _ = _call(
        _context(catalog),
        FakeResponse(_source_response(source_plans)),
        plan_ids=requested_ids,
        detail_types=[DetailType.PREMIUM],
    )

    assert result.outcome == "complete"
    assert [plan.plan_id for plan in result.plans] == requested_ids
    assert len(result.plans) == 21


def test_all_available_plans_returns_requested_details_for_full_catalog():
    catalog = [
        {
            "plan_id": f"H2000-{index:03d}-000",
            "plan_name": f"Full Comparison Plan {index}",
        }
        for index in range(1, 22)
    ]
    source_plans = [
        {
            "plan_id": f"source-{index}",
            "contract_code": plan["plan_id"],
            "coverage_type": "MAPD",
            "plan_name": plan["plan_name"],
            "premium": {"total": float(index)},
            "cost_coverages": [
                {
                    "code": "ANNUAL_DEDUCTIBLE",
                    "description": "Annual Deductible",
                    "value": str(index * 100),
                    "value_type": "DOLLAR",
                }
            ],
        }
        for index, plan in enumerate(catalog, start=1)
    ]

    result, _ = _call(
        _context(catalog),
        FakeResponse(_source_response(source_plans)),
        plan_ids=[catalog[0]["plan_id"]],
        detail_types=[DetailType.MEDICAL_DEDUCTIBLE],
        all_available_plans=True,
    )

    assert result.outcome == "complete"
    assert [plan.plan_id for plan in result.plans] == [plan["plan_id"] for plan in catalog]
    assert all(
        [detail.detail_type for detail in plan.details] == [DetailType.MEDICAL_DEDUCTIBLE]
        for plan in result.plans
    )


def test_mols_contract_code_maps_to_canonical_catalog_id_and_filters_details(caplog):
    caplog.set_level(logging.INFO, logger=plan_details.__name__)
    catalog = {
        "plan_id": "H4036-008-000",
        "plan_name": "Example Medicare Advantage PPO",
    }
    source_plan = {
        "plan_id": "5872",
        "contract_code": "H4036-008-000",
        "coverage_type": "MAPD",
        "plan_name": catalog["plan_name"],
        "premium": {"total": 42.5},
        "cost_coverages": [
            {
                "code": "ANNUAL_DEDUCTIBLE",
                "description": "Annual Deductible",
                "value": "0",
                "value_type": "DOLLAR",
            },
            {
                "code": "OOP_MAX",
                "description": "Annual Out-of-Pocket Maximum",
                "value": "4900",
                "value_type": "DOLLAR",
            },
        ],
        "benefits": [
            {
                "code": "PCP_Copay_IN",
                "name": "In Network Primary Care Physician (PCP)",
                "value": "$0.00 copay",
                "benefit_type": "SUMMARY",
            },
            {
                "code": "Urgently_Needed_Care",
                "name": "Urgently Needed Care",
                "value": "$35.00 copay",
                "benefit_type": "DETAIL",
            },
            {"code": "UNRELATED", "name": "Unrelated", "value": "$999"},
        ],
        "options": [{"name": "Option A", "display_name": "Option A", "rate": 12.25}],
        "tiers": [
            {
                "name": "Tier 1",
                "level": "1",
                "retail_frequency": "30 days",
                "retail_value": "$5",
            }
        ],
    }
    requested = [
        DetailType.PREMIUM,
        DetailType.MEDICAL_DEDUCTIBLE,
        DetailType.OUT_OF_POCKET_MAXIMUM,
        DetailType.PRIMARY_CARE,
        DetailType.URGENT_CARE,
        DetailType.PLAN_OPTIONS,
        DetailType.DRUG_TIERS,
    ]

    result, captured = _call(
        _context([catalog]),
        FakeResponse(_source_response([source_plan])),
        plan_ids=[catalog["plan_id"]],
        detail_types=requested,
    )

    assert result.outcome == "complete"
    assert result.coverage_complete is True
    assert [item.detail_type for item in result.definitions.detail_types] == requested
    assert result.plans[0].plan_id == "H4036-008-000"
    assert result.plans[0].plan_name == catalog["plan_name"]
    assert result.plans[0].details[0].premium.total == 42.5
    assert "covered" not in plan_details.PlanBenefit.model_fields
    assert "5872" not in result.model_dump_json()
    assert "UNRELATED" not in result.model_dump_json()
    assert captured["url"] == "https://rag.example.test/plans/quote"
    assert "X-Request-ID" not in captured["headers"]
    assert captured["json"] == {
        **_context([catalog]).request_context["application_plan_quote_inputs"],
        "market_segment": "Medicare",
        "brand": "ABCBS",
        "state_code": "WI",
        "requested_eff_date": "2026-11-01",
    }
    record = next(
        record
        for record in caplog.records
        if record.message == "structured_plans_response_received"
    )
    assert record.request_id == "plan-details-request"
    assert record.downstream_request_id == "rag-request-id"


def test_mols_normalized_pcp_row_maps_to_primary_care():
    catalog = {
        "plan_id": "H4036-008-000",
        "plan_name": "Example Medicare Advantage PPO",
    }
    source_plan = {
        "plan_id": "5872",
        "contract_code": catalog["plan_id"],
        "coverage_type": "MAPD",
        "plan_name": catalog["plan_name"],
        "benefits": [
            {
                "code": "PCP",
                "name": "Primary Care",
                "value": "$5 copay",
            }
        ],
    }

    result, _ = _call(
        _context([catalog]),
        FakeResponse(_source_response([source_plan])),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.PRIMARY_CARE],
    )

    assert result.outcome == "complete"
    assert result.plans[0].details[0].benefits[0].code == "PCP"
    assert result.plans[0].details[0].benefits[0].name == "Primary Care"


@pytest.mark.parametrize(
    ("detail_type", "code", "all_available_plans", "needs_setting_guidance"),
    [
        (DetailType.PRIMARY_CARE, "PCP_Copay_IN", False, True),
        (DetailType.SPECIALIST, "Specialist_Copay_IN", False, True),
        (DetailType.PRIMARY_CARE, "PCP_Copay_IN", True, False),
        (DetailType.SPECIALIST, "Specialist_Copay_IN", True, False),
        (DetailType.URGENT_CARE, "Urgently_Needed_Care", False, False),
    ],
)
def test_setting_guidance_preserves_values_and_full_catalog_boundary(
    detail_type, code, all_available_plans, needs_setting_guidance
):
    catalog = {"plan_id": "H4036-008-000", "plan_name": "Example Medicare Advantage PPO"}
    source = {
        **catalog,
        "contract_code": catalog["plan_id"],
        "coverage_type": "MAPD",
        "benefits": [{"code": code, "name": "Visit", "value": "$5 copay"}],
    }
    result, _ = _call(
        _context([catalog]),
        FakeResponse(_source_response([source])),
        plan_ids=[] if all_available_plans else [catalog["plan_id"]],
        detail_types=[detail_type],
        all_available_plans=all_available_plans,
    )
    assert result.coverage_complete is True
    assert result.fallback_detail_types == []
    assert result.plans[0].details[0].benefits[0].value == "$5 copay"
    if needs_setting_guidance:
        assert "call search_plans" in result.response_instructions
        assert "do not explicitly establish" in result.response_instructions
    else:
        assert result.response_instructions is None


def test_combined_health_rx_deductible_supports_both_requested_dimensions():
    catalog = {"plan_id": "97UA", "plan_name": "Example Silver Pathway 6000"}
    source_plan = {
        "plan_id": "413116",
        "contract_code": catalog["plan_id"],
        "plan_name": catalog["plan_name"],
        "benefits": [
            {
                "code": "MEDDEDUCTIBLEFAM",
                "name": "Deductible(s)",
                "value": "Deductible (Health + Rx): $6,000 individual / $12,000 family",
            }
        ],
    }

    result, _ = _call(
        _context([catalog], market="IOLS", quote={"brand": "ABCBS"}, state="OH"),
        FakeResponse(_source_response([source_plan], market="IOLS")),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.MEDICAL_DEDUCTIBLE, DetailType.PHARMACY_DEDUCTIBLE],
    )

    assert result.outcome == "complete"
    assert [detail.detail_type for detail in result.plans[0].details] == [
        DetailType.MEDICAL_DEDUCTIBLE,
        DetailType.PHARMACY_DEDUCTIBLE,
    ]
    assert all(
        detail.benefits[0].value.startswith("Deductible (Health + Rx)")
        for detail in result.plans[0].details
    )


def test_separate_prescription_deductible_supports_the_pharmacy_dimension():
    catalog = {"plan_id": "8YC8", "plan_name": "Anthem Gold Pathway X Transition 2300"}
    deductible = (
        "Health Deductible: $2,300 per Individual up to $4,600 for Family; "
        "separate prescription drug deductible: $600 per Individual or $1,200 for Family"
    )
    source_plan = {
        "plan_id": "414087",
        "contract_code": catalog["plan_id"],
        "plan_name": catalog["plan_name"],
        "benefits": [
            {
                "code": "MEDDEDUCTIBLEFAM",
                "name": "Deductible(s)",
                "value": deductible,
            }
        ],
    }

    result, _ = _call(
        _context([catalog], market="IOLS", quote={"brand": "ABCBS"}, state="OH"),
        FakeResponse(_source_response([source_plan], market="IOLS")),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.MEDICAL_DEDUCTIBLE, DetailType.PHARMACY_DEDUCTIBLE],
    )

    assert result.outcome == "complete"
    assert result.coverage_complete is True
    assert [detail.detail_type for detail in result.plans[0].details] == [
        DetailType.MEDICAL_DEDUCTIBLE,
        DetailType.PHARMACY_DEDUCTIBLE,
    ]
    assert all(detail.benefits[0].value == deductible for detail in result.plans[0].details)


@pytest.mark.parametrize(
    ("value", "expected_complete"),
    [
        pytest.param(None, False, id="null"),
        pytest.param("", False, id="empty"),
        pytest.param(" ", False, id="whitespace"),
        pytest.param("N/A", False, id="not-applicable"),
        pytest.param("0", True, id="zero"),
    ],
)
def test_cost_coverage_requires_an_answerable_value(value, expected_complete):
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}
    source_plan = {
        "plan_id": "source-1",
        "contract_code": catalog["plan_id"],
        "plan_name": catalog["plan_name"],
        "cost_coverages": [
            {
                "code": "OOPMAXFAM",
                "description": "Out of Pocket Max",
                "value": value,
            }
        ],
    }

    result, _ = _call(
        _context([catalog], market="IOLS", quote={"brand": "ABCBS"}, state="OH"),
        FakeResponse(_source_response([source_plan], market="IOLS")),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.OUT_OF_POCKET_MAXIMUM],
    )

    assert result.coverage_complete is expected_complete
    if expected_complete:
        assert result.outcome == "complete"
        assert result.plans[0].details[0].cost_coverages[0].value == "0"
        assert result.response_instructions is None
    else:
        assert result.outcome == "unavailable"
        assert result.plans[0].missing_detail_types == [DetailType.OUT_OF_POCKET_MAXIMUM]
        assert result.fallback_detail_types == [DetailType.OUT_OF_POCKET_MAXIMUM]
        assert "Call search_plans before finalizing" in result.response_instructions


@pytest.mark.parametrize(
    ("premium", "expected_complete"),
    [
        pytest.param({"subsidy_applied": 50}, False, id="subsidy-only"),
        pytest.param({"total": 0}, True, id="zero-total"),
        pytest.param({"subsidized": 0}, True, id="zero-subsidized"),
    ],
)
def test_premium_requires_an_answerable_monthly_amount(premium, expected_complete):
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}
    source_plan = {
        "plan_id": "source-1",
        "contract_code": catalog["plan_id"],
        "plan_name": catalog["plan_name"],
        "premium": premium,
    }

    result, _ = _call(
        _context([catalog], market="IOLS", quote={"brand": "ABCBS"}, state="OH"),
        FakeResponse(_source_response([source_plan], market="IOLS")),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.PREMIUM],
    )

    assert result.coverage_complete is expected_complete
    if expected_complete:
        assert result.outcome == "complete"
        assert result.plans[0].missing_detail_types == []
        assert result.response_instructions is None
        assert result.premium_summary is not None
    else:
        assert result.outcome == "unavailable"
        assert result.plans[0].details == []
        assert result.plans[0].missing_detail_types == [DetailType.PREMIUM]
        assert result.fallback_detail_types == []
        assert result.response_instructions is None
        assert result.premium_summary is None


@pytest.mark.parametrize("with_missing_cost", [False, True])
def test_premium_summary_preserves_zero_ties_unknowns_and_fallback(with_missing_cost):
    premiums = [7.10, 0, 0, 0, 0, None]
    catalog = [
        {"plan_id": f"P{index}", "plan_name": f"Plan {index}"} for index in range(len(premiums))
    ]
    source = [
        {
            "plan_id": plan["plan_id"],
            "contract_code": plan["plan_id"],
            "coverage_type": "MAPD",
            "plan_name": plan["plan_name"],
            "premium": {"total": premium},
        }
        for plan, premium in zip(catalog, premiums, strict=True)
    ]
    requested = [DetailType.PREMIUM]
    if with_missing_cost:
        requested.append(DetailType.MEDICAL_DEDUCTIBLE)

    result, _ = _call(
        _context(catalog),
        FakeResponse(_source_response(source)),
        plan_ids=[plan["plan_id"] for plan in catalog],
        detail_types=requested,
    )

    assert result.outcome == "partial"
    assert [plan.details[0].premium.total for plan in result.plans[:-1]] == premiums[:-1]
    assert DetailType.PREMIUM in result.plans[-1].missing_detail_types
    summary = result.premium_summary.total
    assert summary.lowest.monthly_amount == 0
    assert [plan.plan_id for plan in summary.lowest.plans] == ["P1", "P2", "P3", "P4"]
    assert summary.highest.monthly_amount == 7.1
    assert [plan.plan_id for plan in summary.highest.plans] == ["P0"]
    assert [plan.plan_id for plan in summary.unknown_plans] == ["P5"]
    if with_missing_cost:
        assert "monthly_amount" not in result.response_instructions
        assert "Call search_plans before finalizing" in result.response_instructions
    assert DetailType.PREMIUM not in result.fallback_detail_types


@pytest.mark.parametrize(
    ("amounts", "lowest", "highest", "unknown"),
    [
        (["100", "9.50", "87"], (9.5, ["P1"]), (100, ["P0"]), []),
        ([7.1, 0, 0, 87], (0, ["P1", "P2"]), (87, ["P3"]), []),
        ([10, 10], (10, ["P0", "P1"]), (10, ["P0", "P1"]), []),
        ([-1, 0, 1], (-1, ["P0"]), (1, ["P2"]), []),
        ([None, float("nan"), float("inf"), 0], (0, ["P3"]), (0, ["P3"]), ["P0", "P1", "P2"]),
    ],
)
def test_premium_summary_compares_numbers_and_preserves_plan_identity(
    amounts, lowest, highest, unknown
):
    plans = [
        plan_details.CanonicalPlanDetails(
            plan_id=f"P{index}",
            plan_name="Same name",
            details=[
                plan_details.DetailResult(
                    detail_type=DetailType.PREMIUM,
                    evidence_scope="Monthly premium",
                    premium=plan_details.PlanPremium(total=amount),
                )
            ],
        )
        for index, amount in enumerate(amounts)
    ]

    summary = plan_details._premium_comparison_summary(plans)

    assert summary.total.lowest.monthly_amount == lowest[0]
    assert [plan.plan_id for plan in summary.total.lowest.plans] == lowest[1]
    assert summary.total.highest.monthly_amount == highest[0]
    assert [plan.plan_id for plan in summary.total.highest.plans] == highest[1]
    assert [plan.plan_id for plan in summary.total.unknown_plans] == unknown
    assert all(plan.plan_name == "Same name" for plan in summary.total.lowest.plans)
    assert summary.subsidized.lowest is None
    assert summary.subsidized.highest is None
    assert [plan.plan_id for plan in summary.subsidized.unknown_plans] == [
        plan.plan_id for plan in plans
    ]
    assert (
        plan_details.PremiumComparisonSummary.model_validate_json(summary.model_dump_json())
        == summary
    )


def test_premium_summary_separates_total_net_and_missing_plans():
    catalog = [{"plan_id": f"P{index}", "plan_name": f"Plan {index}"} for index in range(4)]
    source = [
        {
            **plan,
            "contract_code": plan["plan_id"],
            "premium": premium,
        }
        for plan, premium in zip(
            catalog,
            [
                {"total": 10, "subsidized": 9, "subsidy_applied": 1},
                {"total": 20, "subsidized": 0, "subsidy_applied": 20},
                {"total": 100, "subsidized": 100},
            ],
            strict=False,
        )
    ]

    result, _ = _call(
        _context(catalog, market="IOLS", quote={"brand": "ABCBS"}, state="OH"),
        FakeResponse(_source_response(source, market="IOLS")),
        plan_ids=["P0", "P1", "P3"],
        detail_types=[DetailType.PREMIUM],
    )

    summary = result.premium_summary
    assert summary.total.lowest.monthly_amount == 10
    assert summary.total.lowest.plans[0].plan_id == "P0"
    assert summary.total.highest.monthly_amount == 20
    assert summary.total.highest.plans[0].plan_id == "P1"
    assert summary.subsidized.lowest.monthly_amount == 0
    assert summary.subsidized.lowest.plans[0].plan_id == "P1"
    assert summary.subsidized.highest.monthly_amount == 9
    assert summary.subsidized.highest.plans[0].plan_id == "P0"
    assert [plan.plan_id for plan in summary.total.unknown_plans] == ["P3"]
    assert [plan.plan_id for plan in summary.subsidized.unknown_plans] == ["P3"]
    assert "P2" not in summary.model_dump_json()
    assert "subsidy_applied" not in summary.model_dump_json()


def test_drug_tiers_require_cost_sharing_values():
    catalog = {"plan_id": "H4036-008-000", "plan_name": "Anthem Advantage"}
    source_plan = {
        "plan_id": "5872",
        "contract_code": catalog["plan_id"],
        "coverage_type": "MAPD",
        "plan_name": catalog["plan_name"],
        "benefits": [{"code": "GENPREDRUGS", "name": "Generic drugs", "value": "N/A"}],
        "tiers": [{"name": "Preferred Generic", "level": "1", "retail_value": "N/A"}],
    }

    result, _ = _call(
        _context([catalog]),
        FakeResponse(_source_response([source_plan])),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.DRUG_TIERS],
    )

    assert result.outcome == "unavailable"
    assert result.coverage_complete is False
    assert result.plans[0].missing_detail_types == [DetailType.DRUG_TIERS]
    assert result.fallback_detail_types == [DetailType.DRUG_TIERS]


def test_na_benefit_value_marks_detail_missing_and_allows_document_fallback():
    catalog = {"plan_id": "2985_WI", "plan_name": "Plan G"}
    source_plan = {
        "plan_id": "2985",
        "coverage_type": "MED_SUPP",
        "plan_name": catalog["plan_name"],
        "benefits": [
            {
                "code": "PartB_Copay",
                "name": "Medicare Part B Copayment",
                "value": "N/A",
                "benefit_type": "DETAIL",
            }
        ],
    }

    result, _ = _call(
        _context([catalog]),
        FakeResponse(_source_response([source_plan])),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.EMERGENCY_ROOM],
    )

    assert result.outcome == "unavailable"
    assert result.plans[0].details == []
    assert result.plans[0].missing_detail_types == [DetailType.EMERGENCY_ROOM]
    assert result.fallback_detail_types == [DetailType.EMERGENCY_ROOM]


def test_drug_tiers_retain_zero_costs_and_drop_unanswerable_rows():
    catalog = {"plan_id": "H4036-008-000", "plan_name": "Anthem Advantage"}
    source_plan = {
        "plan_id": "5872",
        "contract_code": catalog["plan_id"],
        "coverage_type": "MAPD",
        "plan_name": catalog["plan_name"],
        "tiers": [
            {"name": "Unpriced Tier", "level": "0"},
            {"name": "Preferred Generic", "level": "1", "retail_value": "$0"},
            {"name": "Specialty", "level": "5", "mail_order_value": "25%"},
        ],
    }

    result, _ = _call(
        _context([catalog]),
        FakeResponse(_source_response([source_plan])),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.DRUG_TIERS],
    )

    assert result.outcome == "complete"
    assert result.coverage_complete is True
    assert [tier.name for tier in result.plans[0].details[0].tiers] == [
        "Preferred Generic",
        "Specialty",
    ]


def test_iols_contract_code_maps_duplicate_source_id_to_the_requested_variant():
    selected = {
        "plan_id": "8Y7K",
        "plan_name": "Example Essential Silver 6000",
    }
    other = {
        "plan_id": "8Y7V",
        "plan_name": "Wellpoint Essential Silver POS 6000 Standard",
    }
    source_plans = [
        {
            "plan_id": "412301",
            "contract_code": "8Y7K",
            "plan_name": selected["plan_name"],
            "premium": {"total": 1051.16, "subsidized": 459.33, "subsidy_applied": 591.83},
            "benefits": [
                {
                    "code": "EMERGENCYROOM",
                    "name": "Emergency Room Services",
                    "value": "You pay $500 per visit after deductible.",
                }
            ],
        },
        {
            "plan_id": "412301",
            "contract_code": "8Y7V",
            "plan_name": other["plan_name"],
            "premium": {"total": 999.0},
        },
    ]
    result, captured = _call(
        _context(
            [selected, other],
            market="IOLS",
            quote={
                "zip_code": "75001",
                "county_code": "113",
                "county_name": "DALLAS",
                "applicants": [{"applicant_type": "PRIMARY", "date_of_birth": "1991-01-01"}],
            },
            state="TX",
        ),
        FakeResponse(_source_response(source_plans, market="IOLS")),
        plan_ids=["8Y7K"],
        detail_types=[DetailType.PREMIUM, DetailType.EMERGENCY_ROOM],
    )

    assert result.outcome == "complete"
    assert result.plans[0].plan_id == "8Y7K"
    assert result.plans[0].details[0].premium.total == 1051.16
    assert result.plans[0].details[0].premium.subsidized == 459.33
    assert "412301" not in result.model_dump_json()
    assert captured["url"] == "https://rag.example.test/plans/quote"


def test_state_suffixed_medsupp_id_matches_source_plan_id():
    catalog = {"plan_id": "2985_OH", "plan_name": "Plan G"}
    response = _source_response(
        [
            {
                "plan_id": "2985",
                "contract_code": None,
                "coverage_type": "MED_SUPP",
                "plan_name": "Plan G",
                "benefits": [
                    {
                        "code": "PartB_Excess_Charges",
                        "name": "Medicare Part B Excess Charges",
                        "value": "You Pay: $0",
                    },
                    {
                        "code": "Emergency_Care",
                        "name": "Emergency Care",
                        "value": "$50.00 copay",
                    },
                ],
            }
        ]
    )

    result, _ = _call(
        _context([catalog], state="OH"),
        FakeResponse(response),
        plan_ids=["2985_OH"],
        detail_types=[DetailType.EMERGENCY_ROOM],
    )

    assert result.outcome == "complete"
    assert result.plans[0].plan_id == "2985_OH"
    assert result.plans[0].details[0].benefits[0].value == "$50.00 copay"
    assert '2985"' not in result.model_dump_json()


@pytest.mark.parametrize(
    "missing_field", [None, "date_of_birth", "gender", "part_a_eff_date", "part_b_eff_date"]
)
@pytest.mark.parametrize("missing_value", [None, "", "  ", "absent"])
@pytest.mark.parametrize("detail_type", [DetailType.PREMIUM, DetailType.EMERGENCY_ROOM])
def test_medsupp_addendum_reflects_quote_inputs(missing_field, missing_value, detail_type):
    catalog = {"plan_id": "2985_OH", "plan_name": "Plan G"}
    response = _source_response(
        [
            {
                "plan_id": "2985",
                "contract_code": None,
                "coverage_type": "MED_SUPP",
                "plan_name": "Plan G",
                "premium": {"total": 125.0},
                "benefits": [
                    {"code": "Emergency_Care", "name": "Emergency Care", "value": "$50 copay"}
                ],
            }
        ]
    )
    quote = {
        "zip_code": "45011",
        "county_code": "39017",
        "county_name": "BUTLER",
        "applicants": [
            {
                "applicant_type": "PRIMARY",
                "date_of_birth": "1957-01-01",
                "gender": "FEMALE",
            }
        ],
        "part_a_eff_date": "2026-08-01",
        "part_b_eff_date": "2026-07-01",
    }
    if missing_field:
        target = quote["applicants"][0] if missing_field in {"date_of_birth", "gender"} else quote
        if missing_value == "absent":
            target.pop(missing_field)
        else:
            target[missing_field] = missing_value

    result, _ = _call(
        _context([catalog], quote=quote, state="OH"),
        FakeResponse(response),
        plan_ids=[catalog["plan_id"]],
        detail_types=[detail_type],
    )

    assert result.outcome == "complete"
    assert result.required_response_addendum == (
        "For accurate information about Medicare Supplement (MedSup) plans, go to the Medicare "
        "Supplement tab and provide your gender, date of birth, and Medicare Part A and Part B "
        "effective dates."
        if missing_field
        else None
    )


@pytest.mark.parametrize(
    ("market", "coverage_type", "has_details", "expected_addendum"),
    [
        ("MOLS", "MED_SUPP", True, True),
        ("MOLS", "MED_SUPP", False, False),
        ("MOLS", "MA", True, False),
        ("MOLS", "MAPD", True, False),
        ("IOLS", None, True, False),
        ("IOLS", "MED_SUPP", True, False),
    ],
)
def test_medsupp_addendum_requires_medicare_and_supported_medsupp_details(
    market, coverage_type, has_details, expected_addendum
):
    catalog = {"plan_id": "2985_OH", "plan_name": "Selected Plan"}
    source_plan = {
        "plan_id": "2985",
        "contract_code": catalog["plan_id"],
        "coverage_type": coverage_type,
        "plan_name": catalog["plan_name"],
    }
    if has_details:
        source_plan["premium"] = {"total": 125.0}
    result, _ = _call(
        _context([catalog], market=market, state="OH"),
        FakeResponse(_source_response([source_plan], market=market)),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.PREMIUM],
    )

    assert result.outcome == ("complete" if has_details else "unavailable")
    assert result.required_response_addendum == (
        plan_details.INCOMPLETE_MEDSUPP_QUOTE_ADDENDUM if expected_addendum else None
    )


def test_partial_result_allows_only_missing_nonpremium_document_fallback():
    catalog = {"plan_id": "H4036-008-000", "plan_name": "Anthem Advantage"}
    response = _source_response(
        [
            {
                "plan_id": "5872",
                "contract_code": catalog["plan_id"],
                "coverage_type": "MAPD",
                "plan_name": catalog["plan_name"],
                "benefits": [
                    {
                        "code": "Emergency_Care",
                        "name": "Emergency Care",
                        "value": "$115.00 copay",
                    }
                ],
            }
        ]
    )

    result, _ = _call(
        _context([catalog]),
        FakeResponse(response),
        plan_ids=[catalog["plan_id"]],
        detail_types=[
            DetailType.PREMIUM,
            DetailType.EMERGENCY_ROOM,
            DetailType.PHARMACY_DEDUCTIBLE,
        ],
    )

    assert result.outcome == "partial"
    assert [item.detail_type for item in result.definitions.detail_types] == [
        DetailType.PREMIUM,
        DetailType.EMERGENCY_ROOM,
        DetailType.PHARMACY_DEDUCTIBLE,
    ]
    assert result.plans[0].missing_detail_types == [
        DetailType.PREMIUM,
        DetailType.PHARMACY_DEDUCTIBLE,
    ]
    assert result.fallback_detail_types == [DetailType.PHARMACY_DEDUCTIBLE]
    assert "Call search_plans before finalizing" in result.response_instructions
    scope = json.loads(result.response_instructions.split("scope: ", 1)[1].split(". Use", 1)[0])
    assert scope == [
        {
            "plan_id": catalog["plan_id"],
            "plan_name": catalog["plan_name"],
            "detail_types": ["pharmacy_deductible"],
        }
    ]
    assert "Never search documents for premium amounts" in result.response_instructions


def test_fallback_instructions_target_only_plan_with_missing_detail():
    catalog = [
        {"plan_id": "2894_CT", "plan_name": "Plan A"},
        {"plan_id": "H2836-006-000", "plan_name": "Anthem Full Dual Advantage"},
    ]
    response = _source_response(
        [
            {"plan_id": "2894", "plan_name": "Plan A", "coverage_type": "MED_SUPP"},
            {
                "plan_id": "5846",
                "plan_name": "Anthem Full Dual Advantage",
                "contract_code": "H2836-006-000",
                "coverage_type": "MAPD",
                "cost_coverages": [
                    {
                        "code": "OOP_MAX",
                        "description": "Annual Out-of-Pocket Maximum",
                        "value": "$9250",
                    }
                ],
            },
        ]
    )
    result, _ = _call(
        _context(catalog, state="CT"),
        FakeResponse(response),
        plan_ids=[plan["plan_id"] for plan in catalog],
        detail_types=[DetailType.OUT_OF_POCKET_MAXIMUM],
    )
    assert result.outcome == "partial"
    assert result.plans[1].details[0].cost_coverages[0].value == "$9250"
    scope = json.loads(result.response_instructions.split("scope: ", 1)[1].split(". Use", 1)[0])
    assert scope == [
        {"plan_id": "2894_CT", "plan_name": "Plan A", "detail_types": ["out_of_pocket_maximum"]}
    ]


def test_missing_quote_context_fails_before_http_without_document_fallback():
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}
    context = _context([catalog])
    context.request_context["application_plan_quote_inputs"] = {}

    with patch.object(plan_details.httpx, "AsyncClient") as client:
        result = asyncio.run(
            _ASYNC_GET_PLAN_DETAILS(
                plan_ids=["P1"],
                detail_types=[DetailType.PREMIUM, DetailType.MEDICAL_DEDUCTIBLE],
                context=context,
            )
        )

    client.assert_not_called()
    assert result.outcome == "unavailable"
    assert [item.detail_type for item in result.definitions.detail_types] == [
        DetailType.PREMIUM,
        DetailType.MEDICAL_DEDUCTIBLE,
    ]
    assert result.error_message is None
    assert result.unavailability_reason == "structured_quote_unavailable"
    assert result.plans_searched_for == ["Plan One"]
    assert result.missing_plans == []
    assert result.fallback_detail_types == []
    assert result.required_response == plan_details.STRUCTURED_PLAN_QUOTE_USER_MESSAGE


def test_connection_lookup_failure_returns_audited_error_without_http():
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}
    with (
        patch.object(
            plan_details.connections,
            "key_value",
            side_effect=RuntimeError("private provider failure"),
        ),
        patch.object(plan_details.httpx, "AsyncClient") as client,
    ):
        result = asyncio.run(
            _ASYNC_GET_PLAN_DETAILS(
                plan_ids=["P1"],
                detail_types=[DetailType.PREMIUM],
                context=_context([catalog]),
            )
        )

    client.assert_not_called()
    assert result.outcome == "error"
    assert result.error_message == "structured Plans API connection is unavailable"
    assert result.error_code == plan_details.PlanDetailsErrorCode.CONNECTION_UNAVAILABLE
    assert result.retryable is True
    assert result.required_response == plan_details.PREMIUM_DETAILS_UNAVAILABLE_MESSAGE
    assert "private provider failure" not in result.model_dump_json()
    assert result.audit_completed is True


@pytest.mark.parametrize(
    ("response", "expected_message", "expected_code", "expected_status", "retryable"),
    [
        (
            FakeResponse({}, status_code=503),
            "temporarily unavailable (HTTP 503)",
            plan_details.PlanDetailsErrorCode.SERVICE_UNAVAILABLE,
            503,
            True,
        ),
        (
            FakeResponse({}, status_code=422),
            "rejected the quote request (HTTP 422)",
            plan_details.PlanDetailsErrorCode.REQUEST_REJECTED,
            422,
            False,
        ),
        (
            FakeResponse({}, status_code=500),
            "service failed (HTTP 500)",
            plan_details.PlanDetailsErrorCode.SERVICE_ERROR,
            500,
            True,
        ),
        (
            FakeResponse(_source_response([], market="IOLS")),
            "structured Plans API response consistency failed",
            plan_details.PlanDetailsErrorCode.RESPONSE_INCONSISTENT,
            200,
            False,
        ),
        (
            FakeResponse({"market": "MOLS"}),
            "contract validation failed",
            plan_details.PlanDetailsErrorCode.RESPONSE_INVALID,
            200,
            False,
        ),
        (
            FakeResponse(ValueError("not json")),
            "returned invalid JSON (HTTP 200)",
            plan_details.PlanDetailsErrorCode.RESPONSE_INVALID,
            200,
            False,
        ),
    ],
)
def test_upstream_failures_return_no_facts(
    response,
    expected_message,
    expected_code,
    expected_status,
    retryable,
):
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}

    result, _ = _call(
        _context([catalog]),
        response,
        plan_ids=["P1"],
        detail_types=[DetailType.MEDICAL_DEDUCTIBLE],
    )

    assert result.outcome == "error"
    assert result.plans == []
    assert expected_message in result.error_message
    assert result.error_code == expected_code
    assert result.http_status == expected_status
    assert result.retryable is retryable
    assert result.fallback_detail_types == [DetailType.MEDICAL_DEDUCTIBLE]
    assert result.required_response is None
    assert '"plan_id": "P1"' in result.response_instructions
    assert '"detail_types": ["medical_deductible"]' in result.response_instructions


def test_premium_api_failure_requires_safe_standard_response():
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}

    result, _ = _call(
        _context([catalog]),
        FakeResponse({}, status_code=503),
        plan_ids=["P1"],
        detail_types=[DetailType.PREMIUM],
    )

    assert result.outcome == "error"
    assert result.plans == []
    assert result.fallback_detail_types == []
    assert result.required_response == plan_details.PREMIUM_DETAILS_UNAVAILABLE_MESSAGE
    assert result.response_instructions is None
    assert result.error_code == plan_details.PlanDetailsErrorCode.SERVICE_UNAVAILABLE
    assert result.http_status == 503
    assert result.retryable is True
    assert result.escalation == {
        "type": True,
        "identification": "structured_plan_service_unavailable",
        "description": "structured plan-details service is temporarily unavailable (HTTP 503)",
    }


def test_full_catalog_upstream_failure_requires_unavailable_response_without_fallback():
    catalog = [
        {"plan_id": "P1", "plan_name": "Plan One"},
        {"plan_id": "P2", "plan_name": "Plan Two"},
    ]

    result, _ = _call(
        _context(catalog),
        FakeResponse({}, status_code=503),
        plan_ids=[],
        detail_types=[DetailType.MEDICAL_DEDUCTIBLE],
        all_available_plans=True,
    )

    assert result.outcome == "error"
    assert result.missing_plans == ["Plan One", "Plan Two"]
    assert result.fallback_detail_types == []
    assert result.required_response == plan_details.FULL_CATALOG_DETAILS_UNAVAILABLE_MESSAGE
    assert result.response_instructions is None


def test_partial_full_catalog_result_keeps_supported_facts_without_document_fallback():
    catalog = [
        {"plan_id": "P1", "plan_name": "Plan One"},
        {"plan_id": "P2", "plan_name": "Plan Two"},
    ]
    source_plan = {
        "plan_id": "source-1",
        "contract_code": "P1",
        "coverage_type": "MAPD",
        "plan_name": "Plan One",
        "cost_coverages": [
            {
                "code": "ANNUAL_DEDUCTIBLE",
                "description": "Annual Deductible",
                "value": "100",
            }
        ],
    }

    result, _ = _call(
        _context(catalog),
        FakeResponse(_source_response([source_plan])),
        plan_ids=[],
        detail_types=[DetailType.MEDICAL_DEDUCTIBLE],
        all_available_plans=True,
    )

    assert result.outcome == "partial"
    assert result.plans_found == ["Plan One"]
    assert result.missing_plans == ["Plan Two"]
    assert result.fallback_detail_types == []
    assert result.required_response is None


def test_ambiguous_source_identity_is_not_guessed():
    catalog = {"plan_id": "2985_OH", "plan_name": "Plan G"}
    duplicated = {
        "plan_id": "2985",
        "contract_code": None,
        "coverage_type": "MED_SUPP",
        "plan_name": "Plan G",
        "premium": {"total": 100.0},
    }
    response = _source_response([duplicated, duplicated])

    result, _ = _call(
        _context([catalog], state="OH"),
        FakeResponse(response),
        plan_ids=[catalog["plan_id"]],
        detail_types=[DetailType.PREMIUM],
    )

    assert result.outcome == "unavailable"
    assert result.plans_found == []
    assert result.missing_plans == ["Plan G"]
    assert result.fallback_detail_types == []


def test_detail_helpers_deduplicate_rows_and_match_named_pharmacy_deductible():
    benefit = plan_details.PlanBenefit(code="PCP", name="Primary Care", value="$0")
    assert plan_details._deduplicated([benefit, benefit]) == [benefit]

    source = plan_details.SourcePlan(
        plan_id="source-1",
        plan_name="Plan One",
        cost_coverages=[
            plan_details.CostCoverage(
                code="deductible",
                description="Pharmacy Deductible",
                value="0",
            )
        ],
    )
    detail = plan_details._detail(source, DetailType.PHARMACY_DEDUCTIBLE)

    assert detail is not None
    assert detail.cost_coverages[0].value == "0"


def test_mols_source_matching_ignores_wrong_contracts_and_unsupported_coverage_types():
    catalog = plan_details.PlanSummary(plan_id="H4036-008-000", plan_name="Plan One")
    candidates = [
        plan_details.SourcePlan(
            plan_id="source-1",
            plan_name="Plan One",
            contract_code="H0000-000-000",
            coverage_type=plan_details.MolsCoverageType.MEDICARE_ADVANTAGE,
        ),
        plan_details.SourcePlan(
            plan_id="source-2",
            plan_name="Plan One",
            contract_code=catalog.plan_id,
            coverage_type=plan_details.MolsCoverageType.ORIGINAL_MEDICARE,
        ),
    ]

    assert (
        plan_details._match_source_plan(
            catalog,
            candidates,
            "OH",
            plan_details.PlansMarket.MOLS,
        )
        is None
    )


def test_all_available_plans_must_be_boolean():
    with pytest.raises(ValueError, match="must be a boolean"):
        plan_details._selected_plans([], [], all_available_plans="true")


@pytest.mark.parametrize(
    ("plan_ids", "expected_message"),
    [
        (None, "at least one"),
        ([], "at least one"),
        (["P1", "P1"], "unique"),
        ([None], "invalid exact"),
        ([" P1"], "invalid exact"),
        (["P2"], "outside the current catalog"),
    ],
)
def test_plan_id_validation_failures_return_audited_errors(plan_ids, expected_message):
    context = _context([{"plan_id": "P1", "plan_name": "Plan One"}])

    result = asyncio.run(
        _ASYNC_GET_PLAN_DETAILS(
            plan_ids=plan_ids,
            detail_types=[DetailType.PREMIUM],
            context=context,
        )
    )

    assert result.outcome == "error"
    assert expected_message in result.error_message
    assert result.audit_completed is True


@pytest.mark.parametrize(
    ("detail_types", "expected_message"),
    [
        (None, "one to ten"),
        ([], "one to ten"),
        (["unsupported"], "unsupported value"),
        ([DetailType.PREMIUM, DetailType.PREMIUM], "unique"),
    ],
)
def test_detail_type_validation_failures_return_audited_errors(detail_types, expected_message):
    context = _context([{"plan_id": "P1", "plan_name": "Plan One"}])

    result = asyncio.run(
        _ASYNC_GET_PLAN_DETAILS(
            plan_ids=["P1"],
            detail_types=detail_types,
            context=context,
        )
    )

    assert result.outcome == "error"
    assert expected_message in result.error_message
    assert result.audit_completed is True


@pytest.mark.parametrize(
    "context_update",
    [
        {"application_market_segment": "Group"},
        {"user_brand": ""},
    ],
)
def test_incomplete_common_quote_context_disables_structured_details(context_update):
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}
    context = _context([catalog])
    context.request_context.update(context_update)

    with patch.object(plan_details.connections, "key_value") as connection:
        result = asyncio.run(
            _ASYNC_GET_PLAN_DETAILS(
                plan_ids=["P1"],
                detail_types=[DetailType.MEDICAL_DEDUCTIBLE],
                context=context,
            )
        )

    assert result.outcome == "unavailable"
    assert result.unavailability_reason == "structured_quote_unavailable"
    assert result.fallback_detail_types == []
    assert result.required_response == plan_details.STRUCTURED_PLAN_QUOTE_USER_MESSAGE
    connection.assert_not_called()


def test_missing_runtime_context_returns_an_unaudited_error():
    result = asyncio.run(
        _ASYNC_GET_PLAN_DETAILS(
            plan_ids=["P1"],
            detail_types=[DetailType.PREMIUM],
            context=None,
        )
    )

    assert result.outcome == "error"
    assert result.error_message == "agent runtime context is required"
    assert result.audit_completed is False


def test_details_fall_back_to_application_catalog_when_control_has_no_lookup():
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}
    context = _context([catalog], market="IOLS", quote={"brand": "ABCBS"}, state="OH")
    context.request_context[SEARCH_CONTROL_CONTEXT_KEY] = json.dumps({"route": "search_turn"})
    source = {
        "plan_id": "source-1",
        "contract_code": "P1",
        "plan_name": "Plan One",
        "premium": {"total": 10},
    }

    result, _ = _call(
        context,
        FakeResponse(_source_response([source], market="IOLS")),
        plan_ids=["P1"],
        detail_types=[DetailType.PREMIUM],
    )

    assert result.outcome == "complete"
    assert result.plans_found == ["Plan One"]


def test_missing_connection_values_return_an_audited_error_without_http():
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}
    with (
        patch.object(plan_details.connections, "key_value", return_value={}),
        patch.object(plan_details.httpx, "AsyncClient") as client,
    ):
        result = asyncio.run(
            _ASYNC_GET_PLAN_DETAILS(
                plan_ids=["P1"],
                detail_types=[DetailType.PREMIUM],
                context=_context([catalog]),
            )
        )

    client.assert_not_called()
    assert result.outcome == "error"
    assert result.error_message == "structured Plans API connection is not configured"
    assert result.error_code == plan_details.PlanDetailsErrorCode.CONNECTION_NOT_CONFIGURED
    assert result.retryable is False
    assert result.required_response == plan_details.PREMIUM_DETAILS_UNAVAILABLE_MESSAGE


def test_invalid_provider_endpoint_returns_an_audited_request_error():
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}
    connection = {
        "RAG_API_BASE_URL": "https://rag.example.test",
        "RAG_API_KEY": "secret",
        "RAG_PLANS_ENDPOINT": "/plans/quote",
    }
    with (
        patch.object(plan_details.connections, "key_value", return_value=connection),
        patch.object(
            plan_details, "build_endpoint_url", side_effect=ValueError("invalid endpoint")
        ),
        patch.object(plan_details.httpx, "AsyncClient") as client,
    ):
        result = asyncio.run(
            _ASYNC_GET_PLAN_DETAILS(
                plan_ids=["P1"],
                detail_types=[DetailType.PREMIUM],
                context=_context([catalog]),
            )
        )

    client.assert_not_called()
    assert result.outcome == "error"
    assert result.error_message == "structured Plans API request failed"
    assert result.error_code == plan_details.PlanDetailsErrorCode.REQUEST_FAILED
    assert result.retryable is True
    assert result.required_response == plan_details.PREMIUM_DETAILS_UNAVAILABLE_MESSAGE


def test_missing_source_plan_preserves_nonpremium_fallback_scope():
    catalog = {"plan_id": "P1", "plan_name": "Plan One"}

    result, _ = _call(
        _context([catalog]),
        FakeResponse(_source_response([])),
        plan_ids=["P1"],
        detail_types=[DetailType.MEDICAL_DEDUCTIBLE],
    )

    assert result.outcome == "unavailable"
    assert result.missing_plans == ["Plan One"]
    assert result.fallback_detail_types == [DetailType.MEDICAL_DEDUCTIBLE]
