"""Registered structured plan-details tool for current quoted plans."""

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, cast

import httpx
from ibm_watsonx_orchestrate.agent_builder.connections import (
    ConnectionType,
    ExpectedCredentials,
)
from ibm_watsonx_orchestrate.agent_builder.tools import ToolPermission, tool
from ibm_watsonx_orchestrate.run import connections
from ibm_watsonx_orchestrate.run.context import AgentRun
from pydantic import BaseModel, Field, ValidationError

from shared.audit import emit_audit_event
from shared.context import plan_identity_key, trusted_plan_control
from shared.medicare_supplement import INCOMPLETE_MEDSUPP_QUOTE_ADDENDUM
from shared.rag import (
    RAG_APP_ID,
    PlanSummary,
    build_endpoint_url,
    normalize_request_id,
    parse_available_plans,
)
from shared.response_addenda import capture_context_after_completion, record_response_addendum
from shared.routing import STRUCTURED_PLAN_QUOTE_USER_MESSAGE

RAG_CONNECTION_TYPE = cast(ConnectionType, cast(object, ConnectionType.KEY_VALUE))

logger = logging.getLogger(__name__)

PLAN_DETAILS_DESCRIPTION = (
    "Retrieve authoritative benefit coverage, cost-sharing, and premium values from the current "
    "quote for selected plans or the full application catalog. It supports multiple structured "
    "detail types in one call across any number of plans in that catalog. Use only when "
    "shopper_control.structured_plan_quote_available is true and request only the detail types "
    "needed. This tool does not establish named-medication coverage or whether a benefit belongs "
    "to base or optional plan coverage."
)
PLAN_IDS_DESCRIPTION = (
    "Exact trusted-catalog plan IDs for a selected subset, with no tool-specific count limit. "
    "Ignored when all_available_plans=true."
)
ALL_AVAILABLE_PLANS_DESCRIPTION = (
    "True selects every current-catalog plan for an exhaustive benefit check, comparison, or "
    "ranking and ignores plan_ids; false uses the selected plan_ids subset."
)
FULL_CATALOG_DETAILS_UNAVAILABLE_MESSAGE = (
    "The requested plan details are currently unavailable. Please try again later."
)
PREMIUM_DETAILS_UNAVAILABLE_MESSAGE = (
    "Premium information is currently unavailable. Please try again later."
)
ESSENTIAL_EXTRAS_RESPONSE_INSTRUCTIONS = (
    "For an Essential Extras question, plan_options rows are not program evidence. Even if "
    "their labels mention the same services, do not present those paid packages as Essential "
    "Extras or invent a selection limit. Fetch essential_extras for the same plans when it "
    "has not been requested. If its rows are missing, use the permitted document fallback; "
    "do not fill that gap with plan_options or routine benefits. If the documents establish "
    "only an ordinary benefit, leave Essential Extras membership unconfirmed.\n\n"
    "ESSENTIAL EXTRAS RULE: Essential Extras is a named program, not a synonym for extra "
    "benefits. Report only the selected plan's Essential_Extras, Essential_Extras_Options, "
    "and Essential_Extras_Selections values as structured program evidence. Read them together: "
    "options are alternatives subject to the supplied selection limit, not cumulative benefits. "
    "Missing terms do not mean noncoverage. Base-plan benefits, paid packages, and nearby "
    "transportation amounts do not describe Essential Extras unless evidence explicitly assigns "
    "them to that program. An additional option does not establish whether it adds to, replaces, "
    "or changes an existing allowance. A premium and selection limit do not establish whether "
    "one affects the other; never assert that choices are independent of premium without "
    "explicit evidence. Preserve every supported structured fact even when another requested "
    "fact or relationship cannot be established."
)
DETAIL_TYPES_DESCRIPTION = (
    "Array containing only these detail_type enum values: premium, medical_deductible, "
    "pharmacy_deductible, out_of_pocket_maximum, primary_care, specialist, urgent_care, "
    "emergency_room, plan_options, drug_tiers, essential_extras. essential_extras means the named "
    "program's availability, options, and selection limit, not routine benefits or paid packages. "
    "drug_tiers means generic tier cost sharing only "
    "and never a named medication's tier assignment. premium includes provider-calculated total, "
    "subsidized, and applied-subsidy monthly amounts. plan_options means provider-listed optional "
    "identifiers, display labels, and rates. A requested relationship between a benefit and base "
    "or optional coverage requires plan-document evidence. Financial assistance and ways to pay "
    "deductibles or copays are plan-independent education, not plan_options. Copy the matching "
    "value exactly and include only types needed for the shopper's current question."
)


class DetailType(StrEnum):
    """Structured plan facts supported by the Plans APIs."""

    PREMIUM = "premium"
    MEDICAL_DEDUCTIBLE = "medical_deductible"
    PHARMACY_DEDUCTIBLE = "pharmacy_deductible"
    OUT_OF_POCKET_MAXIMUM = "out_of_pocket_maximum"
    PRIMARY_CARE = "primary_care"
    SPECIALIST = "specialist"
    URGENT_CARE = "urgent_care"
    EMERGENCY_ROOM = "emergency_room"
    PLAN_OPTIONS = "plan_options"
    DRUG_TIERS = "drug_tiers"
    ESSENTIAL_EXTRAS = "essential_extras"


class PlanDetailsErrorCode(StrEnum):
    """Safe machine-readable categories for structured plan-detail failures."""

    CONNECTION_UNAVAILABLE = "connection_unavailable"
    CONNECTION_NOT_CONFIGURED = "connection_not_configured"
    REQUEST_FAILED = "request_failed"
    REQUEST_REJECTED = "request_rejected"
    SERVICE_UNAVAILABLE = "service_unavailable"
    SERVICE_ERROR = "service_error"
    RESPONSE_INVALID = "response_invalid"
    RESPONSE_INCONSISTENT = "response_inconsistent"


PLAN_DETAILS_COMPLETENESS_DEFINITION = (
    "coverage_complete indicates whether every requested structured detail type was returned for "
    "every selected plan."
)
DETAIL_TYPE_DEFINITIONS: dict[DetailType, str] = {
    DetailType.PREMIUM: (
        "Provider-calculated monthly premium values retained under their provider labels. Use "
        "the request-specific premium definition and do not derive or correct returned values "
        "arithmetically."
    ),
    DetailType.MEDICAL_DEDUCTIBLE: (
        "Provider cost-sharing rows mapped to the plan's medical or annual deductible."
    ),
    DetailType.PHARMACY_DEDUCTIBLE: (
        "Provider cost-sharing rows mapped to the plan's pharmacy or prescription deductible."
    ),
    DetailType.OUT_OF_POCKET_MAXIMUM: (
        "Provider cost-sharing rows mapped to the plan's annual out-of-pocket maximum."
    ),
    DetailType.PRIMARY_CARE: (
        "Provider cost-sharing and benefit rows labeled for primary-care services."
    ),
    DetailType.SPECIALIST: (
        "Provider cost-sharing and benefit rows labeled for specialist services."
    ),
    DetailType.URGENT_CARE: (
        "Provider cost-sharing and benefit rows labeled for urgent or urgently needed care."
    ),
    DetailType.EMERGENCY_ROOM: (
        "Provider cost-sharing and benefit rows labeled for emergency-room care."
    ),
    DetailType.PLAN_OPTIONS: (
        "Provider-listed optional plan-option identifiers, display labels, and rates. Benefit "
        "relationships to base or optional coverage require plan-document evidence."
    ),
    DetailType.DRUG_TIERS: (
        "Provider-listed generic drug-tier cost sharing for retail and mail-order fulfillment."
    ),
    DetailType.ESSENTIAL_EXTRAS: (
        "Provider Essential Extras availability, options, and selection limit. Read all supplied "
        "rows together; options are alternatives subject to that limit. Only the supplied terms "
        "are established. Missing requested terms require plan-document evidence."
    ),
}
DETAIL_TYPE_EVIDENCE_LIMITS: dict[DetailType, list[str]] = {
    DetailType.PRIMARY_CARE: [
        "Whether an ordinary visit price applies to online, telehealth, preferred-provider, or "
        "other service settings not explicitly named in the returned row.",
    ],
    DetailType.SPECIALIST: [
        "Whether an ordinary visit price applies to online, telehealth, preferred-provider, or "
        "other service settings not explicitly named in the returned row.",
    ],
    DetailType.ESSENTIAL_EXTRAS: [
        "Unstated options, selection limits, or eligibility.",
        "Relationships to premiums, routine benefits, or paid optional packages.",
    ],
    DetailType.PLAN_OPTIONS: [
        "Which benefits a listed option contains.",
        "Whether a benefit is included in or absent from base coverage.",
    ],
    DetailType.DRUG_TIERS: [
        "Coverage or tier assignment for a named medication.",
    ],
}


class DetailDefinition(BaseModel):
    """Code-owned meaning of one requested structured detail type."""

    detail_type: DetailType
    definition: str
    does_not_establish: list[str] = Field(default_factory=list)


class PlanDetailsDefinitions(BaseModel):
    """Code-owned interpretation index returned beside structured plan values."""

    completeness: str = PLAN_DETAILS_COMPLETENESS_DEFINITION
    detail_types: list[DetailDefinition] = Field(default_factory=list)


class PlansMarket(StrEnum):
    """Normalized Plans API markets."""

    IOLS = "IOLS"
    MOLS = "MOLS"


_PREMIUM_DEFINITIONS_BY_MARKET: dict[PlansMarket, str] = {
    PlansMarket.IOLS: (
        "For this quote, total is the gross monthly household premium before subsidy, subsidized "
        "is the net monthly household premium after subsidy, and subsidy_applied is the monthly "
        "subsidy actually applied, which may be lower than the requested subsidy. Cost-share "
        "reduction affects deductibles, copays, and coinsurance, not these premium fields. Treat "
        "returned values as authoritative; do not derive or correct them arithmetically. Null "
        "means the provider omitted the value or supplied a nonnumeric value."
    ),
    PlansMarket.MOLS: (
        "For this quote, total is the provider-calculated monthly premium. subsidized and "
        "subsidy_applied are not supplied; do not infer them. Treat the returned total as "
        "authoritative and do not derive or correct it arithmetically. Null means the provider "
        "omitted the value or supplied a nonnumeric value."
    ),
}


def _definitions(
    detail_types: list[DetailType],
    market: PlansMarket | None = None,
) -> PlanDetailsDefinitions:
    """Return request-specific definitions for requested types in request order."""

    premium_definition = (
        _PREMIUM_DEFINITIONS_BY_MARKET[market]
        if market is not None
        else DETAIL_TYPE_DEFINITIONS[DetailType.PREMIUM]
    )
    return PlanDetailsDefinitions(
        detail_types=[
            DetailDefinition(
                detail_type=detail_type,
                definition=(
                    premium_definition
                    if detail_type is DetailType.PREMIUM
                    else DETAIL_TYPE_DEFINITIONS[detail_type]
                ),
                does_not_establish=DETAIL_TYPE_EVIDENCE_LIMITS.get(detail_type, []),
            )
            for detail_type in detail_types
        ]
    )


class MolsCoverageType(StrEnum):
    """MOLS coverage categories that determine canonical plan identity."""

    MEDICARE_ADVANTAGE = "MA"
    MEDICARE_ADVANTAGE_PRESCRIPTION_DRUG = "MAPD"
    MEDICARE_SUPPLEMENT = "MED_SUPP"
    ORIGINAL_MEDICARE = "ORIG_MEDICARE"


class PlanDetailsOutcome(StrEnum):
    """Structured detail retrieval outcomes."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class PlanDetailsIntent(StrEnum):
    """Plan scopes exposed through tool metadata."""

    SPECIFIC_PLAN = "specific_plan"
    BROAD_PLANS = "broad_plans"


class BenefitSource(StrEnum):
    """Normalized Plans API benefit collections."""

    PLAN = "plan_benefit"
    ADDITIONAL = "additional_benefit"


class PlanPremium(BaseModel):
    """Provider-calculated monthly premiums; nullable values must not be recomputed."""

    total: float | None = Field(
        default=None,
        description="Provider-calculated total monthly premium when supplied.",
    )
    subsidized: float | None = Field(
        default=None,
        description=(
            "Provider-calculated net monthly household premium after the applied subsidy when "
            "supplied. This is not the subsidy amount."
        ),
    )
    subsidy_applied: float | None = Field(
        default=None,
        description=(
            "Provider-calculated monthly subsidy actually applied to the premium when supplied; "
            "it may be lower than the requested subsidy."
        ),
    )


class CostCoverage(BaseModel):
    """One normalized Plans API cost-coverage row."""

    code: str | None = None
    description: str
    value: str | None = None
    value_type: str | None = None


class PlanBenefit(BaseModel):
    """One normalized Plans API benefit row."""

    code: str | None = None
    name: str
    value: str | None = None
    benefit_type: str | None = None
    source: BenefitSource = BenefitSource.PLAN


class PlanOption(BaseModel):
    """One optional plan variant and its provider-supplied rate."""

    name: str
    display_name: str | None = None
    rate: float | None = None


class PlanTier(BaseModel):
    """One normalized retail and mail-order drug tier."""

    name: str
    level: str | None = None
    retail_frequency: str | None = None
    retail_value: str | None = None
    mail_order_frequency: str | None = None
    mail_order_value: str | None = None


class SourcePlan(BaseModel):
    """Internal normalized plan returned by the Code Engine Plans API."""

    plan_id: str
    plan_name: str
    contract_code: str | None = None
    coverage_type: MolsCoverageType | None = None
    premium: PlanPremium | None = None
    cost_coverages: list[CostCoverage] = Field(default_factory=list)
    benefits: list[PlanBenefit] = Field(default_factory=list)
    options: list[PlanOption] = Field(default_factory=list)
    tiers: list[PlanTier] = Field(default_factory=list)


class SourcePlansResponse(BaseModel):
    """Subset of the normalized Plans API response required by the adapter."""

    market: PlansMarket
    plan_count: int = Field(ge=0)
    plans: list[SourcePlan]


class DetailResult(BaseModel):
    """Requested source fragments for one structured detail type."""

    detail_type: DetailType
    evidence_scope: str = Field(
        description=("Code-owned meaning and evidence boundary for the adjacent provider values.")
    )
    premium: PlanPremium | None = None
    cost_coverages: list[CostCoverage] = Field(default_factory=list)
    benefits: list[PlanBenefit] = Field(default_factory=list)
    options: list[PlanOption] = Field(default_factory=list)
    tiers: list[PlanTier] = Field(default_factory=list)


class CanonicalPlanDetails(BaseModel):
    """Structured details attributed only to the application catalog identity."""

    plan_id: str = Field(description="Exact plan_id from the current application plan catalog")
    plan_name: str = Field(description="Exact plan_name from the same catalog entry")
    details: list[DetailResult] = Field(default_factory=list)
    missing_detail_types: list[DetailType] = Field(default_factory=list)


class PremiumExtreme(BaseModel):
    """One returned monthly amount and every selected plan tied at that amount."""

    monthly_amount: float
    plans: list[PlanSummary]


class PremiumComparison(BaseModel):
    """Extremes among known amounts, with unrankable plans explicitly separated."""

    lowest: PremiumExtreme | None = None
    highest: PremiumExtreme | None = None
    unknown_plans: list[PlanSummary] = Field(default_factory=list)


class PremiumComparisonSummary(BaseModel):
    """Independent rankings of selected plans' total and after-subsidy premiums."""

    total: PremiumComparison
    subsidized: PremiumComparison


class PlanDetailsResponse(BaseModel):
    """Catalog-authorized plan facts and explicit fallback scope."""

    outcome: PlanDetailsOutcome = Field(
        description=(
            "complete means every requested structured detail was returned for every plan; "
            "partial means at least one was returned; unavailable and error contain no facts"
        )
    )
    business_intent: PlanDetailsIntent | None = None
    requested_detail_types: list[DetailType] = Field(default_factory=list)
    definitions: PlanDetailsDefinitions = Field(
        default_factory=PlanDetailsDefinitions,
        description="Code-owned definitions for completeness and the requested detail types.",
    )
    plans: list[CanonicalPlanDetails] = Field(default_factory=list)
    plans_found: list[str] = Field(default_factory=list)
    plans_searched_for: list[str] = Field(default_factory=list)
    missing_plans: list[str] = Field(default_factory=list)
    coverage_complete: bool = Field(
        default=False,
        description=(
            "Whether every requested structured detail type was returned for every selected plan."
        ),
    )
    fallback_detail_types: list[DetailType] = Field(
        default_factory=list,
        description=(
            "Missing non-premium details that may be searched in plan documents. A premium "
            "amount never appears here because documents are not an allowed premium fallback."
        ),
    )
    response_instructions: str | None = Field(
        default=None,
        description="Guidance for retrieving missing non-premium facts.",
    )
    premium_summary: PremiumComparisonSummary | None = Field(
        default=None,
        description=(
            "Computed monthly premium extremes and all ties among selected plans with known "
            "amounts. Total and after-subsidy rankings are independent; unknowns are excluded."
        ),
    )
    required_response_addendum: str | None = None
    required_response: str | None = Field(
        default=None,
        description="Exact response to use when present.",
    )
    unavailability_reason: str | None = None
    error_message: str | None = None
    error_code: PlanDetailsErrorCode | None = Field(
        default=None,
        description="Safe machine-readable category for a structured plan-detail failure.",
        exclude_if=lambda value: value is None,
    )
    http_status: int | None = Field(
        default=None,
        description="HTTP status returned by the structured plan-details service, when present.",
        exclude_if=lambda value: value is None,
    )
    retryable: bool | None = Field(
        default=None,
        description="Whether retrying later may resolve this structured plan-detail failure.",
        exclude_if=lambda value: value is None,
    )
    retrieval_time_ms: int | None = None
    escalation: dict[str, Any] = Field(default_factory=lambda: {"type": False})
    audit_completed: bool = False


_DETAIL_CODES: dict[DetailType, set[str]] = {
    DetailType.ESSENTIAL_EXTRAS: {
        "essentialextras",
        "essentialextrasoptions",
        "essentialextrasselections",
    },
    DetailType.MEDICAL_DEDUCTIBLE: {
        "annualdeductible",
        "meddeductiblefam",
        "meddeductibleindv",
    },
    DetailType.PHARMACY_DEDUCTIBLE: {"deductible", "rxdeductible"},
    DetailType.OUT_OF_POCKET_MAXIMUM: {"annualoopmax", "oopmax", "oopmaxind"},
    DetailType.PRIMARY_CARE: {
        "pcp",
        "pcpcopay",
        "pcpcopayin",
        "pcpcopayind2c",
        "pcpcopayout",
        "physicianservices",
        "primoffvis",
    },
    DetailType.SPECIALIST: {
        "physicianservices",
        "spcloffvis",
        "specialistcopay",
        "specialistcopayin",
        "specialistcopayind2c",
        "specialistcopayout",
    },
    DetailType.URGENT_CARE: {"outpaturgcare", "urgentlyneededcare"},
    DetailType.EMERGENCY_ROOM: {"emergencycare", "emergencyroom", "partbcopay"},
    DetailType.DRUG_TIERS: {
        "genpredrugs",
        "nonpredrugs",
        "prefpredrugs",
        "spclpredrugs",
    },
}

_DETAIL_NAMES: dict[DetailType, set[str]] = {
    DetailType.MEDICAL_DEDUCTIBLE: {"annual deductible", "deductible(s)"},
    DetailType.PHARMACY_DEDUCTIBLE: {"pharmacy deductible"},
    DetailType.OUT_OF_POCKET_MAXIMUM: {
        "annual out-of-pocket maximum",
        "out of pocket max",
    },
    DetailType.PRIMARY_CARE: {
        "in network primary care physician (pcp)",
        "primary care",
        "primary care visit",
        "primary doctor (in network)",
    },
    DetailType.SPECIALIST: {"in network specialist", "specialist (in network)", "specialist visit"},
    DetailType.URGENT_CARE: {"urgent care centers or facilities", "urgently needed care"},
    DetailType.EMERGENCY_ROOM: {"emergency care", "emergency room services"},
}
_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]")
_COMBINED_HEALTH_RX = re.compile(r"\bhealth\s*\+\s*rx\b", re.IGNORECASE)
_PRESCRIPTION_DEDUCTIBLE = re.compile(
    r"\b(?:separate\s+)?(?:prescription(?:\s+drug)?|pharmacy|rx)\s+deductible\b",
    re.IGNORECASE,
)


def _has_text(value: str | None) -> bool:
    """Return whether a provider value contains answerable content, preserving zero strings."""

    return value is not None and bool(value.strip()) and value.strip().casefold() != "n/a"


def _deduplicated[ModelT: BaseModel](items: list[ModelT]) -> list[ModelT]:
    """Preserve provider order while removing only byte-equivalent normalized rows."""

    output: list[ModelT] = []
    seen: set[str] = set()
    for item in items:
        key = item.model_dump_json()
        if key not in seen:
            output.append(item)
            seen.add(key)
    return output


def _matches_detail(
    detail_type: DetailType,
    code: str | None,
    name: str,
    value: str | None = None,
) -> bool:
    code_key = _NON_ALPHANUMERIC.sub("", str(code or "").casefold())
    name_key = plan_identity_key(name)
    if detail_type is DetailType.PHARMACY_DEDUCTIBLE:
        if code_key in {"meddeductiblefam", "meddeductibleindv"}:
            source_text = f"{name} {value or ''}"
            return bool(
                _COMBINED_HEALTH_RX.search(source_text)
                or _PRESCRIPTION_DEDUCTIBLE.search(source_text)
            )
        if code_key == "deductible":
            return name_key == "pharmacy deductible"
    return code_key in _DETAIL_CODES.get(detail_type, ()) or name_key in _DETAIL_NAMES.get(
        detail_type, ()
    )


def _detail(source: SourcePlan, detail_type: DetailType) -> DetailResult | None:
    """Return only answerable provider rows for one requested detail type."""

    if detail_type is DetailType.PREMIUM:
        premium = source.premium
        if premium is None or (premium.total is None and premium.subsidized is None):
            return None
        return DetailResult(
            detail_type=detail_type,
            evidence_scope=DETAIL_TYPE_DEFINITIONS[detail_type],
            premium=premium,
        )
    if detail_type is DetailType.PLAN_OPTIONS:
        return (
            DetailResult(
                detail_type=detail_type,
                evidence_scope=DETAIL_TYPE_DEFINITIONS[detail_type],
                options=source.options,
            )
            if source.options
            else None
        )
    if detail_type is DetailType.DRUG_TIERS:
        tiers = [
            item
            for item in source.tiers
            if any(_has_text(value) for value in (item.retail_value, item.mail_order_value))
        ]
        tier_benefits = [
            item
            for item in source.benefits
            if _matches_detail(detail_type, item.code, item.name, item.value)
            and _has_text(item.value)
        ]
        if not tiers and not tier_benefits:
            return None
        return DetailResult(
            detail_type=detail_type,
            evidence_scope=DETAIL_TYPE_DEFINITIONS[detail_type],
            benefits=_deduplicated(tier_benefits),
            tiers=_deduplicated(tiers),
        )
    cost_coverages = [
        item
        for item in source.cost_coverages
        if _has_text(item.value)
        and _matches_detail(detail_type, item.code, item.description, item.value)
    ]
    benefits = [
        item
        for item in source.benefits
        if _has_text(item.value) and _matches_detail(detail_type, item.code, item.name, item.value)
    ]
    if not cost_coverages and not benefits:
        return None
    return DetailResult(
        detail_type=detail_type,
        evidence_scope=DETAIL_TYPE_DEFINITIONS[detail_type],
        cost_coverages=_deduplicated(cost_coverages),
        benefits=_deduplicated(benefits),
    )


def _one_source_plan(catalog: PlanSummary, candidates: list[SourcePlan]) -> SourcePlan | None:
    """Resolve one candidate by unique identity, using the catalog name only as a tiebreaker."""

    if len(candidates) == 1:
        return candidates[0]
    catalog_name = plan_identity_key(catalog.plan_name)
    named = [
        candidate
        for candidate in candidates
        if catalog_name == plan_identity_key(candidate.plan_name)
    ]
    return named[0] if len(named) == 1 else None


def _match_source_plan(
    catalog: PlanSummary,
    source_plans: list[SourcePlan],
    state_code: str,
    market: PlansMarket,
) -> SourcePlan | None:
    """Resolve one provider plan without changing the catalog identity exposed to the LLM."""

    if market is PlansMarket.IOLS:
        return _one_source_plan(
            catalog,
            [plan for plan in source_plans if plan.contract_code == catalog.plan_id],
        )

    suffix = f"_{state_code.upper()}" if state_code else ""
    matches: list[SourcePlan] = []
    for plan in source_plans:
        coverage_type = plan.coverage_type
        if coverage_type in {
            MolsCoverageType.MEDICARE_ADVANTAGE,
            MolsCoverageType.MEDICARE_ADVANTAGE_PRESCRIPTION_DRUG,
        }:
            if plan.contract_code == catalog.plan_id:
                matches.append(plan)
        elif (
            coverage_type is MolsCoverageType.MEDICARE_SUPPLEMENT
            and suffix
            and f"{plan.plan_id}{suffix}" == catalog.plan_id
        ):
            matches.append(plan)
    return _one_source_plan(catalog, matches)


def _selected_plans(
    available: list[PlanSummary],
    plan_ids: list[str],
    *,
    all_available_plans: bool,
) -> list[PlanSummary]:
    """Select the full catalog or validate exact IDs while preserving source order."""

    if not isinstance(all_available_plans, bool):
        raise ValueError("all_available_plans must be a boolean")
    if all_available_plans:
        return list(available)
    if not isinstance(plan_ids, list) or not plan_ids:
        raise ValueError(
            "plan_ids must contain at least one exact plan ID unless all_available_plans is true"
        )
    if len(set(plan_ids)) != len(plan_ids):
        raise ValueError("plan_ids must contain unique plan IDs")
    available_by_id = {plan.plan_id: plan for plan in available}
    selected: list[PlanSummary] = []
    for plan_id in plan_ids:
        if not isinstance(plan_id, str) or not plan_id or plan_id != plan_id.strip():
            raise ValueError("plan_ids contains an invalid exact plan ID")
        plan = available_by_id.get(plan_id)
        if plan is None:
            raise ValueError("plan_ids contains a plan outside the current catalog")
        selected.append(plan)
    return selected


def _requested_details(values: list[DetailType]) -> list[DetailType]:
    """Validate requested detail types and preserve their order."""

    if not isinstance(values, list) or not 1 <= len(values) <= len(DetailType):
        raise ValueError("detail_types must contain one to ten values")
    normalized: list[DetailType] = []
    for value in values:
        try:
            detail_type = value if isinstance(value, DetailType) else DetailType(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("detail_types contains an unsupported value") from exc
        if detail_type in normalized:
            raise ValueError("detail_types must contain unique values")
        normalized.append(detail_type)
    return normalized


def _quote_request(ctx: Mapping[str, Any]) -> tuple[PlansMarket, dict[str, Any]]:
    """Compose the provider-neutral rag-api request from minimal WXO context."""

    raw: Any = ctx.get("application_plan_quote_inputs")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("structured plan quote inputs are unavailable")
    segment = str(ctx.get("application_market_segment", "") or "").strip()
    if segment == "IND":
        market = PlansMarket.IOLS
    elif segment == "Medicare":
        market = PlansMarket.MOLS
    else:
        raise ValueError("structured plan quote market is invalid")
    common = {
        "market_segment": segment,
        "brand": str(ctx.get("user_brand", "") or "").strip(),
        "state_code": str(ctx.get("user_state_code", "") or "").strip(),
        "requested_eff_date": str(ctx.get("user_requested_eff_date", "") or "").strip(),
    }
    if not all(common.values()):
        raise ValueError("structured plan quote inputs are incomplete")
    return market, {**raw, **common}


def _finish(response: PlanDetailsResponse, runtime: AgentRun) -> PlanDetailsResponse:
    """Attach the required audit receipt to a structured-details response."""

    supported = response.outcome in {PlanDetailsOutcome.COMPLETE, PlanDetailsOutcome.PARTIAL}
    identification = (
        "structured_plan_response"
        if supported
        else f"structured_plan_{response.error_code}"
        if response.error_code is not None
        else "structured_plan_details_unavailable"
    )
    receipt = emit_audit_event(
        {
            "type": not supported,
            "identification": identification,
            "description": (
                "Structured plan details response logging"
                if supported
                else response.error_message or "Structured plan details were unavailable"
            ),
        },
        runtime,
        event_logger=logger,
    )
    response.escalation = dict(receipt.escalation)
    response.audit_completed = receipt.logged
    record_response_addendum(runtime, response.required_response_addendum)
    return response


def _premium_comparison_summary(plans: list[CanonicalPlanDetails]) -> PremiumComparisonSummary:
    """Identify numeric extremes and every tie separately for total and net premiums.

    Use only selected plans' returned amounts. Missing or nonfinite amounts cannot
    establish a ranking; list those plans separately without treating them as zero.
    """
    summary = PremiumComparisonSummary(total=PremiumComparison(), subsidized=PremiumComparison())
    for plan in plans:
        premium = next(
            (detail.premium for detail in plan.details if detail.detail_type is DetailType.PREMIUM),
            None,
        )
        identity = PlanSummary(plan_id=plan.plan_id, plan_name=plan.plan_name)
        for amount, comparison in (
            (premium.total if premium is not None else None, summary.total),
            (premium.subsidized if premium is not None else None, summary.subsidized),
        ):
            if amount is None or not math.isfinite(amount):
                comparison.unknown_plans.append(identity)
                continue
            if comparison.lowest is None or amount < comparison.lowest.monthly_amount:
                comparison.lowest = PremiumExtreme(monthly_amount=amount, plans=[identity])
            elif amount == comparison.lowest.monthly_amount:
                comparison.lowest.plans.append(identity)
            if comparison.highest is None or amount > comparison.highest.monthly_amount:
                comparison.highest = PremiumExtreme(monthly_amount=amount, plans=[identity])
            elif amount == comparison.highest.monthly_amount:
                comparison.highest.plans.append(identity)
    return summary


def _document_fallback_instructions(
    plans: list[CanonicalPlanDetails], fallback_types: list[DetailType]
) -> str | None:
    """Require document retrieval only for each plan's permitted missing facts."""
    permitted = set(fallback_types) - {DetailType.PREMIUM}
    scope = [
        {
            "plan_id": plan.plan_id,
            "plan_name": plan.plan_name,
            "detail_types": [kind for kind in plan.missing_detail_types if kind in permitted],
        }
        for plan in plans
        if permitted.intersection(plan.missing_detail_types)
    ]
    if not scope:
        return None
    return (
        "Call search_plans before finalizing the answer to look for the missing non-premium "
        "facts in this scope: "
        + json.dumps(scope, ensure_ascii=False)
        + ". Use each exact plan_id and ask a focused document question about only its listed "
        "detail_types. Do not stop with an unavailable answer until this document search has "
        "been attempted. Preserve all structured facts already returned; do not verify or "
        "replace them with documents. Never search documents for premium amounts. If document "
        "search fails or does not support a missing fact, state that only that fact is unavailable."
    )


def _failure(
    message: str,
    *,
    detail_types: list[DetailType] | None = None,
    selected: list[PlanSummary] | None = None,
    unavailability_reason: str | None = None,
    full_catalog: bool = False,
    market: PlansMarket | None = None,
    error_code: PlanDetailsErrorCode | None = None,
    http_status: int | None = None,
    retryable: bool | None = None,
) -> PlanDetailsResponse:
    """Build a fact-free failure response with the permitted fallback scope."""

    requested = detail_types or []
    selected_plans = selected or []
    fallback_types = (
        []
        if unavailability_reason is not None or full_catalog
        else [detail_type for detail_type in requested if detail_type is not DetailType.PREMIUM]
    )
    return PlanDetailsResponse(
        outcome=(
            PlanDetailsOutcome.UNAVAILABLE
            if unavailability_reason is not None
            else PlanDetailsOutcome.ERROR
        ),
        business_intent=(
            PlanDetailsIntent.SPECIFIC_PLAN
            if len(selected_plans) == 1
            else PlanDetailsIntent.BROAD_PLANS
            if selected_plans
            else None
        ),
        requested_detail_types=requested,
        definitions=_definitions(requested, market),
        plans_searched_for=[plan.plan_name for plan in selected_plans],
        missing_plans=(
            [] if unavailability_reason is not None else [plan.plan_name for plan in selected_plans]
        ),
        fallback_detail_types=fallback_types,
        response_instructions=_document_fallback_instructions(
            [
                CanonicalPlanDetails(
                    plan_id=plan.plan_id,
                    plan_name=plan.plan_name,
                    missing_detail_types=requested,
                )
                for plan in selected_plans
            ],
            fallback_types,
        ),
        required_response=(
            STRUCTURED_PLAN_QUOTE_USER_MESSAGE
            if unavailability_reason is not None
            else PREMIUM_DETAILS_UNAVAILABLE_MESSAGE
            if requested == [DetailType.PREMIUM]
            else FULL_CATALOG_DETAILS_UNAVAILABLE_MESSAGE
            if full_catalog
            else None
        ),
        unavailability_reason=unavailability_reason,
        error_message=None if unavailability_reason is not None else message,
        error_code=error_code,
        http_status=http_status,
        retryable=retryable,
    )


def _http_failure(status_code: int) -> tuple[str, PlanDetailsErrorCode, bool]:
    """Describe an HTTP failure without guessing at the provider's underlying cause."""

    if status_code == 503:
        return (
            "structured plan-details service is temporarily unavailable (HTTP 503)",
            PlanDetailsErrorCode.SERVICE_UNAVAILABLE,
            True,
        )
    if 400 <= status_code < 500:
        return (
            f"structured plan-details service rejected the quote request (HTTP {status_code})",
            PlanDetailsErrorCode.REQUEST_REJECTED,
            status_code in {408, 429},
        )
    return (
        f"structured plan-details service failed (HTTP {status_code})",
        PlanDetailsErrorCode.SERVICE_ERROR,
        status_code >= 500,
    )


@tool(
    name="get_plan_details",
    description=PLAN_DETAILS_DESCRIPTION,
    permission=ToolPermission.READ_ONLY,
    expected_credentials=[ExpectedCredentials(app_id=RAG_APP_ID, type=RAG_CONNECTION_TYPE)],
)
@capture_context_after_completion
async def get_plan_details(
    plan_ids: Annotated[
        list[str],
        Field(description=PLAN_IDS_DESCRIPTION),
    ],
    detail_types: Annotated[
        list[DetailType],
        Field(
            min_length=1,
            max_length=len(DetailType),
            description=DETAIL_TYPES_DESCRIPTION,
        ),
    ],
    context: AgentRun,
    all_available_plans: Annotated[
        bool,
        Field(description=ALL_AVAILABLE_PLANS_DESCRIPTION),
    ] = False,
) -> PlanDetailsResponse:
    """Return authoritative structured facts for selected or all plans in the current quote.

    Args:
        plan_ids: Exact trusted-catalog IDs with no tool count limit; pass an empty list for all.
        detail_types: Structured fact categories needed for the current question.
        context: Agent runtime context injected automatically; never pass it explicitly.
        all_available_plans: True selects every trusted-catalog plan and overrides plan_ids; use
            for an explicit all-plan request or an exhaustive ranking with no narrower plan set.

    Returns:
        An audited response containing canonical plan identities, supported details, and explicit
        non-premium document-fallback categories.
    """

    started = time.perf_counter()
    runtime = cast(AgentRun | None, context)
    if runtime is None or runtime.request_context is None:
        return PlanDetailsResponse(
            outcome=PlanDetailsOutcome.ERROR,
            error_message="agent runtime context is required",
        )
    ctx = cast(Mapping[str, Any], runtime.request_context)
    request_id = normalize_request_id(ctx.get("request_id", ""))
    selected: list[PlanSummary] = []
    requested: list[DetailType] = []
    try:
        control = trusted_plan_control(runtime)
        catalog_value = control.get("plan_lookup")
        if catalog_value is None:
            catalog_value = ctx.get("application_available_plans")
        available = parse_available_plans(catalog_value)
        selected = _selected_plans(
            available,
            plan_ids,
            all_available_plans=all_available_plans,
        )
        requested = _requested_details(detail_types)
    except (TypeError, ValueError) as exc:
        return _finish(
            _failure(
                str(exc),
                detail_types=requested,
                selected=selected,
                full_catalog=all_available_plans,
            ),
            runtime,
        )
    try:
        market, quote = _quote_request(ctx)
    except (TypeError, ValueError):
        return _finish(
            _failure(
                "structured plan quote inputs are unavailable",
                detail_types=requested,
                selected=selected,
                unavailability_reason="structured_quote_unavailable",
                full_catalog=all_available_plans,
            ),
            runtime,
        )

    intent = (
        PlanDetailsIntent.SPECIFIC_PLAN if len(selected) == 1 else PlanDetailsIntent.BROAD_PLANS
    )
    plans_searched_for = [plan.plan_name for plan in selected]
    try:
        connection = await asyncio.to_thread(connections.key_value, RAG_APP_ID)
    except Exception:
        logger.error("structured Plans API connection lookup failed")
        return _finish(
            _failure(
                "structured Plans API connection is unavailable",
                detail_types=requested,
                selected=selected,
                full_catalog=all_available_plans,
                market=market,
                error_code=PlanDetailsErrorCode.CONNECTION_UNAVAILABLE,
                retryable=True,
            ),
            runtime,
        )
    base_url = str(connection.get("RAG_API_BASE_URL", "") or "").strip()
    api_key = str(connection.get("RAG_API_KEY", "") or "").strip()
    endpoint_path = str(connection.get("RAG_PLANS_ENDPOINT", "") or "").strip()
    if not base_url or not api_key or not endpoint_path:
        return _finish(
            _failure(
                "structured Plans API connection is not configured",
                detail_types=requested,
                selected=selected,
                full_catalog=all_available_plans,
                market=market,
                error_code=PlanDetailsErrorCode.CONNECTION_NOT_CONFIGURED,
                retryable=False,
            ),
            runtime,
        )
    try:
        url = build_endpoint_url(base_url, endpoint_path)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-API-Key": api_key,
                },
                json=quote,
                timeout=30,
            )
    except (ValueError, httpx.RequestError):
        return _finish(
            _failure(
                "structured Plans API request failed",
                detail_types=requested,
                selected=selected,
                full_catalog=all_available_plans,
                market=market,
                error_code=PlanDetailsErrorCode.REQUEST_FAILED,
                retryable=True,
            ),
            runtime,
        )
    logger.info(
        "structured_plans_response_received",
        extra={
            "request_id": request_id,
            "downstream_request_id": response.headers.get("X-Request-ID"),
            "status_code": response.status_code,
        },
    )
    if not 200 <= response.status_code < 300:
        message, error_code, retryable = _http_failure(response.status_code)
        return _finish(
            _failure(
                message,
                detail_types=requested,
                selected=selected,
                full_catalog=all_available_plans,
                market=market,
                error_code=error_code,
                http_status=response.status_code,
                retryable=retryable,
            ),
            runtime,
        )
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return _finish(
            _failure(
                (
                    "structured plan-details service returned invalid JSON "
                    f"(HTTP {response.status_code})"
                ),
                detail_types=requested,
                selected=selected,
                full_catalog=all_available_plans,
                market=market,
                error_code=PlanDetailsErrorCode.RESPONSE_INVALID,
                http_status=response.status_code,
                retryable=False,
            ),
            runtime,
        )
    try:
        source = SourcePlansResponse.model_validate(data)
    except ValidationError:
        return _finish(
            _failure(
                "structured Plans API response contract validation failed",
                detail_types=requested,
                selected=selected,
                full_catalog=all_available_plans,
                market=market,
                error_code=PlanDetailsErrorCode.RESPONSE_INVALID,
                http_status=response.status_code,
                retryable=False,
            ),
            runtime,
        )
    if source.market != market or source.plan_count != len(source.plans):
        return _finish(
            _failure(
                "structured Plans API response consistency failed",
                detail_types=requested,
                selected=selected,
                full_catalog=all_available_plans,
                market=market,
                error_code=PlanDetailsErrorCode.RESPONSE_INCONSISTENT,
                http_status=response.status_code,
                retryable=False,
            ),
            runtime,
        )

    state_code = str(ctx.get("user_state_code", "") or "").strip()
    quote_applicants = quote.get("applicants")
    quote_applicant = (
        quote_applicants[0]
        if isinstance(quote_applicants, list) and len(quote_applicants) == 1
        else None
    )
    applicant = quote_applicant if isinstance(quote_applicant, dict) else {}
    incomplete_medsupp_quote = market is PlansMarket.MOLS and any(
        not isinstance(value, str) or not value.strip()
        for value in (
            applicant.get("date_of_birth"),
            applicant.get("gender"),
            quote.get("part_a_eff_date"),
            quote.get("part_b_eff_date"),
        )
    )
    plans: list[CanonicalPlanDetails] = []
    plans_found: list[str] = []
    missing_plans: list[str] = []
    fallback_detail_types: list[DetailType] = []
    medsupp_details_found = False
    premium_details_found = False
    for catalog in selected:
        source_plan = _match_source_plan(catalog, source.plans, state_code, source.market)
        if source_plan is None:
            plans.append(
                CanonicalPlanDetails(
                    plan_id=catalog.plan_id,
                    plan_name=catalog.plan_name,
                    missing_detail_types=requested,
                )
            )
            missing_plans.append(catalog.plan_name)
            for detail_type in requested:
                if (
                    detail_type is not DetailType.PREMIUM
                    and detail_type not in fallback_detail_types
                ):
                    fallback_detail_types.append(detail_type)
            continue
        details: list[DetailResult] = []
        missing: list[DetailType] = []
        for detail_type in requested:
            result = _detail(source_plan, detail_type)
            if result is None:
                missing.append(detail_type)
                if (
                    detail_type is not DetailType.PREMIUM
                    and detail_type not in fallback_detail_types
                ):
                    fallback_detail_types.append(detail_type)
            else:
                details.append(result)
                if detail_type is DetailType.PREMIUM:
                    premium_details_found = True
                if source_plan.coverage_type is MolsCoverageType.MEDICARE_SUPPLEMENT:
                    medsupp_details_found = True
        plans.append(
            CanonicalPlanDetails(
                plan_id=catalog.plan_id,
                plan_name=catalog.plan_name,
                details=details,
                missing_detail_types=missing,
            )
        )
        if details:
            plans_found.append(catalog.plan_name)
        else:
            missing_plans.append(catalog.plan_name)

    coverage_complete = all(not plan.missing_detail_types for plan in plans)
    if all_available_plans:
        fallback_detail_types = []
    if not plans_found:
        outcome = PlanDetailsOutcome.UNAVAILABLE
    elif coverage_complete:
        outcome = PlanDetailsOutcome.COMPLETE
    else:
        outcome = PlanDetailsOutcome.PARTIAL
    instructions = _document_fallback_instructions(plans, fallback_detail_types)
    if len(plans) > 1:
        plan_guidance = (
            "Each item in plans contains one plan's identity and its own benefit details. "
            "Keep every benefit value attached to that item's plan_id and plan_name; never "
            "associate values by position across separate lists. If listing costs, verify "
            "each amount against that same plan's details."
        )
        instructions = " ".join(part for part in (plan_guidance, instructions) if part)
    named_essential_extras = bool(
        re.search(
            r"\bessential[\s-]+extras?\b",
            str(control.get("current_user_query", "")),
            flags=re.IGNORECASE,
        )
    )
    if DetailType.ESSENTIAL_EXTRAS in requested or named_essential_extras:
        guidance = ESSENTIAL_EXTRAS_RESPONSE_INSTRUCTIONS
        if named_essential_extras and DetailType.ESSENTIAL_EXTRAS not in requested:
            guidance += (
                " The current question also asks about Essential Extras. Call get_plan_details "
                "for essential_extras for the same plan scope before answering that part."
            )
        guidance += (
            " This is a full-catalog request: do not search documents. Report missing terms or "
            "relationships as unestablished while retaining supplied facts."
            if all_available_plans
            else " For requested missing terms or relationships, including whether premium affects "
            "the choice limit, call search_plans for that specific question and the same plans "
            "before finalizing. Never search for premium amounts. If documents do not explicitly "
            "establish the relationship, say it is unestablished; do not infer independence."
        )
        instructions = " ".join(part for part in (instructions, guidance) if part)
    if not all_available_plans and any(
        detail_type in requested for detail_type in (DetailType.PRIMARY_CARE, DetailType.SPECIALIST)
    ):
        setting_guidance = (
            "Before answering a question about a service setting or provider arrangement that "
            "the returned visit rows do not explicitly establish, call search_plans for those "
            "missing terms and the same selected plans. An ordinary visit price does not establish "
            "the price in another setting. Retain the supplied visit facts; if the documents do "
            "not establish the requested setting, state that uncertainty."
        )
        instructions = " ".join(part for part in (setting_guidance, instructions) if part)
    elapsed = max(0, int((time.perf_counter() - started) * 1000))
    return _finish(
        PlanDetailsResponse(
            outcome=outcome,
            business_intent=intent,
            requested_detail_types=requested,
            definitions=_definitions(requested, market),
            plans=plans,
            plans_found=plans_found,
            plans_searched_for=plans_searched_for,
            missing_plans=missing_plans,
            coverage_complete=coverage_complete,
            fallback_detail_types=fallback_detail_types,
            response_instructions=instructions,
            premium_summary=_premium_comparison_summary(plans) if premium_details_found else None,
            required_response_addendum=(
                INCOMPLETE_MEDSUPP_QUOTE_ADDENDUM
                if incomplete_medsupp_quote and medsupp_details_found
                else None
            ),
            required_response=(
                FULL_CATALOG_DETAILS_UNAVAILABLE_MESSAGE
                if all_available_plans and outcome is PlanDetailsOutcome.UNAVAILABLE
                else None
            ),
            unavailability_reason=(
                "The current structured quote does not contain the requested details"
                if outcome is PlanDetailsOutcome.UNAVAILABLE
                else None
            ),
            retrieval_time_ms=elapsed,
        ),
        runtime,
    )
