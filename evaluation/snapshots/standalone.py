"""Freeze and validate a sanitized standalone release-evaluation snapshot."""

from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from evaluation.behavioral import summarize
from evaluation.io import read_csv as _read_csv
from evaluation.io import read_json as _read_json
from evaluation.io import read_jsonl as _read_jsonl
from evaluation.io import sha256 as _sha256
from evaluation.io import write_csv as _write_csv
from evaluation.io import write_json as _write_json
from evaluation.snapshots.paired import _run_snapshot, _sanitized_result

SCHEMA_VERSION = 1
REQUIRED_FILES = (
    "release.yaml",
    "run.json",
    "questions.yaml",
    "responses.jsonl",
    "component-review.csv",
    "conversation-review.csv",
    "summary.json",
    "checksums.sha256",
)
GRADE_ORDER = {"Meets": 0, "Mixed": 1, "Misses": 2}
COMPONENT_FIELDS = (
    "conversation_id",
    "turn_id",
    "agent_behavior",
    "agent_behavior_critical",
    "retrieval_quality",
    "retrieval_quality_critical",
    "response_quality",
    "response_quality_critical",
    "disposition",
    "critical_issue_notes",
    "reviewer_notes",
)
CONVERSATION_FIELDS = (
    "conversation_id",
    "turn_count",
    "agent_behavior",
    "response_quality",
    "critical_issue",
    "turn_ids",
    "critical_issue_notes",
    "reviewer_notes",
)
SOURCE_REVIEW_FIELDS = {
    "conversation_id": "Conversation ID",
    "turn_id": "Turn ID",
    "agent_behavior": "Agent Behavior (Meets/Mixed/Misses)",
    "retrieval_quality": "Retrieval Quality (Meets/Mixed/Misses/N/A)",
    "response_quality": "Final Response Quality (Meets/Mixed/Misses)",
    "disposition": "Disposition (Accept/Follow-up/Defect)",
    "reviewer_notes": "Reviewer Notes",
}
FORBIDDEN_KEYS = {
    "run_id",
    "submitted_run_id",
    "request_id",
    "trace_id",
    "request_context",
    "tool_calls",
    "tool_responses",
}
CRITICAL_FIELDS = (
    "agent_behavior_critical",
    "retrieval_quality_critical",
    "response_quality_critical",
)


def _grade_counts(rows: list[dict[str, str]], field: str) -> dict[str, int]:
    return dict(Counter(row[field] for row in rows))


def _is_true(value: str) -> bool:
    return value.strip().casefold() == "true"


def _forbidden_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if key in FORBIDDEN_KEYS:
                paths.append(path)
            paths.extend(_forbidden_paths(item, path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_forbidden_paths(item, f"{prefix}[{index}]"))
    return paths


def create_snapshot(
    *,
    release: str,
    released_on: str,
    run_dir: Path,
    review_csv: Path,
    critical_review_csv: Path,
    conversation_review_csv: Path,
    corpus: Path,
    output_dir: Path,
    force: bool = False,
) -> None:
    if output_dir.exists() and any(output_dir.iterdir()) and not force:
        raise FileExistsError(f"Snapshot already exists: {output_dir}; pass --force to refresh")
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = _read_json(run_dir / "manifest.json")
    raw_summary = _read_json(run_dir / "summary.json")
    records = _read_jsonl(run_dir / "results.jsonl")
    by_turn = {str(row["turn_id"]): row for row in records}
    if len(by_turn) != len(records):
        raise ValueError("Run contains duplicate turn IDs")

    source_review = _read_csv(review_csv)
    component_review = [
        {
            target: (
                "Not graded"
                if target == "retrieval_quality" and row[source] == "N/A"
                else row[source]
            )
            for target, source in SOURCE_REVIEW_FIELDS.items()
        }
        for row in source_review
    ]
    if {row["turn_id"] for row in component_review} != set(by_turn):
        raise ValueError("Component review does not cover the run turn set")

    critical_by_turn = {row["turn_id"]: row for row in _read_csv(critical_review_csv)}
    if not set(critical_by_turn) <= set(by_turn):
        raise ValueError("Critical-issue review contains unknown turn IDs")
    for row in component_review:
        critical = critical_by_turn.get(row["turn_id"], {})
        for critical_field in CRITICAL_FIELDS:
            row[critical_field] = str(_is_true(critical.get(critical_field, "false"))).lower()
        row["critical_issue_notes"] = critical.get("critical_issue_notes", "")
        for critical_field, grade_field in (
            ("agent_behavior_critical", "agent_behavior"),
            ("retrieval_quality_critical", "retrieval_quality"),
            ("response_quality_critical", "response_quality"),
        ):
            if _is_true(row[critical_field]) and row[grade_field] != "Misses":
                raise ValueError(f"{critical_field} requires a Misses grade for {row['turn_id']}")

    turns_by_conversation: dict[str, list[str]] = {}
    for row in records:
        turns_by_conversation.setdefault(str(row["conversation_id"]), []).append(
            str(row["turn_id"])
        )
    multi_turn_ids = {
        conversation_id
        for conversation_id, turn_ids in turns_by_conversation.items()
        if len(turn_ids) > 1
    }
    supplied_conversations = _read_csv(conversation_review_csv)
    if {row["conversation_id"] for row in supplied_conversations} != multi_turn_ids:
        raise ValueError("Conversation review does not cover the multi-turn conversation set")
    behavior_by_turn = {row["turn_id"]: row["agent_behavior"] for row in component_review}
    response_by_turn = {row["turn_id"]: row["response_quality"] for row in component_review}
    critical_by_reviewed_turn = {
        row["turn_id"]: any(
            _is_true(row[field])
            for field in ("agent_behavior_critical", "response_quality_critical")
        )
        for row in component_review
    }
    critical_notes_by_turn = {
        row["turn_id"]: row["critical_issue_notes"] for row in component_review
    }
    conversation_review = []
    for supplied in supplied_conversations:
        conversation_id = supplied["conversation_id"]
        turn_ids = turns_by_conversation[conversation_id]
        derived_behavior = max(
            (behavior_by_turn[turn_id] for turn_id in turn_ids),
            key=lambda grade: GRADE_ORDER[grade],
        )
        derived_response = max(
            (response_by_turn[turn_id] for turn_id in turn_ids),
            key=lambda grade: GRADE_ORDER[grade],
        )
        critical_turns = [turn_id for turn_id in turn_ids if critical_by_reviewed_turn[turn_id]]
        if supplied["agent_behavior"] != derived_behavior:
            raise ValueError(
                f"Conversation grade for {conversation_id} is {supplied['agent_behavior']}; "
                f"expected {derived_behavior} from its least favorable turn"
            )
        conversation_review.append(
            {
                "conversation_id": conversation_id,
                "turn_count": len(turn_ids),
                "agent_behavior": derived_behavior,
                "response_quality": derived_response,
                "critical_issue": str(bool(critical_turns)).lower(),
                "turn_ids": ";".join(turn_ids),
                "critical_issue_notes": " | ".join(
                    f"{turn_id}: {critical_notes_by_turn[turn_id]}" for turn_id in critical_turns
                ),
                "reviewer_notes": supplied["reviewer_notes"],
            }
        )

    run_summary = summarize(records)
    if raw_summary != run_summary:
        raise ValueError("Stored run summary does not match results.jsonl")
    run_snapshot = _run_snapshot(manifest, run_summary)
    _write_json(output_dir / "run.json", run_snapshot)

    sanitized_records = []
    for record in records:
        sanitized = _sanitized_result(record, "candidate")
        sanitized.pop("side", None)
        sanitized_records.append(sanitized)
    (output_dir / "responses.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in sanitized_records),
        encoding="utf-8",
    )
    _write_csv(output_dir / "component-review.csv", component_review, COMPONENT_FIELDS)
    _write_csv(output_dir / "conversation-review.csv", conversation_review, CONVERSATION_FIELDS)
    shutil.copyfile(corpus, output_dir / "questions.yaml")

    summary = {
        "schema_version": SCHEMA_VERSION,
        "release": release,
        "conversations": len(turns_by_conversation),
        "turns": len(records),
        "operational": run_summary,
        "qualitative_review": {
            "turns_reviewed": len(component_review),
            "agent_behavior": _grade_counts(component_review, "agent_behavior"),
            "retrieval_quality": _grade_counts(component_review, "retrieval_quality"),
            "response_quality": _grade_counts(component_review, "response_quality"),
            "disposition": _grade_counts(component_review, "disposition"),
            "critical_issues": {
                field.removesuffix("_critical"): sum(
                    _is_true(row[field]) for row in component_review
                )
                for field in CRITICAL_FIELDS
            },
        },
        "multi_turn_conversations": {
            "reviewed": len(conversation_review),
            "turns": sum(int(row["turn_count"]) for row in conversation_review),
            "agent_behavior": _grade_counts(conversation_review, "agent_behavior"),
            "response_quality": _grade_counts(conversation_review, "response_quality"),
            "critical_issues": sum(_is_true(row["critical_issue"]) for row in conversation_review),
            "grade_method": "least favorable per-turn grade for each reported component",
        },
    }
    _write_json(output_dir / "summary.json", summary)

    release_metadata = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_kind": "standalone",
        "release": release,
        "released_on": released_on,
        "snapshot_policy": "sanitized_committed_release_validation",
        "candidate": {
            "agent_id": manifest.get("agent_id"),
            "candidate": manifest.get("candidate"),
            "source_ref": manifest.get("source_ref"),
            "source_revision": manifest.get("source_revision"),
        },
        "corpus": {
            "file": "questions.yaml",
            "name": yaml.safe_load(corpus.read_text(encoding="utf-8")).get("name"),
            "sha256": _sha256(corpus),
        },
        "review": {
            "rubric": "../../QUALITATIVE_GRADING_RUBRIC.md",
            "reviewed_at": released_on,
            "method": "turn-by-turn rubric review with multi-turn conversation rollup",
            "conversation_grade_method": summary["multi_turn_conversations"]["grade_method"],
            "critical_issue_definition": (
                "Severity flag within Misses for materially wrong or fabricated conclusions, "
                "evidence that directly contradicts the answer, wrong-plan answers, or meaningful "
                "policy-boundary bypasses; it is not an additional mutually exclusive grade."
            ),
            "reference_answers_are_exact_oracles": False,
        },
        "raw_artifacts": {
            "results_sha256": _sha256(run_dir / "results.jsonl"),
            "source_review_sha256": _sha256(review_csv),
            "source_critical_review_sha256": _sha256(critical_review_csv),
            "source_conversation_review_sha256": _sha256(conversation_review_csv),
            "storage": "local_gitignored",
        },
    }
    (output_dir / "release.yaml").write_text(
        yaml.safe_dump(release_metadata, sort_keys=False), encoding="utf-8"
    )

    committed = [name for name in REQUIRED_FILES if name != "checksums.sha256"]
    (output_dir / "checksums.sha256").write_text(
        "".join(f"{_sha256(output_dir / name)}  {name}\n" for name in committed),
        encoding="utf-8",
    )
    validate_snapshot(output_dir)


def validate_snapshot(snapshot_dir: Path) -> None:
    missing = [name for name in REQUIRED_FILES if not (snapshot_dir / name).is_file()]
    if missing:
        raise ValueError(f"Snapshot is missing required files: {', '.join(missing)}")

    expected_hashes = {}
    for line in (snapshot_dir / "checksums.sha256").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if name in expected_hashes:
            raise ValueError(f"Duplicate checksum entry: {name}")
        expected_hashes[name] = digest
    required_checksum_names = set(REQUIRED_FILES) - {"checksums.sha256"}
    if set(expected_hashes) != required_checksum_names:
        raise ValueError("Checksum manifest coverage mismatch")
    for name, expected in expected_hashes.items():
        actual = _sha256(snapshot_dir / name)
        if actual != expected:
            raise ValueError(f"Checksum mismatch for {name}: {actual} != {expected}")

    metadata = yaml.safe_load((snapshot_dir / "release.yaml").read_text(encoding="utf-8"))
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported standalone release snapshot schema")
    if metadata.get("snapshot_kind") != "standalone":
        raise ValueError("Expected a standalone release snapshot")
    if metadata["corpus"]["sha256"] != _sha256(snapshot_dir / "questions.yaml"):
        raise ValueError("Frozen corpus checksum does not match release metadata")

    run = _read_json(snapshot_dir / "run.json")
    responses = _read_jsonl(snapshot_dir / "responses.jsonl")
    if len(responses) != run.get("turn_count_per_trial"):
        raise ValueError("Response count does not match the frozen run")
    for row in responses:
        forbidden = _forbidden_paths(row)
        if forbidden:
            raise ValueError(f"Unsanitized response fields for {row.get('turn_id')}: {forbidden}")

    corpus = yaml.safe_load((snapshot_dir / "questions.yaml").read_text(encoding="utf-8"))
    corpus_conversations = corpus["conversations"]
    corpus_conversation_ids = {str(item["id"]) for item in corpus_conversations}
    corpus_turn_ids = {
        str(turn["id"]) for conversation in corpus_conversations for turn in conversation["turns"]
    }
    response_ids = {str(row["turn_id"]) for row in responses}
    response_conversation_ids = {str(row["conversation_id"]) for row in responses}
    if corpus_turn_ids != response_ids:
        raise ValueError("Frozen corpus turn IDs do not match response snapshot")
    if corpus_conversation_ids != response_conversation_ids:
        raise ValueError("Frozen corpus conversation IDs do not match response snapshot")
    if run.get("corpus_sha256") != metadata["corpus"]["sha256"]:
        raise ValueError("Run corpus checksum does not match frozen corpus")

    component_review = _read_csv(snapshot_dir / "component-review.csv")
    if {row["turn_id"] for row in component_review} != response_ids:
        raise ValueError("Component review turn IDs do not match response snapshot")
    response_conversation_by_turn = {
        str(row["turn_id"]): str(row["conversation_id"]) for row in responses
    }
    if any(
        row["conversation_id"] != response_conversation_by_turn[row["turn_id"]]
        for row in component_review
    ):
        raise ValueError("Component review conversation IDs do not match responses")
    for row in component_review:
        for critical_field, grade_field in (
            ("agent_behavior_critical", "agent_behavior"),
            ("retrieval_quality_critical", "retrieval_quality"),
            ("response_quality_critical", "response_quality"),
        ):
            if _is_true(row[critical_field]) and row[grade_field] != "Misses":
                raise ValueError(f"Critical issue is not within Misses: {row['turn_id']}")
    conversation_review = _read_csv(snapshot_dir / "conversation-review.csv")
    response_conversations = Counter(str(row["conversation_id"]) for row in responses)
    multi_turn_ids = {key for key, count in response_conversations.items() if count > 1}
    if {row["conversation_id"] for row in conversation_review} != multi_turn_ids:
        raise ValueError("Conversation review IDs do not match multi-turn responses")
    behavior_by_turn = {row["turn_id"]: row["agent_behavior"] for row in component_review}
    response_by_turn = {row["turn_id"]: row["response_quality"] for row in component_review}
    turn_ids_by_conversation: dict[str, list[str]] = {}
    for row in responses:
        turn_ids_by_conversation.setdefault(str(row["conversation_id"]), []).append(
            str(row["turn_id"])
        )
    for row in conversation_review:
        turn_ids = turn_ids_by_conversation[row["conversation_id"]]
        expected_behavior = max(
            (behavior_by_turn[turn_id] for turn_id in turn_ids),
            key=lambda grade: GRADE_ORDER[grade],
        )
        expected_response = max(
            (response_by_turn[turn_id] for turn_id in turn_ids),
            key=lambda grade: GRADE_ORDER[grade],
        )
        if row["agent_behavior"] != expected_behavior:
            raise ValueError(
                f"Conversation behavior does not match turns: {row['conversation_id']}"
            )
        if row["response_quality"] != expected_response:
            raise ValueError(
                f"Conversation response grade does not match: {row['conversation_id']}"
            )
        expected_critical = any(
            any(
                _is_true(component[field])
                for field in ("agent_behavior_critical", "response_quality_critical")
            )
            for component in component_review
            if component["turn_id"] in turn_ids
        )
        if _is_true(row["critical_issue"]) is not expected_critical:
            raise ValueError(f"Conversation critical flag does not match: {row['conversation_id']}")
        if int(row["turn_count"]) != len(turn_ids):
            raise ValueError(f"Conversation turn count does not match: {row['conversation_id']}")
        if row["turn_ids"].split(";") != turn_ids:
            raise ValueError(f"Conversation turn IDs do not match: {row['conversation_id']}")

    summary = _read_json(snapshot_dir / "summary.json")
    if summary.get("turns") != len(responses):
        raise ValueError("Summary turn count does not match responses")
    if summary.get("conversations") != len(response_conversations):
        raise ValueError("Summary conversation count does not match responses")
    if summary.get("operational") != run.get("summary"):
        raise ValueError("Summary operational metrics do not match the frozen run")
    qualitative = summary["qualitative_review"]
    for field in ("agent_behavior", "retrieval_quality", "response_quality", "disposition"):
        if qualitative[field] != _grade_counts(component_review, field):
            raise ValueError(f"Summary {field} counts do not match component review")
    expected_critical_counts = {
        field.removesuffix("_critical"): sum(_is_true(row[field]) for row in component_review)
        for field in CRITICAL_FIELDS
    }
    if qualitative["critical_issues"] != expected_critical_counts:
        raise ValueError("Summary critical-issue counts do not match component review")
    multi_turn = summary["multi_turn_conversations"]
    if multi_turn["reviewed"] != len(conversation_review):
        raise ValueError("Multi-turn review count does not match conversation review")
    if multi_turn["agent_behavior"] != _grade_counts(conversation_review, "agent_behavior"):
        raise ValueError("Multi-turn grade counts do not match conversation review")
    if multi_turn["response_quality"] != _grade_counts(conversation_review, "response_quality"):
        raise ValueError("Multi-turn response counts do not match conversation review")
    if multi_turn["critical_issues"] != sum(
        _is_true(row["critical_issue"]) for row in conversation_review
    ):
        raise ValueError("Multi-turn critical issues do not match conversation review")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="Freeze one standalone release evaluation")
    create.add_argument("--release", required=True)
    create.add_argument("--released-on", default=str(date.today()))
    create.add_argument("--run", required=True, type=Path)
    create.add_argument("--review", required=True, type=Path)
    create.add_argument("--critical-review", required=True, type=Path)
    create.add_argument("--conversation-review", required=True, type=Path)
    create.add_argument("--corpus", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--force", action="store_true")
    validate = subparsers.add_parser("validate", help="Validate a standalone snapshot")
    validate.add_argument("--snapshot", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "create":
        create_snapshot(
            release=args.release,
            released_on=args.released_on,
            run_dir=args.run,
            review_csv=args.review,
            critical_review_csv=args.critical_review,
            conversation_review_csv=args.conversation_review,
            corpus=args.corpus,
            output_dir=args.output,
            force=args.force,
        )
    else:
        validate_snapshot(args.snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
