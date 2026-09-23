"""Contract tests for committed release-evaluation snapshots."""

import shutil
from pathlib import Path

import pytest
from evaluation.snapshots.paired import (
    _read_json,
    _read_jsonl,
    materialize_candidate,
    validate_snapshot,
)
from evaluation.snapshots.standalone import (
    validate_snapshot as validate_standalone_snapshot,
)

ROOT = Path(__file__).resolve().parents[2]
RC3_SNAPSHOT = ROOT / "tests/evaluation/release_snapshots/rc3.0"
RC31_SNAPSHOT = ROOT / "tests/evaluation/release_snapshots/rc3.1"


def test_rc3_release_snapshot_is_complete_and_consistent() -> None:
    validate_snapshot(RC3_SNAPSHOT)


def test_rc31_release_snapshot_is_complete_and_consistent() -> None:
    validate_standalone_snapshot(RC31_SNAPSHOT)


def test_release_snapshot_requires_checksum_for_every_artifact(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    shutil.copytree(RC3_SNAPSHOT, snapshot)
    checksum_path = snapshot / "checksums.sha256"
    checksum_path.write_text(
        "\n".join(
            line
            for line in checksum_path.read_text(encoding="utf-8").splitlines()
            if not line.endswith("  candidate-run.json")
        )
        + "\n",
        encoding="utf-8",
    )
    candidate_path = snapshot / "candidate-run.json"
    candidate_path.write_text(candidate_path.read_text(encoding="utf-8") + " \n", encoding="utf-8")

    with pytest.raises(ValueError, match="Checksum manifest coverage mismatch"):
        validate_snapshot(snapshot)


def test_rc3_release_snapshot_excludes_provider_correlation_and_runtime_state() -> None:
    records = _read_jsonl(RC3_SNAPSHOT / "responses.jsonl")

    assert len(records) == 266
    forbidden = {"run_id", "submitted_run_id", "request_id", "trace_id", "context"}
    assert all(not forbidden.intersection(record) for record in records)
    assert all("response_text" in record for record in records)


def test_rc3_release_snapshot_can_materialize_cached_baseline(tmp_path: Path) -> None:
    output = tmp_path / "baseline"

    materialize_candidate(RC3_SNAPSHOT, "candidate", output)

    manifest = _read_json(output / "manifest.json")
    records = _read_jsonl(output / "results.jsonl")
    assert manifest["agent_id"] == "65b0ad24-4e50-471e-979f-e10b60055ff9"
    assert manifest["turn_count_per_trial"] == 133
    assert len(records) == 133
    assert all(record["result"]["run_id"] == "" for record in records)
