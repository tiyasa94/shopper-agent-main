"""Quality and integrity checks for the curated behavioral corpus."""

from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from pathlib import Path

import yaml
from evaluation.contexts import context_to_shopper_api, shopper_api_to_wxo

from evaluation import behavioral as agent_comparison
from shared.classifier import CLASSIFIER_PROMPT_TEMPLATE

ROOT = Path(__file__).parents[2]
CORPUS_PATH = Path(__file__).with_name("behavioral_questions.yaml")
RAW_SOURCE_PATH = Path(__file__).with_name("external_questions.yaml")
CORPUS = yaml.safe_load(CORPUS_PATH.read_text())


def _turns() -> list[tuple[dict, dict]]:
    return [
        (conversation, turn)
        for conversation in CORPUS["conversations"]
        for turn in conversation["turns"]
    ]


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _quote_inputs(context: dict) -> dict:
    return shopper_api_to_wxo(context_to_shopper_api(context))["application_plan_quote_inputs"]


def test_behavioral_corpus_is_the_default_and_preserves_raw_source() -> None:
    assert agent_comparison.DEFAULT_CORPUS == CORPUS_PATH
    assert RAW_SOURCE_PATH.is_file()


def test_behavioral_corpus_statistics_match_content() -> None:
    conversations = CORPUS["conversations"]
    turns = _turns()
    stats = CORPUS["statistics"]

    assert stats == {
        "conversations": len(conversations),
        "turns": len(turns),
        "contexts": len(CORPUS["contexts"]),
        "turns_by_market": dict(Counter(item[0]["market"] for item in turns)),
        "multi_turn_conversations": sum(
            len(conversation["turns"]) > 1 for conversation in conversations
        ),
    }


def test_questions_and_references_have_no_known_import_artifacts() -> None:
    """Reject dangling import placeholders while allowing named-plan wording."""
    forbidden = (
        re.compile(r"\bwith plan\s*(?:[?.!]|$)", re.IGNORECASE),
        re.compile(r"<\s*member accessing", re.IGNORECASE),
        re.compile(r"\bfull plan name\b", re.IGNORECASE),
        re.compile(r"\bou licensed\b|\byor question\b|cancellaiton", re.IGNORECASE),
    )
    for _, turn in _turns():
        text = f"{turn['question']} {turn['reference_answer']}"
        assert all(not pattern.search(text) for pattern in forbidden), turn["id"]


def test_questions_are_unique_and_all_review_fields_are_present() -> None:
    conversations = CORPUS["conversations"]
    turns = _turns()
    conversation_ids = [conversation["id"] for conversation in conversations]
    turn_ids = [turn["id"] for _, turn in turns]
    questions = [
        (conversation["context"], _normalized(turn["question"])) for conversation, turn in turns
    ]

    assert len(conversation_ids) == len(set(conversation_ids))
    assert len(turn_ids) == len(set(turn_ids))
    assert len(questions) == len(set(questions))
    assert all(conversation["evaluation_status"] == "eligible" for conversation in conversations)
    for _, turn in turns:
        assert turn["question"].strip()
        assert turn["reference_answer"].strip()
        assert turn["expected_behavior"].strip()
        assert turn["source_intent"] in {"generic_info", "specific_plan", "broad_plans"}
        assert isinstance(turn["source_rag_expected"], bool)
        assert turn["expected_route"] in {
            "clarify_benefit",
            "clarify_plan",
            "general_search",
            "guardrail",
            "plan_details",
            "plan_details_and_plan_search",
            "plan_search",
        }
        assert isinstance(turn["expected_plan_ids"], list)
        assert isinstance(turn.get("expected_detail_types", []), list)
        assert set(turn.get("expected_detail_types", [])) <= {
            "premium",
            "medical_deductible",
            "pharmacy_deductible",
            "out_of_pocket_maximum",
            "primary_care",
            "specialist",
            "urgent_care",
            "emergency_room",
            "plan_options",
            "drug_tiers",
            "essential_extras",
        }


def test_behavioral_questions_and_reference_answers_do_not_leak_into_prompts() -> None:
    agent = yaml.safe_load((ROOT / "src/agent.yaml").read_text())
    prompt = _normalized(CLASSIFIER_PROMPT_TEMPLATE + " " + yaml.safe_dump(agent, sort_keys=False))

    for _, turn in _turns():
        question = _normalized(turn["question"])
        if len(question.split()) >= 3:
            assert question not in prompt, turn["id"]
        answer_words = _normalized(turn["reference_answer"]).split()
        for index in range(max(0, len(answer_words) - 11)):
            assert " ".join(answer_words[index : index + 12]) not in prompt, turn["id"]


def test_expected_routes_and_plan_ids_are_coherent() -> None:
    for conversation, turn in _turns():
        plan_ids = set(turn["expected_plan_ids"])
        available_ids = {
            plan["plan_id"]
            for plan in CORPUS["contexts"][conversation["context"]]["application_available_plans"]
        }
        assert plan_ids <= available_ids, turn["id"]
        assert turn["source_rag_expected"] is (
            turn["expected_route"]
            in {"general_search", "plan_search", "plan_details_and_plan_search"}
        )
        assert (bool(plan_ids) or turn.get("expected_all_available_plans", False)) is (
            turn["expected_route"]
            in {"plan_search", "plan_details", "plan_details_and_plan_search"}
        )
        assert bool(turn.get("expected_detail_types")) is (
            turn["expected_route"] in {"plan_details", "plan_details_and_plan_search"}
        )


def test_multi_plan_intents_and_caremore_route_match_the_structured_contract() -> None:
    turns = {turn["id"]: turn for _, turn in _turns()}

    assert turns["rc4_medigap_shared_booklet_01"]["source_intent"] == "broad_plans"

    caremore = turns["rc5_partial_match_caremore_comparison_01"]
    assert caremore["source_rag_expected"] is False
    assert caremore["expected_route"] == "plan_details"
    assert caremore["expected_plan_ids"] == ["H0544-058-000", "H4161-012-000"]
    assert caremore["expected_detail_types"]


def test_contexts_match_the_preserved_source_examples() -> None:
    source = yaml.safe_load(RAW_SOURCE_PATH.read_text())
    for name, context in source["contexts"].items():
        enriched = CORPUS["contexts"][name]
        for key, value in context.items():
            if key != "user_applicant_count":
                if key == "user_county_code" and context["application_market_segment"] == "IND":
                    value = str(value)[-3:]
                assert enriched[key] == value
        assert len(enriched["user_applicants"]) == context["user_applicant_count"]
        assert enriched["user_county_name"]
    assert (
        CORPUS["contexts"]["uat_individual_prospect_primary_care"]["application_current_page"]
        == "medical-plans"
    )


def test_every_context_supplies_valid_minimum_plan_quote_inputs() -> None:
    for name, context in CORPUS["contexts"].items():
        quote = _quote_inputs(context)
        assert quote, name
        assert all(quote[field] for field in ("zip_code", "county_code", "county_name")), name
        if context["application_market_segment"] == "IND":
            assert len(quote["county_code"]) == 3, name
            assert 1 <= len(quote["applicants"]) <= 20, name
            for applicant in quote["applicants"]:
                assert all(
                    applicant[field]
                    for field in (
                        "applicant_type",
                        "date_of_birth",
                        "is_tobacco_user",
                        "applicant_id",
                    )
                ), name
                assert "relation" not in applicant, name
        else:
            assert len(quote["county_code"]) == 5, name
            assert len(quote["applicants"]) == 1, name
            assert all(quote["applicants"][0].values()), name


def test_medicare_plan_quote_contexts_cover_minimum_to_full_inputs() -> None:
    contexts = CORPUS["contexts"]

    minimum = _quote_inputs(contexts["plans_api_mols_minimum"])
    demographics = _quote_inputs(contexts["plans_api_mols_demographics"])
    full = _quote_inputs(contexts["plans_api_mols_full"])

    assert minimum["applicants"] == [{"applicant_type": "PRIMARY"}]
    assert demographics["applicants"] == [
        {
            "applicant_type": "PRIMARY",
            "date_of_birth": "1957-01-01",
            "gender": "FEMALE",
        }
    ]
    assert "part_a_eff_date" not in demographics
    assert "part_b_eff_date" not in demographics
    assert full["applicants"] == demographics["applicants"]
    assert full["part_a_eff_date"] == "2026-08-01"
    assert full["part_b_eff_date"] == "2026-07-01"


def test_iols_plan_quote_context_uses_only_fixed_required_applicant_fields() -> None:
    quote = _quote_inputs(CORPUS["contexts"]["plans_api_iols_minimum"])

    assert quote["applicants"] == [
        {
            "applicant_type": "PRIMARY",
            "date_of_birth": "1991-01-01",
            "is_tobacco_user": "NO",
            "applicant_id": "applicant-1",
        },
        {
            "applicant_type": "SPOUSE",
            "date_of_birth": "1992-02-02",
            "is_tobacco_user": "NO",
            "applicant_id": "applicant-2",
        },
        {
            "applicant_type": "DEPENDENT",
            "date_of_birth": "2015-03-03",
            "is_tobacco_user": "NO",
            "applicant_id": "applicant-3",
        },
    ]
    assert quote["subsidy_amt"] == "741"
    assert quote["cost_share_reduction"] == "100"


def test_corpus_covers_observed_conversation_failures_and_core_boundaries() -> None:
    ids = {conversation["id"] for conversation in CORPUS["conversations"]}
    assert {
        "behavioral_ind_er_casual_selection",
        "behavioral_ind_correction",
        "behavioral_ind_personal_premium",
        "behavioral_medicare_benefit_selection",
        "behavioral_medicare_ambiguous_plan_pronoun",
        "behavioral_medicare_current_vision",
        "behavioral_medicare_same_plan",
        "behavioral_medicare_surgery_disambiguation",
        "behavioral_medicare_unicode_selection",
        "behavioral_medicare_optional_package",
        "behavioral_medicare_conflicting_network",
        "defect_11_hmo_pos_equivalent_wording",
        "defect_39_medication_comparison_boundary",
        "instructional_bias_age_medicare_guardrail",
        "instructional_bias_gender_guardrail",
        "wellpoint_unavailable_external_plan",
        "rc4_ambiguous_monthly_premium",
        "rc4_base_optional_package_identity",
        "rc4_er_service_setting",
        "rc4_factual_reasons_boundary",
        "rc4_inpatient_day_ranges",
        "rc4_medicare_effective_date",
        "rc4_medicare_no_action_open_enrollment",
        "rc4_medigap_shared_booklet",
        "rc5_negated_plan_correction",
        "rc5_ozempic_abbreviated_selection",
        "rc5_partial_match_caremore_comparison",
        "rc5_stage_bare_both_selection",
        "rc4_over_limit_deductible",
        "rc4_unavailable_mixed_comparison",
        "uat_primary_care_requires_plan_disambiguation",
        "structured_iols_example_continuity",
        "structured_iols_example_comparison",
        "structured_iols_example_mixed_fallback",
        "structured_iols_example_pending_premium",
        "structured_iols_example_visit_comparison",
        "structured_mols_example_continuity",
        "structured_mols_example_cost_limits",
        "structured_mols_example_urgent_emergency",
        "structured_mols_example_options_and_tiers",
        "structured_mols_example_full_medsupp",
    } <= ids
    assert CORPUS["statistics"]["multi_turn_conversations"] >= 12
    assert {turn["expected_route"] for _, turn in _turns()} == {
        "clarify_benefit",
        "clarify_plan",
        "general_search",
        "guardrail",
        "plan_details",
        "plan_details_and_plan_search",
        "plan_search",
    }


def test_family_quote_accepts_child_without_relation() -> None:
    """Keep all three applicants when the family uses the CHILD applicant type."""
    context = deepcopy(CORPUS["contexts"]["plans_api_iols_minimum"])
    public = context_to_shopper_api(context)
    public["user_context"]["applicants"][2]["applicant_type"] = "CHILD"

    quote = shopper_api_to_wxo(public)["application_plan_quote_inputs"]

    assert [applicant["applicant_type"] for applicant in quote["applicants"]] == [
        "PRIMARY",
        "SPOUSE",
        "CHILD",
    ]
    assert all("relation" not in applicant for applicant in quote["applicants"])
