from __future__ import annotations

import json
from pathlib import Path

import yaml
from evaluation.assertions import evaluate


def _record(turn_id: str, response: str, *, rag_called: bool, plan_ids: list[str]) -> dict:
    return {
        "trial": 1,
        "conversation_id": "conversation",
        "turn_id": turn_id,
        "available_plans": [{"plan_id": "P1", "plan_name": "Catalog Plan"}],
        "result": {"response_text": response},
        "observations": {
            "rag_called": rag_called,
            "rag_call_count": int(rag_called),
            "search_plan_ids": plan_ids,
            "sensitive_value_echoed": False,
            "serialized_envelope_exposed": False,
        },
    }


def test_evaluate_checks_response_retrieval_and_catalog_rules(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    records = [
        _record("amount", "The amount is $6,751.", rag_called=True, plan_ids=["P1"]),
        _record("unknown", "Which plan do you mean?", rag_called=False, plan_ids=[]),
    ]
    (run / "results.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "turns": {
                    "amount": {
                        "expected_rag_called": True,
                        "expected_plan_ids": ["P1"],
                        "required_response_substrings": ["$6,751"],
                        "forbidden_response_substrings": ["$2,100"],
                    },
                    "unknown": {
                        "expected_rag_called": False,
                        "forbid_available_plan_names": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    report = evaluate(run, spec)

    assert report["checks"] == 2
    assert report["failed"] == 0
    assert report["missing_turns"] == []


def test_evaluate_reports_failures_and_missing_turns(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    record = _record("bad", "Catalog Plan costs $2,100.", rag_called=False, plan_ids=[])
    (run / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "turns": {
                    "bad": {
                        "expected_rag_called": True,
                        "required_response_substrings": ["$6,751"],
                        "forbidden_response_substrings": ["$2,100"],
                        "forbid_available_plan_names": True,
                    },
                    "missing": {"expected_rag_called": False},
                },
            }
        ),
        encoding="utf-8",
    )

    report = evaluate(run, spec)

    assert report["failed"] == 1
    assert report["missing_turns"] == ["missing"]
    assert len(report["results"][0]["failures"]) == 4


def test_evaluate_infers_plan_ids_from_public_plan_names(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    record = _record("turn", "Supported answer", rag_called=True, plan_ids=[])
    record["observations"]["plans_searched_for"] = ["Catalog Plan"]
    (run / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "turns": {"turn": {"expected_plan_ids": ["P1"]}},
            }
        ),
        encoding="utf-8",
    )

    report = evaluate(run, spec)

    assert report["passed"] == 1


def test_evaluate_treats_expected_plan_ids_as_an_unordered_exact_set(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    record = _record("turn", "Supported answer", rag_called=True, plan_ids=["P2", "P1"])
    (run / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "turns": {"turn": {"expected_plan_ids": ["P1", "P2"]}},
            }
        ),
        encoding="utf-8",
    )

    report = evaluate(run, spec)

    assert report["passed"] == 1


def test_evaluate_checks_structured_plan_call_ids_and_detail_types(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    record = _record("turn", "Supported answer", rag_called=False, plan_ids=[])
    record["observations"].update(
        {
            "plan_details_called": True,
            "plan_details_call_count": 1,
            "plan_details_plan_ids": ["P1"],
            "plan_detail_types": ["specialist", "premium"],
            "plan_details_outcome": "complete",
            "plan_details_coverage_complete": True,
            "plan_details_fallback_types": [],
        }
    )
    (run / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "turns": {
                    "turn": {
                        "expected_plan_details_called": True,
                        "expected_plan_ids": ["P1"],
                        "expected_detail_types": ["premium", "specialist"],
                        "expected_plan_details_outcome": "complete",
                        "expected_plan_details_coverage_complete": True,
                        "expected_plan_details_fallback_types": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    report = evaluate(run, spec)

    assert report["passed"] == 1


def test_evaluate_leaves_repeated_calls_as_diagnostic_metrics(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    record = _record("turn", "Supported answer", rag_called=True, plan_ids=["P1"])
    record["observations"].update(
        {
            "rag_call_count": 2,
            "plan_details_call_count": 2,
        }
    )
    (run / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "turns": {
                    "turn": {
                        "expected_rag_called": True,
                        "expected_plan_ids": ["P1"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert evaluate(run, spec)["passed"] == 1


def test_evaluate_skips_private_plan_trace_checks_when_public_transport_cannot_observe_them(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    run.mkdir()
    record = _record("turn", "Supported answer", rag_called=False, plan_ids=[])
    record["observations"].update(
        {
            "plan_details_observable": False,
            "plan_details_called": None,
            "plan_detail_types": [],
        }
    )
    (run / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "turns": {
                    "turn": {
                        "expected_plan_details_called": True,
                        "expected_detail_types": ["premium"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert evaluate(run, spec)["passed"] == 1


def test_evaluate_does_not_match_short_plan_name_inside_word(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    record = _record("turn", "Which plan are you asking about?", rag_called=False, plan_ids=[])
    record["available_plans"] = [{"plan_id": "A", "plan_name": "Plan A"}]
    (run / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "turns": {"turn": {"forbid_available_plan_names": True}},
            }
        ),
        encoding="utf-8",
    )

    report = evaluate(run, spec)

    assert report["passed"] == 1


def test_evaluate_requires_exact_summary_details_structure(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    records = [
        _record(
            "exact",
            "Summary answer:\nThe direct answer.\n\nDetails:\n- Grounded detail.",
            rag_called=True,
            plan_ids=["P1"],
        ),
        _record(
            "markdown",
            "**Summary answer:**\nThe direct answer.\n\n**Details:**\n- Grounded detail.",
            rag_called=True,
            plan_ids=["P1"],
        ),
        _record(
            "answer_heading",
            "**Answer:** The direct answer.\n\n**Details**\n- Grounded detail.",
            rag_called=True,
            plan_ids=["P1"],
        ),
        _record(
            "custom_heading",
            "**Specialist visit copay**\n- Plan One: $10.\n- Plan Two: $15.",
            rag_called=True,
            plan_ids=["P1"],
        ),
    ]
    (run / "results.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "turns": {
                    "exact": {"required_response_structure": "summary_details"},
                    "markdown": {"required_response_structure": "summary_details"},
                    "answer_heading": {"required_response_structure": "summary_details"},
                    "custom_heading": {"required_response_structure": "summary_details"},
                },
            }
        ),
        encoding="utf-8",
    )

    report = evaluate(run, spec)

    assert report["passed"] == 1
    assert report["failed"] == 3
    for result in report["results"][1:]:
        assert result["failures"] == [
            "response does not use the exact Summary answer/Details structure"
        ]


def test_evaluate_normalizes_unicode_spacing_before_percent_sign(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    record = _record(
        "turn",
        "The cost is 30\u202f% after deductible.",
        rag_called=False,
        plan_ids=[],
    )
    (run / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "turns": {"turn": {"required_response_substrings": ["30%"]}},
            }
        ),
        encoding="utf-8",
    )

    assert evaluate(run, spec)["passed"] == 1
