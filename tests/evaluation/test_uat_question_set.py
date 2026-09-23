"""Quality and integrity checks for the reviewed 133-turn UAT corpus."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import yaml

from shared.classifier import CLASSIFIER_PROMPT_TEMPLATE

ROOT = Path(__file__).parents[2]
CORPUS_PATH = Path(__file__).with_name("uat_questions.yaml")
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


def test_uat_corpus_preserves_raw_provenance_at_reviewed_release_size() -> None:
    raw = yaml.safe_load(RAW_SOURCE_PATH.read_text())
    assert CORPUS["source"]["sha256"] == raw["source"]["sha256"]
    assert CORPUS["statistics"] == {
        "conversations": 62,
        "turns": 133,
        "contexts": 5,
        "turns_by_market": {"Individual": 55, "Medicare": 78},
        "multi_turn_conversations": 54,
    }
    for name, context in raw["contexts"].items():
        reviewed = CORPUS["contexts"][name]
        expected_county_code = context["user_county_code"]
        if context["application_market_segment"] == "IND":
            expected_county_code = expected_county_code[-3:]
        assert reviewed == {**context, "user_county_code": expected_county_code}


def test_uat_questions_are_unique_natural_and_fully_reviewed() -> None:
    turns = _turns()
    questions = [_normalized(turn["question"]) for _, turn in turns]
    assert len(questions) == len(set(questions))

    forbidden = (
        re.compile(r"\bwith plan\b", re.IGNORECASE),
        re.compile(r"<\s*member accessing", re.IGNORECASE),
        re.compile(r"\bI CareMore\b|\bAnthem Select\b", re.IGNORECASE),
        re.compile(r"dependent/spouse/child|coverage/insurance", re.IGNORECASE),
    )
    for _, turn in turns:
        assert turn["question"].strip()
        assert turn["reference_answer"].strip()
        assert turn["expected_behavior"].strip()
        assert turn["question"].rstrip().endswith(("?", ".", "!"))
        assert all(not pattern.search(turn["question"]) for pattern in forbidden), turn["id"]


def test_uat_routes_retrieval_and_allowed_plan_ids_are_coherent() -> None:
    turns = _turns()
    assert {turn["expected_route"] for _, turn in turns} == {
        "clarify_plan",
        "general_search",
        "guardrail",
        "plan_search",
    }
    assert Counter(turn["expected_route"] for _, turn in turns) == {
        "plan_search": 52,
        "general_search": 29,
        "guardrail": 27,
        "clarify_plan": 25,
    }

    for conversation, turn in turns:
        available_ids = {
            plan["plan_id"]
            for plan in CORPUS["contexts"][conversation["context"]]["application_available_plans"]
        }
        plan_ids = set(turn["expected_plan_ids"])
        assert plan_ids <= available_ids, turn["id"]
        assert turn["source_rag_expected"] is (
            turn["expected_route"] in {"general_search", "plan_search"}
        )
        assert bool(plan_ids) is (turn["expected_route"] == "plan_search")


def test_uat_questions_do_not_appear_verbatim_in_the_active_agent_prompt() -> None:
    agent = yaml.safe_load((ROOT / "src/agent.yaml").read_text())
    prompt = _normalized(CLASSIFIER_PROMPT_TEMPLATE + " " + yaml.safe_dump(agent, sort_keys=False))
    leaked = [
        turn["id"]
        for _, turn in _turns()
        if len(_normalized(turn["question"]).split()) >= 3
        and _normalized(turn["question"]) in prompt
    ]
    assert leaked == []
