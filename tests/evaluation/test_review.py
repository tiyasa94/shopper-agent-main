"""Tests for the client-facing review workbook generator."""

import csv
from pathlib import Path

import pytest
from evaluation.review import REVIEW_COLUMNS, prepare_review

SOURCE_FIELDS = ["Trial", "Conversation ID", "Turn ID", "Question", "Response"]


def _write_csv(path: Path, rows: list[dict[str, str]], fields: list[str] | None = None) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields or SOURCE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_prepare_review_adds_blank_review_columns(tmp_path: Path) -> None:
    results = tmp_path / "results.csv"
    review = tmp_path / "client-review.csv"
    _write_csv(
        results,
        [
            {
                "Trial": "1",
                "Conversation ID": "conversation-1",
                "Turn ID": "turn-1",
                "Question": "What is a copay?",
                "Response": "A copay is a fixed amount.",
            }
        ],
    )

    assert prepare_review(results, review) == (1, 0)
    row = _read_csv(review)[0]
    assert row["Question"] == "What is a copay?"
    assert all(row[field] == "" for field in REVIEW_COLUMNS)


def test_prepare_review_refreshes_results_and_preserves_existing_review(tmp_path: Path) -> None:
    results = tmp_path / "results.csv"
    review = tmp_path / "client-review.csv"
    source = {
        "Trial": "1",
        "Conversation ID": "conversation-1",
        "Turn ID": "turn-1",
        "Question": "Question",
        "Response": "Original response",
    }
    _write_csv(results, [source])
    prepare_review(results, review)
    reviewed = _read_csv(review)[0]
    reviewed["Reviewer Name"] = "Reviewer"
    reviewed["Final Response Quality (Meets/Mixed/Misses)"] = "Misses"
    reviewed["Response Miss Safety (Safe/Unsafe)"] = "Unsafe"
    reviewed["Reviewer Notes"] = "Grounded and complete."
    _write_csv(review, [reviewed], [*SOURCE_FIELDS, *REVIEW_COLUMNS])

    _write_csv(results, [{**source, "Response": "Refreshed response"}])
    assert prepare_review(results, review) == (1, 1)

    row = _read_csv(review)[0]
    assert row["Response"] == "Refreshed response"
    assert row["Reviewer Name"] == "Reviewer"
    assert row["Response Miss Safety (Safe/Unsafe)"] == "Unsafe"
    assert row["Reviewer Notes"] == "Grounded and complete."


def test_prepare_review_rejects_results_without_stable_turn_key(tmp_path: Path) -> None:
    results = tmp_path / "results.csv"
    _write_csv(results, [{"Question": "Question"}], ["Question"])

    with pytest.raises(ValueError, match="record keys"):
        prepare_review(results, tmp_path / "client-review.csv")
