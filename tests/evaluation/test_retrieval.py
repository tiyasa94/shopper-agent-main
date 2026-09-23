"""Tests for the local retrieval corpus and benchmark mechanics."""

import subprocess
from collections import Counter

import pytest
from evaluation.retrieval import (
    DEFAULT_CORPUS,
    _git_revision,
    _payload,
    _score_case,
    load_corpus,
    require_loopback_url,
)


def test_git_revision_fingerprints_scoped_dirty_content(tmp_path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Retrieval Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("initial\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)

    clean = _git_revision(tmp_path)
    tracked.write_text("changed\n")
    modified = _git_revision(tmp_path)
    assert modified.startswith(f"{clean}+dirty.")

    (tmp_path / "untracked.txt").write_text("new\n")
    assert _git_revision(tmp_path) != modified


def test_retrieval_corpus_has_the_planned_balanced_shape() -> None:
    corpus = load_corpus(DEFAULT_CORPUS)
    cases = corpus["expanded_cases"]
    assert len(cases) == 157
    assert Counter(case["category"] for case in cases) == {
        "precise_costs": 50,
        "coverage_limits": 30,
        "comparisons": 20,
        "overview": 20,
        "uncertainty": 15,
        "paraphrase_context": 15,
        "plan_level_facts": 7,
    }
    assert Counter(case["tier"] for case in cases) == {
        "release_gate": 60,
        "diagnostic": 97,
    }
    assert Counter(case["market"] for case in cases) == {"IOLS": 74, "MOLS": 83}


def test_every_structured_intent_is_bounded_plan_independent_and_authorized() -> None:
    corpus = load_corpus(DEFAULT_CORPUS)
    for case in corpus["expanded_cases"]:
        context = corpus["contexts"][case["context"]]
        assert isinstance(context.get("exchange_indicator", ""), str)
        available = {plan["plan_id"]: plan["plan_name"] for plan in context["available_plans"]}
        assert set(case["plan_ids"]) <= set(available)
        assert case["expected_concepts"]
        assert 1 <= len(case["searches"]) <= 4
        for search in case["searches"]:
            text = " ".join(
                [
                    search["semantic_query"],
                    *search.get("requested_facts", []),
                    *search.get("conditions", []),
                    *search.get("lexical_terms", []),
                ]
            ).casefold()
            for plan_id in case["plan_ids"]:
                assert plan_id.casefold() not in text
                assert available[plan_id].casefold() not in text


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8081", "http://localhost:8081", "http://[::1]:8081/"],
)
def test_loopback_guard_accepts_only_local_origins(url: str) -> None:
    assert require_loopback_url(url).startswith("http://")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8081",
        "https://rag.example.test",
        "http://192.168.1.2:8081",
        "http://localhost:8081/retrieve/iols",
        "http://user:password@localhost:8081",
    ],
)
def test_loopback_guard_rejects_remote_or_path_qualified_targets(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        require_loopback_url(url)


def test_control_uses_one_explicit_search_and_structured_uses_curated_intent() -> None:
    corpus = load_corpus(DEFAULT_CORPUS)
    case = corpus["expanded_cases"][0]
    context = corpus["contexts"][case["context"]]
    control = _payload(case, context, "control")
    structured = _payload(case, context, "structured")
    assert "request_id" not in control
    assert control["searches"] == [{"semantic_query": case["query"]}]
    assert structured == {
        **control,
        "searches": case["searches"],
    }


def test_case_scoring_reports_recall_plan_safety_and_duplicates() -> None:
    case = {
        "plan_ids": ["P1", "P2"],
        "expected_concepts": [["emergency room", "emergency"], ["copay", "copayment"]],
    }
    body = {
        "results": [
            {
                "text": "Emergency room care has a copayment.",
                "metadata": {"prop_plan_id": "P1"},
            },
            {
                "text": "Emergency room care has a copayment.",
                "metadata": {"prop_plan_id": "P1"},
            },
            {
                "text": "Emergency room copayment applies.",
                "metadata": {"prop_plan_id": "P2"},
            },
        ]
    }
    metrics = _score_case(case, body, 12.5)
    assert metrics["fact_recall_at_8"] == 1.0
    assert metrics["answerable_at_12"] is True
    assert metrics["mrr_at_20"] == 1.0
    assert metrics["plan_coverage"] == 1.0
    assert metrics["plan_contamination"] is False
    assert metrics["duplicate_chunks"] == 1
    assert metrics["duration_ms"] == 12.5
