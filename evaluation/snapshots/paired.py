"""Freeze and validate sanitized, reusable release-evaluation snapshots.

Exploratory evaluation artifacts remain gitignored. This module promotes one completed paired
evaluation into a compact snapshot that is safe to review and reuse as a future baseline. It
intentionally excludes authentication material, WXO run/request/trace identifiers, runtime
contexts, and internal tool responses.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from evaluation.behavioral import summarize
from evaluation.io import read_json as _read_json
from evaluation.io import read_jsonl as _read_jsonl
from evaluation.io import sha256 as _sha256
from evaluation.io import write_json as _write_json

SCHEMA_VERSION = 1
SIDES = ("baseline", "candidate")
GRADE_FIELDS = ("agent_behavior", "retrieval_quality", "response_quality")
OBSERVATION_FIELDS = (
    "route",
    # Retained only to read historical RC3.0 artifacts produced by the removed
    # flow-based evaluator. New evaluations emit ``route``.
    "flow_type",
    "business_intent",
    "rag_called",
    "rag_status",
    "rag_evidence_count",
    "plans_found",
    "plans_searched_for",
    "rag_context_excerpt",
    "audit_called",
    "audit_identification",
    "audit_type",
    "sensitive_value_echoed",
    "serialized_envelope_exposed",
    "transport_attempts",
)
REQUIRED_FILES = (
    "release.yaml",
    "comparison-summary.json",
    "baseline-run.json",
    "candidate-run.json",
    "responses.jsonl",
    "component-review.csv",
    "checksums.sha256",
)


def _release_safe(value: Any) -> Any:
    """Normalize nested public data and remove pointers to ignored raw files."""
    if isinstance(value, dict):
        return {str(key): _release_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_release_safe(item) for item in value]
    if isinstance(value, str):
        return value.replace(
            "… [truncated; full value in results.jsonl]",
            "… [truncated in committed release snapshot]",
        )
    return value


def _run_snapshot(manifest: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    """Keep reproducibility fields and aggregate outcomes, not provider correlation IDs."""
    return {
        "schema_version": SCHEMA_VERSION,
        "agent_id": manifest.get("agent_id"),
        "agent_name": manifest.get("agent_name"),
        "candidate": manifest.get("candidate"),
        "completed_at": manifest.get("completed_at"),
        "conversation_count": manifest.get("conversation_count"),
        "conversation_ids": manifest.get("conversation_ids", []),
        "corpus_sha256": manifest.get("corpus_sha256"),
        "python": manifest.get("python"),
        "rag_base_url": manifest.get("rag_base_url"),
        "schema_version_source": manifest.get("schema_version"),
        "source_fingerprint": manifest.get("source_fingerprint"),
        "source_ref": manifest.get("source_ref"),
        "source_revision": manifest.get("source_revision"),
        "started_at": manifest.get("started_at"),
        "suite": manifest.get("suite"),
        "statuses": manifest.get("statuses", []),
        "summary": summary,
        "transport": manifest.get("transport"),
        "trials": manifest.get("trials"),
        "turn_count_per_trial": manifest.get("turn_count_per_trial"),
    }


def _sanitized_result(record: dict[str, Any], side: str) -> dict[str, Any]:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    observed = record.get("observations") if isinstance(record.get("observations"), dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "side": side,
        "candidate": record.get("candidate"),
        "turn_id": record.get("turn_id"),
        "conversation_id": record.get("conversation_id"),
        "trial": record.get("trial"),
        "market": record.get("market"),
        "context_name": record.get("context_name"),
        "current_plan": _release_safe(record.get("current_plan")),
        "available_plans": _release_safe(record.get("available_plans")),
        "question": record.get("question"),
        "reference_answer": record.get("reference_answer"),
        "expected_behavior": record.get("expected_behavior"),
        "expected_route": record.get("expected_route"),
        "expected_plan_ids": _release_safe(record.get("expected_plan_ids", [])),
        "source_intent": record.get("source_intent"),
        "source_rag_expected": record.get("source_rag_expected"),
        "status": record.get("status"),
        "error": record.get("error"),
        "duration_seconds": result.get("duration_seconds"),
        "response_text": result.get("response_text"),
        "observations": _release_safe({key: observed.get(key) for key in OBSERVATION_FIELDS}),
    }


def _component_note(
    component: str,
    grade: str,
    final_note: str,
    observation: str = "",
    evidence_count: int = 0,
) -> str:
    if component == "agent_behavior":
        if grade == "Meets":
            return "The desired route, guardrail, and authoritative context behavior was observed."
        prefix = "A meaningful routing or context-use gap remained."
        if grade == "Misses":
            prefix = "A material routing or context-selection failure was observed."
        return f"{prefix} {final_note}".strip()
    if component == "retrieval_quality":
        if grade == "Not graded":
            return f"Retrieval quality was not graded: {observation}."
        count = f"{evidence_count} public evidence item{'s' if evidence_count != 1 else ''}"
        if grade == "Meets":
            return f"{count} were relevant, plan-correct, and sufficient under the rubric."
        if grade == "Mixed":
            return (
                f"{count} were useful but materially incomplete, noisy, or conflicting. "
                f"{final_note}"
            )
        return f"{count} did not adequately support the central requested information. {final_note}"
    return final_note


def _enrich_review(
    source: Path,
    baseline: dict[str, dict[str, Any]],
    candidate: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    with source.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if rows and "before_agent_behavior_notes" in rows[0]:
        return rows
    enriched = []
    for row in rows:
        turn_id = row["turn_id"]
        before_record = baseline[turn_id]
        candidate_record = candidate[turn_id]
        before_observed = before_record.get("observations", {})
        candidate_observed = candidate_record.get("observations", {})
        before_final_note = row["before_reviewer_notes"]
        candidate_final_note = row["rc3_reviewer_notes"]
        item = {
            "turn_id": turn_id,
            "before_agent_behavior": row["before_agent_behavior"],
            "before_agent_behavior_notes": _component_note(
                "agent_behavior", row["before_agent_behavior"], before_final_note
            ),
            "rc3_agent_behavior": row["rc3_agent_behavior"],
            "rc3_agent_behavior_notes": _component_note(
                "agent_behavior", row["rc3_agent_behavior"], candidate_final_note
            ),
            "before_retrieval_quality": row["before_retrieval_quality"],
            "before_retrieval_quality_notes": _component_note(
                "retrieval_quality",
                row["before_retrieval_quality"],
                before_final_note,
                row["before_retrieval_observation"],
                int(before_observed.get("rag_evidence_count") or 0),
            ),
            "rc3_retrieval_quality": row["rc3_retrieval_quality"],
            "rc3_retrieval_quality_notes": _component_note(
                "retrieval_quality",
                row["rc3_retrieval_quality"],
                candidate_final_note,
                row["rc3_retrieval_observation"],
                int(candidate_observed.get("rag_evidence_count") or 0),
            ),
            "before_response_quality": row["before_response_quality"],
            "before_response_quality_notes": before_final_note,
            "rc3_response_quality": row["rc3_response_quality"],
            "rc3_response_quality_notes": candidate_final_note,
            "before_retrieval_observation": row["before_retrieval_observation"],
            "rc3_retrieval_observation": row["rc3_retrieval_observation"],
            "before_business_intent_present": row["before_business_intent_present"],
            "rc3_business_intent_present": row["rc3_business_intent_present"],
            "before_agent_identity_present": row["before_agent_identity_present"],
            "rc3_agent_identity_present": row["rc3_agent_identity_present"],
            "before_audit_present": row["before_audit_present"],
            "rc3_audit_present": row["rc3_audit_present"],
        }
        enriched.append(item)
    return enriched


def _counts(rows: list[dict[str, Any]], prefix: str, field: str) -> dict[str, int]:
    return dict(Counter(str(row[f"{prefix}_{field}"]) for row in rows))


def create_snapshot(
    *,
    release: str,
    released_on: str,
    baseline_dir: Path,
    candidate_dir: Path,
    review_csv: Path,
    output_dir: Path,
    force: bool = False,
) -> None:
    if output_dir.exists() and any(output_dir.iterdir()) and not force:
        raise FileExistsError(f"Snapshot already exists: {output_dir}; pass --force to refresh")
    output_dir.mkdir(parents=True, exist_ok=True)

    baseline_manifest = _read_json(baseline_dir / "manifest.json")
    candidate_manifest = _read_json(candidate_dir / "manifest.json")
    baseline_records = _read_jsonl(baseline_dir / "results.jsonl")
    candidate_records = _read_jsonl(candidate_dir / "results.jsonl")
    baseline_by_turn = {str(row["turn_id"]): row for row in baseline_records}
    candidate_by_turn = {str(row["turn_id"]): row for row in candidate_records}
    if baseline_by_turn.keys() != candidate_by_turn.keys():
        raise ValueError("Baseline and candidate turn sets differ")

    review = _enrich_review(review_csv, baseline_by_turn, candidate_by_turn)
    review_ids = {row["turn_id"] for row in review}
    if review_ids != baseline_by_turn.keys():
        raise ValueError("Component review does not cover the paired turn set")

    baseline_summary = summarize(baseline_records)
    candidate_summary = summarize(candidate_records)
    _write_json(
        output_dir / "baseline-run.json",
        _run_snapshot(baseline_manifest, baseline_summary),
    )
    _write_json(
        output_dir / "candidate-run.json",
        _run_snapshot(candidate_manifest, candidate_summary),
    )

    response_records = [
        *(_sanitized_result(row, "baseline") for row in baseline_records),
        *(_sanitized_result(row, "candidate") for row in candidate_records),
    ]
    (output_dir / "responses.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in response_records),
        encoding="utf-8",
    )

    with (output_dir / "component-review.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(review[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(review)

    comparison = {
        "schema_version": SCHEMA_VERSION,
        "release": release,
        "turns_per_candidate": len(review),
        "baseline": {
            "candidate": baseline_manifest.get("candidate"),
            "summary": baseline_summary,
            **{field: _counts(review, "before", field) for field in GRADE_FIELDS},
            "retrieval_observability": _counts(review, "before", "retrieval_observation"),
        },
        "candidate": {
            "candidate": candidate_manifest.get("candidate"),
            "summary": candidate_summary,
            **{field: _counts(review, "rc3", field) for field in GRADE_FIELDS},
            "retrieval_observability": _counts(review, "rc3", "retrieval_observation"),
        },
    }
    _write_json(output_dir / "comparison-summary.json", comparison)

    raw_artifacts = {
        "baseline_results_sha256": _sha256(baseline_dir / "results.jsonl"),
        "candidate_results_sha256": _sha256(candidate_dir / "results.jsonl"),
        "source_review_sha256": _sha256(review_csv),
        "storage": "local_gitignored",
    }
    release_metadata = {
        "schema_version": SCHEMA_VERSION,
        "release": release,
        "released_on": released_on,
        "snapshot_policy": "sanitized_committed_release_baseline",
        "baseline": {
            "agent_id": baseline_manifest.get("agent_id"),
            "candidate": baseline_manifest.get("candidate"),
            "source_ref": baseline_manifest.get("source_ref"),
            "source_revision": baseline_manifest.get("source_revision"),
        },
        "candidate": {
            "agent_id": candidate_manifest.get("agent_id"),
            "candidate": candidate_manifest.get("candidate"),
            "source_ref": candidate_manifest.get("source_ref"),
            "source_revision": candidate_manifest.get("source_revision"),
            "source_revision_provenance": "evaluation_cli_argument",
        },
        "review": {
            "rubric": "../../QUALITATIVE_GRADING_RUBRIC.md",
            "rubric_sha256": _sha256(
                Path(__file__).resolve().parents[2]
                / "tests/evaluation/QUALITATIVE_GRADING_RUBRIC.md"
            ),
            "reviewer": "Qualitative rubric review",
            "reviewed_at": released_on,
            "blinded_candidate_labels": True,
            "reference_answers_are_exact_oracles": False,
        },
        "raw_artifacts": raw_artifacts,
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
    manifest_names = set(expected_hashes)
    if manifest_names != required_checksum_names:
        missing_checksums = sorted(required_checksum_names - manifest_names)
        unexpected_checksums = sorted(manifest_names - required_checksum_names)
        raise ValueError(
            "Checksum manifest coverage mismatch: "
            f"missing={missing_checksums}, unexpected={unexpected_checksums}"
        )
    for name in sorted(required_checksum_names):
        expected = expected_hashes[name]
        actual = _sha256(snapshot_dir / name)
        if actual != expected:
            raise ValueError(f"Checksum mismatch for {name}: {actual} != {expected}")

    metadata = yaml.safe_load((snapshot_dir / "release.yaml").read_text(encoding="utf-8"))
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported release snapshot schema")
    responses = _read_jsonl(snapshot_dir / "responses.jsonl")
    by_side = Counter(str(row.get("side")) for row in responses)
    if set(by_side) != set(SIDES) or len(set(by_side.values())) != 1:
        raise ValueError(f"Unpaired response snapshot: {dict(by_side)}")
    forbidden = {"run_id", "submitted_run_id", "request_id", "trace_id", "context"}
    for row in responses:
        if forbidden.intersection(row):
            raise ValueError(f"Unsanitized response keys for {row.get('turn_id')}")

    with (snapshot_dir / "component-review.csv").open(encoding="utf-8", newline="") as handle:
        review = list(csv.DictReader(handle))
    turn_count = next(iter(by_side.values()))
    if len(review) != turn_count:
        raise ValueError(f"Review has {len(review)} rows; expected {turn_count}")
    response_ids = {str(row["turn_id"]) for row in responses if row.get("side") == "candidate"}
    if {row["turn_id"] for row in review} != response_ids:
        raise ValueError("Review turn IDs do not match response snapshot")

    comparison = _read_json(snapshot_dir / "comparison-summary.json")
    if comparison.get("turns_per_candidate") != turn_count:
        raise ValueError("Comparison summary turn count does not match snapshot")
    for prefix, side in (("before", "baseline"), ("rc3", "candidate")):
        for field in GRADE_FIELDS:
            actual = _counts(review, prefix, field)
            if comparison[side].get(field) != actual:
                raise ValueError(f"Comparison {side} {field} counts do not match review")


def materialize_candidate(snapshot_dir: Path, side: str, output_dir: Path) -> None:
    """Export one frozen side in the existing agent-comparison candidate format."""
    if side not in SIDES:
        raise ValueError(f"Unknown snapshot side: {side}")
    validate_snapshot(snapshot_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Materialized candidate directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    run = _read_json(snapshot_dir / f"{side}-run.json")
    manifest = {
        "schema_version": run.get("schema_version_source", SCHEMA_VERSION),
        "agent_id": run.get("agent_id"),
        "agent_name": run.get("agent_name"),
        "candidate": run.get("candidate"),
        "completed_at": run.get("completed_at"),
        "conversation_count": run.get("conversation_count"),
        "conversation_ids": run.get("conversation_ids", []),
        "corpus_sha256": run.get("corpus_sha256"),
        "python": run.get("python"),
        "rag_base_url": run.get("rag_base_url"),
        "source_fingerprint": run.get("source_fingerprint"),
        "source_ref": run.get("source_ref"),
        "source_revision": run.get("source_revision"),
        "started_at": run.get("started_at"),
        "statuses": run.get("statuses", []),
        "suite": run.get("suite"),
        "summary": run.get("summary", {}),
        "transport": run.get("transport"),
        "trials": run.get("trials"),
        "turn_count_per_trial": run.get("turn_count_per_trial"),
    }
    _write_json(output_dir / "manifest.json", manifest)

    records = [
        row for row in _read_jsonl(snapshot_dir / "responses.jsonl") if row.get("side") == side
    ]
    materialized = []
    for row in records:
        materialized.append(
            {
                "schema_version": row.get("schema_version"),
                "candidate": row.get("candidate"),
                "turn_id": row.get("turn_id"),
                "conversation_id": row.get("conversation_id"),
                "trial": row.get("trial"),
                "market": row.get("market"),
                "context_name": row.get("context_name"),
                "current_plan": row.get("current_plan"),
                "available_plans": row.get("available_plans"),
                "question": row.get("question"),
                "reference_answer": row.get("reference_answer"),
                "expected_behavior": row.get("expected_behavior"),
                "expected_route": row.get("expected_route"),
                "expected_plan_ids": row.get("expected_plan_ids", []),
                "source_intent": row.get("source_intent"),
                "source_rag_expected": row.get("source_rag_expected"),
                "status": row.get("status"),
                "error": row.get("error"),
                "observations": row.get("observations", {}),
                "result": {
                    "submitted_run_id": "",
                    "run_id": "",
                    "duration_seconds": row.get("duration_seconds"),
                    "response_text": row.get("response_text"),
                    "request_context": {},
                    "context": {},
                    "tool_calls": [],
                    "tool_responses": [],
                },
            }
        )
    (output_dir / "results.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in materialized),
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create", help="Freeze one paired release evaluation")
    create.add_argument("--release", required=True)
    create.add_argument("--released-on", default=str(date.today()))
    create.add_argument("--baseline", required=True, type=Path)
    create.add_argument("--candidate", required=True, type=Path)
    create.add_argument("--review", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--force", action="store_true")
    validate = subparsers.add_parser("validate", help="Validate a committed release snapshot")
    validate.add_argument("--snapshot", required=True, type=Path)
    materialize = subparsers.add_parser(
        "materialize", help="Export one snapshot side for agent_comparison compare"
    )
    materialize.add_argument("--snapshot", required=True, type=Path)
    materialize.add_argument("--side", required=True, choices=SIDES)
    materialize.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "create":
        create_snapshot(
            release=args.release,
            released_on=args.released_on,
            baseline_dir=args.baseline,
            candidate_dir=args.candidate,
            review_csv=args.review,
            output_dir=args.output,
            force=args.force,
        )
    elif args.command == "validate":
        validate_snapshot(args.snapshot)
    else:
        materialize_candidate(args.snapshot, args.side, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
