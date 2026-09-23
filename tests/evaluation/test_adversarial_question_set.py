"""Integrity checks for the current adversarial behavioral corpus."""

from pathlib import Path

import yaml
from evaluation.behavioral import load_corpus

CORPUS_PATH = Path(__file__).with_name("adversarial_questions.yaml")


def test_adversarial_corpus_is_runnable_by_the_behavioral_evaluator() -> None:
    corpus = load_corpus(CORPUS_PATH)
    conversations = corpus["conversations"]
    turns = [turn for conversation in conversations for turn in conversation["turns"]]

    assert len(conversations) == corpus["statistics"]["conversations"] == 33
    assert len(turns) == corpus["statistics"]["turns"] == 39
    assert set(corpus["contexts"]) == {"medicare_member_current_plan"}
    assert all(conversation["evaluation_status"] == "eligible" for conversation in conversations)
    assert all(turn["question"] and turn["expected_route"] for turn in turns)


def test_adversarial_retrieval_expectations_match_current_routes() -> None:
    corpus = yaml.safe_load(CORPUS_PATH.read_text())
    turns = [turn for conversation in corpus["conversations"] for turn in conversation["turns"]]

    for turn in turns:
        expected_route = turn["expected_route"]
        if expected_route in {"general_search", "plan_search"}:
            assert turn["source_rag_expected"] is True
        else:
            assert turn["source_rag_expected"] is False
        if expected_route == "plan_search":
            assert turn["expected_plan_ids"]
        else:
            assert turn["expected_plan_ids"] == []
