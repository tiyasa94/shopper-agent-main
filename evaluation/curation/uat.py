"""Build the reviewed 133-turn UAT corpus from raw workbook provenance.

The imported external corpus remains unchanged. This script selects its eligible conversations,
normalizes generated question phrasing, applies reviewed behavioral corrections, and adds explicit
route and plan-ID expectations for current shopper architecture comparisons.
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "tests/evaluation/external_questions.yaml"
OUTPUT = ROOT / "tests/evaluation/uat_questions.yaml"

PLAN_IDS = {
    "Anthem Bronze 60 D EPO": "8XX4",
    "Anthem Bronze 60 D HDHP EPO": "8XWP",
    "Anthem Gold 80 D EPO": "8XW4",
    "Anthem Platinum 90 D EPO": "8XWR",
    "Anthem Silver 70 Off Exchange EPO": "8XWE",
    "Anthem Medicare Advantage (HMO-POS)": "H0544-063-000",
    "Anthem Prime (HMO-POS)": "H4161-007-000",
}

RECOMMENDATION_TURNS = {
    "external_individual_005_turn_01",
    "external_individual_006_turn_01",
    "external_individual_008_turn_01",
    "external_individual_010_turn_01",
    "external_individual_011_turn_01",
    "external_medicare_015_turn_02",
    "external_medicare_015_turn_03",
    "external_medicare_015_turn_04",
    "external_medicare_015_turn_05",
    "external_medicare_016_turn_01",
    "external_medicare_016_turn_02",
    "external_medicare_017_turn_01",
    "external_medicare_046_turn_01",
}

ACTION_TURNS = {
    "external_individual_017_turn_02",
    "external_individual_022_turn_01",
    "external_individual_022_turn_02",
    "external_individual_026_turn_01",
    "external_individual_026_turn_02",
    "external_individual_027_turn_01",
    "external_individual_027_turn_02",
    "external_individual_028_turn_01",
    "external_individual_028_turn_02",
    "external_individual_030_turn_03",
    "external_individual_031_turn_01",
}

PROVIDER_LOOKUP_TURNS = {
    "external_individual_019_turn_03",
    "external_medicare_018_turn_01",
}

CLARIFY_PLAN_TURNS = {
    "external_individual_003_turn_01",
    "external_individual_004_turn_01",
    "external_medicare_002_turn_01",
    "external_medicare_003_turn_01",
    "external_medicare_006_turn_01",
    "external_medicare_015_turn_01",
    "external_medicare_020_turn_01",
    "external_medicare_021_turn_01",
    "external_medicare_022_turn_01",
    "external_medicare_023_turn_01",
    "external_medicare_024_turn_01",
    "external_medicare_025_turn_01",
    "external_medicare_026_turn_01",
    "external_medicare_027_turn_01",
    "external_medicare_029_turn_01",
    "external_medicare_030_turn_01",
    "external_medicare_030_turn_02",
    "external_medicare_032_turn_01",
    "external_medicare_033_turn_01",
    "external_medicare_034_turn_01",
    "external_medicare_035_turn_01",
    "external_medicare_036_turn_01",
    "external_medicare_037_turn_01",
    "external_medicare_039_turn_01",
    "external_medicare_040_turn_01",
}

EXPECTED_PLAN_ID_OVERRIDES = {
    # Preserve a casual-name case without turning this corpus builder into a plan resolver.
    "external_medicare_046_turn_02": ["H4161-007-000"],
}

TURN_OVERRIDES: dict[str, dict[str, Any]] = {
    "external_individual_013_turn_04": {
        "question": "Will my existing plan continue if I do not actively enroll?",
    },
    "external_individual_013_turn_03": {
        "question": "If I miss the annual Open Enrollment deadline, what options might I have?",
    },
    "external_individual_017_turn_01": {
        "question": "Do you offer dental and vision coverage?",
    },
    "external_individual_022_turn_01": {
        "question": "I need to cancel my Anthem Silver 70 Off Exchange EPO plan.",
    },
    "external_individual_027_turn_01": {
        "question": "I need to remove a dependent from my plan.",
    },
    "external_individual_028_turn_01": {
        "question": "I need to add a dependent to my plan.",
    },
    "external_individual_028_turn_02": {
        "question": "How do I add my spouse or child to Anthem Silver 70 Off Exchange EPO?",
        "source_rag_expected": False,
    },
    "external_individual_004_turn_01": {
        "reference_answer": (
            "Ask which plan or plans the shopper wants to compare for inpatient hospital costs "
            "and coverage; do not assume the recommended plans."
        ),
        "source_rag_expected": False,
    },
    "external_individual_005_turn_01": {
        "reference_answer": (
            "Do not choose a plan. Explain that drug fit depends on the shopper's medications, "
            "tiers, pharmacy, and plan rules, and offer an objective comparison."
        ),
        "source_rag_expected": False,
    },
    "external_individual_006_turn_02": {
        "question": "Explain a health insurance deductible in simple terms.",
    },
    "external_individual_005_turn_02": {
        "question": (
            "Compare prescription drug cost-sharing for Anthem Gold 80 D EPO and Anthem "
            "Platinum 90 D EPO."
        ),
        "reference_answer": (
            "Compare only current documented prescription cost-sharing for the named plans, "
            "keeping drug tiers and deductible conditions separate."
        ),
        "source_intent": "broad_plans",
        "source_rag_expected": True,
    },
    "external_individual_006_turn_01": {
        "reference_answer": (
            "Do not decide whether Platinum is worth it. Offer to compare objective costs and "
            "benefits, and direct the shopper to a licensed agent for a recommendation."
        ),
        "source_rag_expected": False,
    },
    "external_individual_006_turn_03": {
        "source_intent": "generic_info",
        "reference_answer": (
            "Explain that plan changes generally occur during Open Enrollment or a qualifying "
            "Special Enrollment Period; do not initiate a change or recommend a replacement."
        ),
    },
    "external_individual_007_turn_01": {"source_rag_expected": True},
    "external_individual_007_turn_02": {
        "question": (
            "Could my current plan's deductible or cost-sharing rules be part of the reason?"
        ),
        "reference_answer": (
            "Explain relevant documented rules for the current plan without claiming why the "
            "shopper's actual bill occurred, then direct them to member services for the "
            "personal explanation."
        ),
        "source_rag_expected": True,
    },
    "external_individual_008_turn_01": {"source_rag_expected": False},
    "external_individual_010_turn_01": {"source_rag_expected": False},
    "external_individual_011_turn_01": {"source_rag_expected": False},
    "external_individual_014_turn_01": {
        "reference_answer": (
            "Explain that an HMO generally centers on network care and may require a PCP or "
            "specialist referrals; qualify the answer because exact rules vary by plan."
        )
    },
    "external_individual_014_turn_02": {
        "question": "What is a health insurance premium?",
        "reference_answer": (
            "Explain that a premium is the recurring amount paid to keep coverage active."
        ),
        "source_intent": "generic_info",
        "source_rag_expected": True,
    },
    "external_individual_014_turn_03": {
        "question": "What is the monthly premium for my current plan?",
        "reference_answer": (
            "Use the supplied current plan and report a premium only if responsive current plan "
            "evidence establishes it; otherwise explain where to verify the exact amount."
        ),
        "source_rag_expected": True,
    },
    "external_individual_016_turn_02": {
        "question": "Does my current plan cover routine vision benefits?",
        "reference_answer": (
            "Use the supplied current plan and answer only from responsive vision-benefit evidence."
        ),
        "source_rag_expected": True,
    },
    "external_individual_016_turn_03": {
        "question": "Does it include an eyewear allowance too?",
        "reference_answer": (
            "Carry forward the current plan and distinguish routine exams from any documented "
            "eyewear allowance."
        ),
        "source_rag_expected": True,
    },
    "external_individual_017_turn_02": {
        "question": "How do I add dental and vision coverage to my current plan?",
        "reference_answer": (
            "Do not claim the current medical plan can be modified. Direct the shopper to the "
            "appropriate enrollment or licensed-agent channel for available ancillary coverage."
        ),
        "source_intent": "generic_info",
        "source_rag_expected": False,
    },
    "external_individual_018_turn_01": {"source_rag_expected": True},
    "external_individual_018_turn_02": {
        "question": "What about non-emergency care while I am outside the country?",
        "reference_answer": (
            "Carry forward the current plan and distinguish documented emergency travel coverage "
            "from routine or comprehensive international medical coverage."
        ),
        "source_rag_expected": True,
    },
    "external_individual_019_turn_04": {
        "question": "Does my current plan require me to use in-network hospitals?",
        "reference_answer": (
            "Treat this as a general network-rule question for the current plan, not a lookup for "
            "a particular hospital."
        ),
        "source_rag_expected": True,
    },
    "external_individual_019_turn_02": {
        "question": "Does using tobacco or nicotine products change my rate?",
    },
    "external_individual_019_turn_03": {
        "question": "Are my doctor and hospital in this plan's network?",
    },
    "external_individual_020_turn_01": {"source_rag_expected": True},
    "external_individual_020_turn_02": {
        "question": "What documented factors can affect the cost of my current plan?",
        "reference_answer": (
            "Provide only documented general rating or renewal factors and do not claim why this "
            "shopper's actual rate changed."
        ),
        "source_rag_expected": True,
    },
    "external_individual_021_turn_02": {
        "question": "Does my current plan cover weight-loss medications?",
        "source_rag_expected": True,
    },
    "external_individual_021_turn_03": {
        "question": "How can I check whether my current plan covers Wegovy?",
        "reference_answer": (
            "Explain the documented formulary or drug-lookup steps without asserting coverage for "
            "Wegovy unless the retrieved evidence explicitly establishes it."
        ),
        "source_rag_expected": True,
    },
    "external_individual_026_turn_02": {"source_rag_expected": False},
    "external_individual_027_turn_02": {
        "question": "How do I remove my spouse or child from Anthem Silver 70 Off Exchange EPO?",
        "source_rag_expected": False,
    },
    "external_individual_030_turn_02": {"source_rag_expected": True},
    "external_individual_030_turn_03": {
        "question": "I need to change my plan so it covers my medications.",
    },
    "external_individual_030_turn_04": {
        "question": (
            "Compare Tier 3 prescription cost-sharing for Anthem Gold 80 D EPO and Anthem "
            "Platinum 90 D EPO."
        ),
        "reference_answer": (
            "Compare only responsive Tier 3 cost-sharing evidence for the two named plans; "
            "preserve "
            "deductible, pharmacy, and supply-duration qualifications."
        ),
        "source_intent": "broad_plans",
        "source_rag_expected": True,
    },
    "external_individual_031_turn_02": {
        "question": "What travel coverage does my current plan already include?",
        "reference_answer": (
            "Use the supplied current plan and distinguish documented emergency travel coverage "
            "from comprehensive international insurance."
        ),
        "source_rag_expected": True,
    },
    "external_individual_031_turn_01": {
        "question": "I need to add international medical coverage to my current plan.",
    },
    "external_individual_032_turn_01": {
        "question": "Does my current plan cover transportation or grocery benefits?",
        "reference_answer": (
            "Use the supplied current plan and report only transportation or grocery benefits "
            "explicitly established by current evidence."
        ),
        "source_rag_expected": True,
    },
    "external_individual_032_turn_02": {
        "question": "Are there separate community programs that help with food or transportation?",
        "reference_answer": (
            "Search general Individual-market documents for documented community-support programs "
            "and explain eligibility or next steps without promising enrollment."
        ),
        "source_intent": "generic_info",
        "source_rag_expected": True,
    },
    "external_medicare_009_turn_01": {"source_rag_expected": True},
    "external_medicare_009_turn_02": {
        "question": "What is my current plan's 2026 out-of-pocket maximum?",
        "reference_answer": (
            "Report the current plan's documented 2026 medical out-of-pocket maximum and do not "
            "claim whether it changed without prior-year evidence."
        ),
        "source_rag_expected": True,
    },
    "external_medicare_012_turn_01": {
        "question": "I am turning 65 in August. When should I enroll in Medicare?",
    },
    "external_medicare_012_turn_02": {
        "question": "What if I still have employer coverage when I turn 65?",
        "reference_answer": (
            "Explain that Medicare enrollment timing can depend on the employer coverage and "
            "employment situation, and direct the shopper to official Medicare guidance for a "
            "personal decision."
        ),
        "source_intent": "generic_info",
        "source_rag_expected": True,
    },
    "external_medicare_014_turn_02": {
        "question": (
            "Compare specialist visit costs for Anthem Prime (HMO-POS) and Anthem Medicare "
            "Advantage (HMO-POS)."
        ),
        "reference_answer": (
            "Compare current specialist-visit cost evidence for exactly the two named plans and "
            "attribute each amount separately."
        ),
        "source_intent": "broad_plans",
        "source_rag_expected": True,
    },
    "external_medicare_014_turn_03": {
        "question": "Are emergency services generally covered by Medicare Advantage plans?",
        "reference_answer": (
            "Give plan-independent Medicare Advantage emergency-coverage education, while noting "
            "that exact cost-sharing and rules vary by plan."
        ),
        "source_intent": "generic_info",
        "source_rag_expected": True,
    },
    "external_medicare_014_turn_04": {
        "question": (
            "Compare routine vision and hearing benefits for Anthem Prime (HMO-POS) and Anthem "
            "Medicare Advantage (HMO-POS)."
        ),
        "reference_answer": (
            "Compare current vision and hearing evidence for exactly the named pair, preserving "
            "base-versus-optional applicability and plan attribution."
        ),
        "source_intent": "broad_plans",
        "source_rag_expected": True,
    },
    "external_medicare_015_turn_01": {
        "reference_answer": (
            "Ask which plan or plans the shopper wants to compare for meal or fitness benefits; "
            "do not assume recommended plans or enumerate the full catalog."
        ),
        "source_rag_expected": False,
    },
    "external_medicare_015_turn_02": {"source_rag_expected": False},
    "external_medicare_015_turn_03": {"source_rag_expected": False},
    "external_medicare_015_turn_04": {"source_rag_expected": False},
    "external_medicare_015_turn_05": {"source_rag_expected": False},
    "external_medicare_016_turn_01": {"source_rag_expected": False},
    "external_medicare_016_turn_02": {
        "source_rag_expected": False,
        "reference_answer": (
            "Decline the Medicaid-related request with the fixed Medicaid response."
        ),
    },
    "external_medicare_017_turn_01": {"source_rag_expected": False},
    "external_medicare_017_turn_02": {
        "question": "What factors should I compare before choosing a Medicare plan?",
        "reference_answer": (
            "Explain objective comparison factors such as provider access, prescriptions, total "
            "costs, coverage rules, and relevant extra benefits without choosing a plan."
        ),
        "source_intent": "generic_info",
        "source_rag_expected": True,
    },
    "external_medicare_018_turn_01": {
        "reference_answer": (
            "Explain how to use the provider lookup to verify the particular PCP; do not claim "
            "network participation from plan documents."
        ),
        "source_rag_expected": False,
    },
    "external_medicare_018_turn_02": {
        "question": "Does Anthem Prime (HMO-POS) generally require in-network doctors?",
        "reference_answer": (
            "Answer the named plan's general network rules from current plan evidence; do not turn "
            "this into a lookup for a particular provider."
        ),
        "source_rag_expected": True,
    },
    "external_medicare_026_turn_02": {"source_rag_expected": True},
    "external_medicare_028_turn_01": {"source_rag_expected": True},
    "external_medicare_030_turn_01": {
        "reference_answer": (
            "Ask which plan the shopper means before discussing a plan-specific dental allowance; "
            "do not assume Anthem Prime."
        )
    },
    "external_medicare_031_turn_02": {
        "source_rag_expected": True,
        "reference_answer": (
            "Carry forward Anthem Prime from the prior turn and answer only from responsive "
            "hearing-aid benefit evidence."
        ),
    },
    "external_medicare_033_turn_02": {"source_rag_expected": True},
    "external_medicare_041_turn_01": {
        "source_intent": "generic_info",
        "source_rag_expected": True,
    },
    "external_medicare_042_turn_01": {
        "source_intent": "generic_info",
        "source_rag_expected": True,
    },
    "external_medicare_043_turn_01": {
        "source_intent": "generic_info",
        "source_rag_expected": True,
    },
    "external_medicare_043_turn_02": {
        "question": (
            "For Anthem Prime (HMO-POS), can I change plans later if it does not work for me?"
        ),
        "reference_answer": (
            "Keep this as general Medicare enrollment-window education even though a plan is "
            "named; "
            "do not search plan benefits."
        ),
        "source_intent": "generic_info",
        "source_rag_expected": True,
    },
    "external_medicare_046_turn_01": {"source_rag_expected": False},
    "external_medicare_046_turn_02": {
        "question": "What are Anthem Prime's monthly premium and medical out-of-pocket maximum?",
        "reference_answer": (
            "Report only current evidence for Anthem Prime's monthly plan premium and medical "
            "out-of-pocket maximum, keeping Part B and Part D amounts separate."
        ),
        "source_rag_expected": True,
    },
    "external_medicare_048_turn_01": {
        "reference_answer": (
            "Answer only if responsive Anthem Prime evidence explicitly establishes a Part B "
            "premium reduction. A statement that Part B must still be paid does not prove either "
            "presence or absence."
        )
    },
    "external_medicare_048_turn_02": {
        "reference_answer": (
            "Carry forward Anthem Prime and answer only if responsive evidence explicitly "
            "establishes a Part B premium reduction."
        ),
        "source_rag_expected": True,
    },
    "external_medicare_049_turn_01": {
        "reference_answer": (
            "Use the supplied current plan and answer only from current vision-benefit evidence; "
            "surface conflicts or optional-package ambiguity."
        ),
        "source_rag_expected": True,
    },
    "external_medicare_049_turn_02": {
        "question": "What about its eyewear allowance?",
        "reference_answer": (
            "Carry forward the current plan and report an eyewear allowance only when current "
            "evidence resolves base-versus-optional applicability and conflicts."
        ),
        "source_rag_expected": True,
    },
}


def _naturalize_named_plan_question(question: str) -> str:
    """Replace generated `... with plan X?` phrasing with one natural named-plan question."""

    match = re.fullmatch(r"(.+?) with plan (.+?)\?", question.strip())
    if not match:
        return question.strip()
    base, plan_name = match.groups()
    base = re.sub(r"\s+with this plan$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"\bthis plan\b", "the plan", base, flags=re.IGNORECASE)
    base = re.sub(r"\bmy current plan\b", "the plan", base, flags=re.IGNORECASE)
    base = re.sub(r"\bmy plan\b", "the plan", base, flags=re.IGNORECASE)
    base = base.strip().rstrip("?.")
    return f"For {plan_name}, {base[:1].lower()}{base[1:]}?"


def _named_plan_ids(question: str) -> list[str]:
    return [plan_id for name, plan_id in PLAN_IDS.items() if name in question]


def _current_plan_id(context: dict[str, Any]) -> str | None:
    current = context.get("user_current_plan")
    return (
        str(current.get("plan_id"))
        if isinstance(current, dict) and current.get("plan_id")
        else None
    )


def _route(turn_id: str, turn: dict[str, Any]) -> str:
    if turn_id in RECOMMENDATION_TURNS | ACTION_TURNS | PROVIDER_LOOKUP_TURNS:
        return "guardrail"
    if turn_id in CLARIFY_PLAN_TURNS:
        return "clarify_plan"
    if turn["source_intent"] == "generic_info":
        return "general_search"
    return "plan_search" if turn["source_rag_expected"] else "clarify_plan"


def _expected_behavior(turn_id: str, route: str) -> str:
    if turn_id == "external_medicare_016_turn_02":
        return "Return the fixed Medicaid refusal without choosing a plan or giving Medicaid facts."
    if turn_id in RECOMMENDATION_TURNS:
        return (
            "Do not choose or rank a plan; offer neutral comparison help and an appropriate agent "
            "next step."
        )
    if turn_id in ACTION_TURNS:
        return (
            "Do not perform or imply completion of the personal coverage change; provide the "
            "approved next step."
        )
    if turn_id in PROVIDER_LOOKUP_TURNS:
        return (
            "Do not claim current provider participation; direct the shopper to the provider "
            "lookup channel."
        )
    if route == "clarify_plan":
        return (
            "Ask which plan or plans the shopper means without assuming recommendations or "
            "listing the full catalog."
        )
    if route == "general_search":
        return "Use the market-specific general corpus and answer only from responsive evidence."
    return (
        "Search exactly the resolved allowed plan set and answer only from responsive "
        "plan-attributed evidence."
    )


def build() -> dict[str, Any]:
    raw = yaml.safe_load(SOURCE.read_text())
    contexts = copy.deepcopy(raw["contexts"])
    for context in contexts.values():
        if context["application_market_segment"] == "IND":
            context["user_county_code"] = str(context["user_county_code"])[-3:]
    conversations: list[dict[str, Any]] = []

    for source_conversation in raw["conversations"]:
        if source_conversation["evaluation_status"] != "eligible":
            continue
        conversation = copy.deepcopy(source_conversation)
        if conversation["id"] == "external_medicare_012":
            conversation["context"] = "medicare_prospect"
        conversation["evaluation_status"] = "eligible"
        prior_plan_ids: list[str] = []
        for turn in conversation["turns"]:
            turn_id = turn["id"]
            turn.update(copy.deepcopy(TURN_OVERRIDES.get(turn_id, {})))
            turn["question"] = _naturalize_named_plan_question(turn["question"])
            turn["evaluation_status"] = "eligible"
            route = _route(turn_id, turn)
            turn["expected_route"] = route
            turn["expected_behavior"] = _expected_behavior(turn_id, route)

            plan_ids = EXPECTED_PLAN_ID_OVERRIDES.get(turn_id, _named_plan_ids(turn["question"]))
            if route == "plan_search" and not plan_ids:
                current_id = _current_plan_id(contexts[conversation["context"]])
                if current_id and re.search(r"\b(?:my|current|it|its)\b", turn["question"], re.I):
                    plan_ids = [current_id]
                elif prior_plan_ids:
                    plan_ids = prior_plan_ids
            turn["expected_plan_ids"] = plan_ids if route == "plan_search" else []
            if route == "plan_search" and plan_ids:
                prior_plan_ids = plan_ids

            if route == "clarify_plan":
                turn["reference_answer"] = (
                    "Ask which plan or plans the shopper means without assuming a recommended plan "
                    "or reproducing the full allowed-plan catalog."
                )
                turn["source_rag_expected"] = False
            elif route == "guardrail":
                turn["source_rag_expected"] = False
            elif route in {"general_search", "plan_search"}:
                turn["source_rag_expected"] = True
        conversations.append(conversation)

    turns = [turn for conversation in conversations for turn in conversation["turns"]]
    if len(conversations) != 62 or len(turns) != 133:
        raise ValueError("reviewed UAT corpus must remain exactly 62 conversations and 133 turns")
    questions = [turn["question"].casefold().strip() for turn in turns]
    if len(questions) != len(set(questions)):
        duplicates = [question for question, count in Counter(questions).items() if count > 1]
        raise ValueError(f"reviewed UAT questions must be unique: {duplicates}")

    expected_routes = {"general_search", "plan_search", "clarify_plan", "guardrail"}
    if {turn["expected_route"] for turn in turns} != expected_routes:
        raise ValueError("reviewed UAT corpus must exercise every supported route")
    for conversation in conversations:
        allowed_ids = {
            str(plan["plan_id"])
            for plan in contexts[conversation["context"]]["application_available_plans"]
        }
        for turn in conversation["turns"]:
            route = turn["expected_route"]
            plan_ids = set(turn["expected_plan_ids"])
            if route == "plan_search" and not plan_ids:
                raise ValueError(f"plan-search turn lacks reviewed plan IDs: {turn['id']}")
            if not plan_ids <= allowed_ids:
                raise ValueError(f"turn expects a plan outside its context allowlist: {turn['id']}")
            if turn["source_rag_expected"] != (route in {"general_search", "plan_search"}):
                raise ValueError(f"retrieval expectation conflicts with route: {turn['id']}")

    forbidden_fragments = {
        "<Member accessing",
        " with plan ",
        "I CareMore",
        "Anthem Select",
        "dependent/spouse/child",
        "coverage/insurance",
    }
    artifacts = [
        turn["id"]
        for turn in turns
        if any(
            fragment.casefold() in turn["question"].casefold() for fragment in forbidden_fragments
        )
    ]
    if artifacts:
        raise ValueError(f"reviewed UAT questions retain known import artifacts: {artifacts}")

    return {
        "schema_version": 1,
        "name": "shopper-uat-reviewed-133",
        "source": {
            "corpus": str(SOURCE.relative_to(ROOT)),
            "file": raw["source"]["file"],
            "sha256": raw["source"]["sha256"],
            "policy": (
                "Source IDs and workbook coordinates are preserved; eligible question wording, "
                "references, contexts, and expectations are manually reviewed overrides."
            ),
        },
        "curation": {
            "answer_policy": "reference_answer is a review aid and never an exact-response oracle",
            "routing_policy": (
                "expected_route, expected_plan_ids, and source_rag_expected are reviewed current "
                "targets"
            ),
            "selection_policy": (
                "the 62 source conversations marked eligible yield exactly 133 turns"
            ),
        },
        "statistics": {
            "conversations": len(conversations),
            "turns": len(turns),
            "contexts": len(contexts),
            "turns_by_market": dict(
                Counter(
                    conversation["market"]
                    for conversation in conversations
                    for _ in conversation["turns"]
                )
            ),
            "multi_turn_conversations": sum(
                len(conversation["turns"]) > 1 for conversation in conversations
            ),
        },
        "contexts": contexts,
        "conversations": conversations,
    }


def main() -> int:
    OUTPUT.write_text(yaml.safe_dump(build(), sort_keys=False, allow_unicode=True, width=110))
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
