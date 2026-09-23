"""Canonical context mappings for direct WXO and public Shopper API transports."""

from __future__ import annotations

import json
from typing import Any


def _value(value: Any, default: str = "") -> str:
    """Normalize a scalar string and apply the default to missing or blank values."""

    normalized = str(value if value is not None else "").strip()
    return normalized or default


def _plans(value: Any, *, include_visibility: bool) -> list[dict[str, Any]]:
    """Normalize catalog identities for the strict public context contract."""

    if isinstance(value, str):
        value = json.loads(value) if value.strip() else []
    if not isinstance(value, list):
        return []
    plans: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        plan = {
            "plan_id": _value(item.get("plan_id")),
            "plan_name": _value(item.get("plan_name")),
        }
        if include_visibility:
            plan["visible"] = item.get("visible", "")
        plans.append(plan)
    return plans


def _current_plan(value: Any, available: list[dict[str, Any]]) -> dict[str, str]:
    """Resolve an object or unique exact catalog reference to a canonical identity."""

    if isinstance(value, str) and value.strip().startswith("{"):
        value = json.loads(value)
    if isinstance(value, dict):
        return {
            "plan_id": _value(value.get("plan_id")),
            "plan_name": _value(value.get("plan_name")),
        }
    reference = _value(value)
    if reference:
        matches = [plan for plan in available if reference in {plan["plan_id"], plan["plan_name"]}]
        if len(matches) == 1:
            return {"plan_id": matches[0]["plan_id"], "plan_name": matches[0]["plan_name"]}
    return {"plan_id": "", "plan_name": ""}


def context_to_shopper_api(source: dict[str, Any]) -> dict[str, Any]:
    """Map a curated flat context into the strict normalized public contract."""

    available = _plans(source.get("application_available_plans"), include_visibility=True)
    recommended = _plans(source.get("application_recommended_plans"), include_visibility=False)
    dsnp = _value(source.get("user_dsnp_eligibility"))
    if dsnp not in {"", "Partial", "Full"}:
        dsnp = ""
    return {
        "user_context": {
            "brand": _value(source.get("user_brand"), "ABC"),
            "zip_code": _value(source.get("user_zip_code")),
            "county_code": _value(source.get("user_county_code")),
            "county_name": _value(source.get("user_county_name")),
            "state_code": _value(source.get("user_state_code")),
            "requested_eff_date": _value(source.get("user_requested_eff_date"), "2026-07-01"),
            "dsnp_eligibility": dsnp,
            "applicants": source.get("user_applicants") or [],
            "current_plan": _current_plan(source.get("user_current_plan"), available),
            "language": _value(source.get("user_language"), "en"),
            "subsidy_amt": _value(source.get("user_subsidy_amt")),
            "cost_share_reduction": _value(source.get("user_cost_share_reduction")),
            "part_a_eff_date": _value(source.get("user_part_a_eff_date")),
            "part_b_eff_date": _value(source.get("user_part_b_eff_date")),
        },
        "application_context": {
            "exchange_indicator": _value(source.get("application_exchange_indicator")),
            "market_segment": _value(source.get("application_market_segment"), "Medicare"),
            "available_plans": available,
            "recommended_plans": recommended,
            "current_page": _value(source.get("application_current_page"), "view-all-plans"),
        },
        "prospect_context": {"prospect_type": _value(source.get("prospect_type"), "prospect")},
        "business_context": {},
    }


def _individual_quote_inputs(public: dict[str, Any]) -> dict[str, Any] | None:
    """Build an IOLS quote only when all required applicant fields are present."""

    user = public["user_context"]
    if not all(
        (
            user["zip_code"],
            user["county_code"],
            user["county_name"],
            user["applicants"],
        )
    ):
        return None
    applicants: list[dict[str, Any]] = []
    for applicant in user["applicants"]:
        applicant_type = _value(applicant.get("applicant_type"))
        birth_date = _value(applicant.get("date_of_birth"))
        tobacco = _value(applicant.get("is_tobacco_user"))
        applicant_id = _value(applicant.get("applicant_id"))
        if not applicant_type or not birth_date or not tobacco or not applicant_id:
            return None
        item: dict[str, Any] = {
            "applicant_type": applicant_type,
            "date_of_birth": birth_date,
            "is_tobacco_user": tobacco,
            "applicant_id": applicant_id,
        }
        applicants.append(item)
    result: dict[str, Any] = {
        "zip_code": user["zip_code"],
        "county_code": user["county_code"],
        "county_name": user["county_name"],
        "applicants": applicants,
    }
    if user["subsidy_amt"]:
        result["subsidy_amt"] = user["subsidy_amt"]
    if user["cost_share_reduction"]:
        result["cost_share_reduction"] = user["cost_share_reduction"]
    return result


def _medicare_quote_inputs(public: dict[str, Any]) -> dict[str, Any] | None:
    """Build a MOLS quote for one applicant, retaining optional rating fields."""

    user = public["user_context"]
    if (
        not all((user["zip_code"], user["county_code"], user["county_name"]))
        or len(user["applicants"]) != 1
    ):
        return None
    applicant = user["applicants"][0]
    applicant_type = _value(applicant.get("applicant_type"))
    if not applicant_type:
        return None
    quote_applicant: dict[str, Any] = {"applicant_type": applicant_type}
    birth_date = _value(applicant.get("date_of_birth"))
    gender = _value(applicant.get("gender"))
    if birth_date:
        quote_applicant["date_of_birth"] = birth_date
    if gender:
        quote_applicant["gender"] = gender
    result: dict[str, Any] = {
        "zip_code": user["zip_code"],
        "county_code": user["county_code"],
        "county_name": user["county_name"],
        "applicants": [quote_applicant],
    }
    if user["part_a_eff_date"]:
        result["part_a_eff_date"] = user["part_a_eff_date"]
    if user["part_b_eff_date"]:
        result["part_b_eff_date"] = user["part_b_eff_date"]
    return result


def shopper_api_to_wxo(source: dict[str, Any]) -> dict[str, Any]:
    """Translate normalized public context into the agent's minimal runtime variables."""

    application = source["application_context"]
    prospect = source["prospect_context"]
    user = source["user_context"]
    quote_inputs = (
        _individual_quote_inputs(source)
        if application["market_segment"] == "IND"
        else _medicare_quote_inputs(source)
    )
    current = user["current_plan"]
    return {
        "user_brand": user["brand"],
        "user_state_code": user["state_code"],
        "user_requested_eff_date": user["requested_eff_date"],
        "user_current_plan": (
            json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            if current["plan_id"]
            else ""
        ),
        "user_language": user["language"],
        "application_exchange_indicator": application["exchange_indicator"],
        "application_market_segment": application["market_segment"],
        "application_available_plans": json.dumps(
            application["available_plans"], ensure_ascii=False, separators=(",", ":")
        ),
        "application_recommended_plans": json.dumps(
            application["recommended_plans"], ensure_ascii=False, separators=(",", ":")
        ),
        "application_plan_quote_inputs": quote_inputs or {},
        "prospect_type": prospect["prospect_type"],
    }


def context_to_wxo(source: dict[str, Any], history: list[dict[str, str]]) -> dict[str, Any]:
    """Serialize a curated flat context as watsonx Orchestrate variables."""

    context = shopper_api_to_wxo(context_to_shopper_api(source))
    context["previous_queries"] = json.dumps(history, ensure_ascii=False)
    return context
