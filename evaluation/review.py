"""Create a reviewer-ready copy of a single-candidate evaluation CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

RECORD_KEY = ("Trial", "Conversation ID", "Turn ID")
REVIEW_COLUMNS = (
    "Reviewer Name",
    "Agent Behavior (Meets/Mixed/Misses)",
    "Retrieval Quality (Meets/Mixed/Misses/N/A)",
    "Final Response Quality (Meets/Mixed/Misses)",
    "Response Miss Safety (Safe/Unsafe)",
    "Disposition (Accept/Follow-up/Defect)",
    "Reviewer Notes",
)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def _record_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (
        str(row.get("Trial", "")),
        str(row.get("Conversation ID", "")),
        str(row.get("Turn ID", "")),
    )


def prepare_review(results_path: Path, output_path: Path) -> tuple[int, int]:
    """Write current generated fields and preserve prior human review entries by turn ID."""
    source_fields, source_rows = _read_csv(results_path)
    missing = [field for field in RECORD_KEY if field not in source_fields]
    if missing:
        raise ValueError(f"Results CSV is missing record keys: {', '.join(missing)}")

    prior_reviews: dict[tuple[str, str, str], dict[str, str]] = {}
    if output_path.exists():
        prior_fields, prior_rows = _read_csv(output_path)
        if all(field in prior_fields for field in (*RECORD_KEY, *REVIEW_COLUMNS)):
            prior_reviews = {_record_key(row): row for row in prior_rows}

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(f"{output_path.suffix}.tmp")
    fields = [*source_fields, *REVIEW_COLUMNS]
    preserved = 0
    with temporary_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for source_row in source_rows:
            prior = prior_reviews.get(_record_key(source_row), {})
            review = {field: prior.get(field, "") for field in REVIEW_COLUMNS}
            if any(review.values()):
                preserved += 1
            writer.writerow({**source_row, **review})
    temporary_path.replace(output_path)
    return len(source_rows), preserved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    rows, preserved = prepare_review(args.results.resolve(), args.output.resolve())
    print(f"Review workbook: {args.output.resolve()} ({rows} turns, {preserved} reviews preserved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
