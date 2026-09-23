"""Contract checks for the client-facing stage runner and documentation."""

from pathlib import Path

ROOT = Path(__file__).parents[2]
RUNNER = (ROOT / "scripts/run-stage-client-test.sh").read_text()


def test_stage_runner_targets_only_the_public_stage_api() -> None:
    assert "elv-d2c-shopper-assistant-api-stage" in RUNNER
    assert 'payload.get("environment") != "stage"' in RUNNER
    assert "--transport shopper-api" in RUNNER
    assert "--allow-remote" in RUNNER
    assert "--concurrency 1" in RUNNER
    assert "stream_run" not in RUNNER
    assert "orchestrate" not in RUNNER


def test_stage_runner_handles_the_client_key_without_loading_repo_secrets() -> None:
    assert "SHOPPER_STAGE_API_KEY" in RUNNER
    assert "read -r -s" in RUNNER
    assert "runtime-secrets.env" not in RUNNER
    assert "source .env" not in RUNNER


def test_stage_runner_bundles_the_grading_rubric_with_the_artifacts() -> None:
    assert 'tests/evaluation/QUALITATIVE_GRADING_RUBRIC.md"' in RUNNER
    assert '"${OUTPUT_DIR}/QUALITATIVE_GRADING_RUBRIC.md"' in RUNNER
