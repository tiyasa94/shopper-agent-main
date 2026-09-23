"""Integrity checks for the curated independent behavioral-evaluation corpus."""

import re
from pathlib import Path

import yaml

from shared.classifier import CLASSIFIER_PROMPT_TEMPLATE

ROOT = Path(__file__).parents[2]
CORPUS_PATH = Path(__file__).with_name("external_questions.yaml")
CORPUS = yaml.safe_load(CORPUS_PATH.read_text())


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _turn_prompt() -> str:
    return CLASSIFIER_PROMPT_TEMPLATE


def _agent_instructions() -> str:
    agent = yaml.safe_load((ROOT / "src/agent.yaml").read_text())
    return agent["instructions"]


def _turns() -> list[dict]:
    return [turn for conversation in CORPUS["conversations"] for turn in conversation["turns"]]


def test_external_corpus_preserves_source_shape_without_harness_outputs():
    stats = CORPUS["statistics"]
    assert stats == {
        "turns": 184,
        "unique_questions": 175,
        "conversations": 85,
        "contexts": 5,
        "turns_by_market": {"Individual": 81, "Medicare": 103},
        "turns_by_status": {
            "excluded_prompt_overlap": 22,
            "eligible": 155,
            "needs_review": 7,
        },
        "conversations_by_status": {
            "excluded_prompt_overlap": 18,
            "eligible": 62,
            "needs_review": 5,
        },
        "missing_reference_answers": 8,
        "missing_source_intents": 2,
    }
    assert CORPUS["source"]["sha256"] == (
        "2dc78fa08103015ae47ab82426d241d29505426d6771559a7e7778057009f045"
    )
    forbidden = {
        "response",
        "rag_context",
        "query_payload",
        "agents_invoked",
        "plans_found",
        "plans_searched_for",
        "escalation",
        "trace_id",
        "timestamp",
        "session_id",
    }
    assert all(not (forbidden & set(turn)) for turn in _turns())


def test_contexts_are_deduplicated_native_wxo_inputs():
    contexts = CORPUS["contexts"]
    assert set(contexts) == {
        "individual_prospect",
        "individual_member_current_plan",
        "medicare_prospect",
        "medicare_member_current_plan",
        "medicare_prospect_current_plan",
    }
    for context in contexts.values():
        assert context["application_market_segment"] in {"IND", "Medicare"}
        assert context["prospect_type"] in {"prospect", "member"}
        assert context["application_available_plans"]
        assert isinstance(context["application_available_plans"], list)
        assert isinstance(context["application_recommended_plans"], list)


def test_conversations_have_stable_unique_ids_contexts_and_source_coordinates():
    contexts = set(CORPUS["contexts"])
    conversations = CORPUS["conversations"]
    conversation_ids = [conversation["id"] for conversation in conversations]
    turns = _turns()
    turn_ids = [turn["id"] for turn in turns]
    coordinates = [
        (conversation["market"], turn["source_row"])
        for conversation in conversations
        for turn in conversation["turns"]
    ]
    assert len(conversation_ids) == len(set(conversation_ids))
    assert len(turn_ids) == len(set(turn_ids))
    assert len(coordinates) == len(set(coordinates))
    assert all(conversation["context"] in contexts for conversation in conversations)
    assert all(
        turn.get("context", conversation["context"]) in contexts
        for conversation in conversations
        for turn in conversation["turns"]
    )


def test_prompt_overlap_excludes_the_entire_conversation():
    for conversation in CORPUS["conversations"]:
        has_overlap = any(turn["prompt_overlap"] for turn in conversation["turns"])
        if has_overlap:
            assert conversation["evaluation_status"] == "excluded_prompt_overlap"
        if conversation["evaluation_status"] == "eligible":
            assert not has_overlap
            assert all(turn["evaluation_status"] == "eligible" for turn in conversation["turns"])


def test_active_refactor_prompts_contain_no_external_question_verbatim():
    active_prompt = _normalized(f"{_turn_prompt()} {_agent_instructions()}")
    leaked = [
        turn["question"]
        for turn in _turns()
        if len(_normalized(turn["question"]).split()) >= 3
        and _normalized(turn["question"]) in active_prompt
    ]
    assert leaked == []


def test_source_annotations_are_not_harness_assertions():
    assert all("expect" not in turn for turn in _turns())


def test_default_pilot_is_balanced_and_stratified_without_prompt_overlap():
    from evaluation.behavioral import select_conversations

    selected = select_conversations(
        CORPUS,
        statuses={"eligible"},
        markets=set(),
        conversation_ids=set(),
        suite="pilot",
        limit=None,
    )

    assert len(selected) == 12
    assert sum(item["market"] == "Individual" for item in selected) == 6
    assert sum(item["market"] == "Medicare" for item in selected) == 6
    assert all(item["evaluation_status"] == "eligible" for item in selected)
    for market in ("Individual", "Medicare"):
        turns = [turn for item in selected if item["market"] == market for turn in item["turns"]]
        assert {turn["source_intent"] for turn in turns} == {
            "broad_plans",
            "generic_info",
            "specific_plan",
        }
        assert {turn["source_rag_expected"] for turn in turns} == {False, True}
