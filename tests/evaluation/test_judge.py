"""Deterministic contracts for opt-in qualitative behavioral grading."""

from __future__ import annotations

import argparse
import json

import pytest
from pydantic import ValidationError

from evaluation import behavioral, judge


def _judgment(*, retrieval_grade: str = "N/A") -> str:
    return json.dumps(
        {
            "agent_behavior": {
                "grade": "Meets",
                "reason": "The route matches the requested clarification behavior.",
                "evidence_refs": ["expected_route", "observations.route"],
            },
            "retrieval_quality": {
                "grade": retrieval_grade,
                "reason": "No RAG evidence was exposed, so retrieval is not graded.",
                "evidence_refs": ["observations.rag_evidence_count"],
            },
            "final_response_quality": {
                "grade": "Mixed",
                "reason": "The response is safe but unnecessarily lists several plans.",
                "evidence_refs": ["expected_behavior", "response"],
            },
            "response_miss_safety": None,
            "disposition": "Follow-up",
            "summary": "Correct clarification route with a presentation gap.",
        }
    )


def _source_record() -> dict:
    return {
        "schema_version": 1,
        "candidate": "working-tree",
        "trial": 1,
        "conversation_id": "ambiguous-plan",
        "turn_id": "ambiguous-plan-01",
        "question": "What would I pay for specialist visits under it?",
        "reference_answer": "Ask which plan the shopper means.",
        "expected_behavior": "Clarify without printing the full plan catalog.",
        "expected_route": "clarify_plan",
        "expected_plan_ids": [],
        "available_plans": [
            {"plan_id": "P1", "plan_name": "Plan One"},
            {"plan_id": "P2", "plan_name": "Plan Two"},
        ],
        "result": {"response_text": "Which plan do you mean?", "run_id": "run-1"},
        "observations": {
            "route": "plan_turn",
            "rag_called": False,
            "rag_evidence_count": 0,
            "rag_context_excerpt": [],
        },
        "status": "completed",
    }


class FakeClient:
    model_id = "test-judge-model"

    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return next(self.responses)


@pytest.mark.parametrize("missing", ["WATSONX_AI_API_VERSION", "WATSONX_IAM_URL"])
def test_judge_requires_explicit_watsonx_endpoint_configuration(monkeypatch, missing) -> None:
    values = {
        "WATSONX_AI_URL": "https://watsonx.example.test",
        "WATSONX_AI_API_KEY": "api-key",
        "WATSONX_AI_PROJECT_ID": "project",
        "WATSONX_AI_MODEL_ID": "model",
        "WATSONX_AI_API_VERSION": "2025-02-06",
        "WATSONX_IAM_URL": "https://iam.example.test/token",
    }
    for name, value in values.items():
        if name != missing:
            monkeypatch.setenv(name, value)
    args = argparse.Namespace(
        url_env="WATSONX_AI_URL",
        api_key_env="WATSONX_AI_API_KEY",
        project_id_env="WATSONX_AI_PROJECT_ID",
        model_id=None,
        model_id_env="WATSONX_AI_MODEL_ID",
        api_version_env="WATSONX_AI_API_VERSION",
        iam_url_env="WATSONX_IAM_URL",
        timeout=60,
    )

    with pytest.raises(ValueError, match=missing):
        judge._client_from_args(args)


def test_judgment_schema_rejects_unknown_grades() -> None:
    payload = json.loads(_judgment())
    payload["agent_behavior"]["grade"] = "Pretty good"

    with pytest.raises(ValidationError):
        judge.parse_judgment(json.dumps(payload))


@pytest.mark.parametrize(
    ("grade", "miss_safety"),
    [("Misses", None), ("Meets", "Safe"), ("Mixed", "Unsafe")],
)
def test_judgment_schema_requires_safety_only_for_response_misses(
    grade: str, miss_safety: str | None
) -> None:
    payload = json.loads(_judgment())
    payload["final_response_quality"]["grade"] = grade
    payload["response_miss_safety"] = miss_safety

    with pytest.raises(ValidationError, match="response_miss_safety"):
        judge.parse_judgment(json.dumps(payload))


@pytest.mark.parametrize("miss_safety", ["Safe", "Unsafe"])
def test_judgment_schema_accepts_safe_and_unsafe_response_misses(miss_safety: str) -> None:
    payload = json.loads(_judgment())
    payload["final_response_quality"]["grade"] = "Misses"
    payload["response_miss_safety"] = miss_safety

    result = judge.parse_judgment(json.dumps(payload))

    assert result.response_miss_safety == miss_safety


def test_judge_uses_semantic_rubric_and_sanitized_record_fields() -> None:
    client = FakeClient([_judgment()])

    result = judge.judge_record(_source_record(), client, "# Test rubric")

    assert result.final_response_quality.grade == "Mixed"
    system_prompt, user_prompt = client.calls[0]
    assert system_prompt.strip()
    assert "# Test rubric" in system_prompt
    assert "same underlying issue in every component" in system_prompt
    assert "concrete, plausible path to meaningful shopper harm" in system_prompt
    payload = json.loads(user_prompt)
    assert payload["question"] == "What would I pay for specialist visits under it?"
    assert payload["response"] == "Which plan do you mean?"
    assert "result" not in payload


def test_judge_enforces_retrieval_observability_grade(monkeypatch) -> None:
    client = FakeClient([_judgment(retrieval_grade="Meets"), _judgment()])
    monkeypatch.setattr(judge.time, "sleep", lambda _: None)

    result = judge.judge_record(_source_record(), client, "rubric")

    assert result.retrieval_quality.grade == "N/A"
    assert len(client.calls) == 2


def test_judge_grades_exposed_structured_plan_details_as_retrieval(monkeypatch) -> None:
    record = _source_record()
    record["observations"]["plan_details_excerpt"] = [
        {
            "plan_id": "P1",
            "plan_name": "Plan One",
            "details": {"premium": {"total": 42.0}},
        }
    ]
    client = FakeClient([_judgment(), _judgment(retrieval_grade="Meets")])
    monkeypatch.setattr(judge.time, "sleep", lambda _: None)

    result = judge.judge_record(record, client, "rubric")

    assert result.retrieval_quality.grade == "Meets"
    assert len(client.calls) == 2


def test_judge_run_writes_reviewable_artifacts(tmp_path) -> None:
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "results.jsonl").write_text(json.dumps(_source_record()) + "\n")
    (source / "manifest.json").write_text("{}\n")
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# Rubric\n")
    output = tmp_path / "judged"
    args = argparse.Namespace(
        run=source,
        output=output,
        rubric=rubric,
        conversation_id=[],
        turn_id=[],
        limit=None,
        resume=False,
    )

    assert judge.run(args, client=FakeClient([_judgment()])) == 0

    judged = json.loads((output / "judgments.jsonl").read_text())
    summary = json.loads((output / "summary.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert judged["schema_version"] == judge.SCHEMA_VERSION
    assert judged["judgment"]["agent_behavior"]["grade"] == "Meets"
    assert judged["expected_route"] == "clarify_plan"
    assert judged["judgment"]["final_response_quality"]["evidence_refs"] == [
        "expected_behavior",
        "response",
    ]
    assert summary["grades"]["retrieval_quality"] == {"N/A": 1}
    assert summary["schema_version"] == judge.SCHEMA_VERSION
    assert summary["response_miss_safety"] == {}
    assert summary["dispositions"] == {"Follow-up": 1}
    assert summary["intended_behavior_cohorts"] == {
        "clarify_plan_no_tool": {
            "expected_route": "clarify_plan",
            "turns": 1,
            "grades": {
                "agent_behavior": {"Meets": 1},
                "retrieval_quality": {"N/A": 1},
                "final_response_quality": {"Mixed": 1},
            },
            "response_miss_safety": {},
            "dispositions": {"Follow-up": 1},
        }
    }
    assert manifest["model_id"] == "test-judge-model"
    assert manifest["schema_version"] == judge.SCHEMA_VERSION
    assert manifest["prompt_version"] == judge.PROMPT_VERSION
    assert (output / "judgments.csv").is_file()


def test_judge_resume_rejects_changed_source_results(tmp_path) -> None:
    source = tmp_path / "candidate"
    source.mkdir()
    results = source / "results.jsonl"
    results.write_text(json.dumps(_source_record()) + "\n")
    (source / "manifest.json").write_text("{}\n")
    rubric = tmp_path / "rubric.md"
    rubric.write_text("# Rubric\n")
    output = tmp_path / "judged"
    args = argparse.Namespace(
        run=source,
        output=output,
        rubric=rubric,
        conversation_id=[],
        turn_id=[],
        limit=None,
        resume=False,
    )
    assert judge.run(args, client=FakeClient([_judgment()])) == 0

    changed = _source_record()
    changed["result"]["response_text"] = "Changed response"
    results.write_text(json.dumps(changed) + "\n")
    args.resume = True

    with pytest.raises(ValueError, match="source_results_sha256"):
        judge.run(args, client=FakeClient([]))


def test_behavioral_cli_exposes_opt_in_judge_command(tmp_path) -> None:
    args = behavioral.build_parser().parse_args(["judge", "--run", str(tmp_path)])

    assert args.func is judge.run
