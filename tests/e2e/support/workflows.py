"""Minimal workflows that prove the local shopper stack is connected."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from evaluation.behavioral import load_corpus

ROOT = Path(__file__).resolve().parents[3]
CORPUS = load_corpus(ROOT / "tests/evaluation/behavioral_questions.yaml")
CONTEXTS: dict[str, dict] = CORPUS["contexts"]


class Action(StrEnum):
    """Cross-service action that a workflow must expose in public metadata."""

    TERMINAL = "terminal"
    GENERAL_SEARCH = "general_search"
    PLAN_SEARCH = "plan_search"
    PLAN_DETAILS = "plan_details"


@dataclass(frozen=True, slots=True)
class Turn:
    """One request and the durable integration facts expected from it."""

    query: str
    action: Action
    plan_ids: tuple[str, ...] = ()
    plan_names: tuple[str, ...] = ()
    detail_types: tuple[str, ...] = ()
    all_available_plans: bool = False


@dataclass(frozen=True, slots=True)
class Workflow:
    """One isolated API session containing one or more functional turns."""

    id: str
    context: str
    turns: tuple[Turn, ...]
    smoke: bool = False


IOLS_BRONZE = (
    "Anthem Bronze Pathway Transition 7300 ($0 Virtual PCP + $0 Select Drugs + Incentives)"
)
MOLS_ADVANTAGE = "Anthem Medicare Advantage (PPO)"
MOLS_ADVANTAGE_3 = "Anthem Medicare Advantage 3 (PPO)"
ANTHEM_PRIME = "Anthem Prime (HMO-POS)"
CURRENT_MEDICARE_PLAN = "Anthem Medicare Advantage (HMO-POS)"
IOLS_CATALOG = tuple(CONTEXTS["plans_api_iols_minimum"]["application_available_plans"])
IOLS_PLAN_IDS = tuple(plan["plan_id"] for plan in IOLS_CATALOG)
IOLS_PLAN_NAMES = tuple(plan["plan_name"] for plan in IOLS_CATALOG)


WORKFLOWS = (
    Workflow(
        id="api_round_trip",
        context="medicare_prospect",
        turns=(Turn(query="Hello", action=Action.TERMINAL),),
        smoke=True,
    ),
    Workflow(
        id="general_document_search",
        context="medicare_prospect",
        turns=(Turn(query="What is a copay?", action=Action.GENERAL_SEARCH),),
        smoke=True,
    ),
    Workflow(
        id="mols_structured_details",
        context="plans_api_mols_minimum",
        turns=(
            Turn(
                query=f"What is the monthly premium for {MOLS_ADVANTAGE}?",
                action=Action.PLAN_DETAILS,
                plan_ids=("H4036-026-000",),
                plan_names=(MOLS_ADVANTAGE,),
                detail_types=("premium",),
            ),
        ),
        smoke=True,
    ),
    Workflow(
        id="named_plan_document_search",
        context="medicare_prospect",
        turns=(
            Turn(
                query=f"What dental benefits does {ANTHEM_PRIME} include?",
                action=Action.PLAN_SEARCH,
                plan_ids=("H4161-007-000",),
                plan_names=(ANTHEM_PRIME,),
            ),
        ),
    ),
    Workflow(
        id="current_plan_document_search",
        context="medicare_member_current_plan",
        turns=(
            Turn(
                query="What dental benefits does my current plan include?",
                action=Action.PLAN_SEARCH,
                plan_ids=("H0544-063-000",),
                plan_names=(CURRENT_MEDICARE_PLAN,),
            ),
        ),
    ),
    Workflow(
        id="iols_structured_details",
        context="plans_api_iols_minimum",
        turns=(
            Turn(
                query=f"What is the monthly premium for {IOLS_BRONZE}?",
                action=Action.PLAN_DETAILS,
                plan_ids=("8YC0",),
                plan_names=(IOLS_BRONZE,),
                detail_types=("premium",),
            ),
        ),
    ),
    Workflow(
        id="iols_full_catalog_structured_details",
        context="plans_api_iols_minimum",
        turns=(
            Turn(
                query="Which plan has the lowest medical deductible?",
                action=Action.PLAN_DETAILS,
                plan_ids=IOLS_PLAN_IDS,
                plan_names=IOLS_PLAN_NAMES,
                detail_types=("medical_deductible",),
                all_available_plans=True,
            ),
            Turn(
                query="How much could I pay in a year, max, for these plans?",
                action=Action.PLAN_DETAILS,
                plan_ids=IOLS_PLAN_IDS,
                plan_names=IOLS_PLAN_NAMES,
                detail_types=("out_of_pocket_maximum",),
                all_available_plans=True,
            ),
        ),
    ),
    Workflow(
        id="current_enrolled_plan_premium",
        context="plans_api_mols_minimum_current",
        turns=(
            Turn(
                query="What is my monthly premium?",
                action=Action.PLAN_DETAILS,
                plan_ids=("H4036-026-000",),
                plan_names=(MOLS_ADVANTAGE,),
                detail_types=("premium",),
            ),
        ),
    ),
    Workflow(
        id="conversation_selected_plan_premium",
        context="plans_api_mols_minimum",
        turns=(
            Turn(
                query=f"What is the medical deductible for {MOLS_ADVANTAGE_3}?",
                action=Action.PLAN_DETAILS,
                plan_ids=("H4036-025-000",),
                plan_names=(MOLS_ADVANTAGE_3,),
                detail_types=("medical_deductible",),
            ),
            Turn(
                query="What is the monthly premium?",
                action=Action.PLAN_DETAILS,
                plan_ids=("H4036-025-000",),
                plan_names=(MOLS_ADVANTAGE_3,),
                detail_types=("premium",),
            ),
        ),
    ),
    Workflow(
        id="multiturn_plan_continuity",
        context="medicare_prospect",
        turns=(
            Turn(
                query=f"What dental benefits does {ANTHEM_PRIME} include?",
                action=Action.PLAN_SEARCH,
                plan_ids=("H4161-007-000",),
                plan_names=(ANTHEM_PRIME,),
            ),
            Turn(
                query="And what about hearing aids?",
                action=Action.PLAN_SEARCH,
                plan_ids=("H4161-007-000",),
                plan_names=(ANTHEM_PRIME,),
            ),
        ),
    ),
)


def select_workflows(profile: str) -> tuple[Workflow, ...]:
    """Return the smoke subset or the complete functional workflow set."""

    if profile == "smoke":
        return tuple(workflow for workflow in WORKFLOWS if workflow.smoke)
    if profile == "full":
        return WORKFLOWS
    raise ValueError("E2E_WORKFLOW_SET must be 'smoke' or 'full'")
