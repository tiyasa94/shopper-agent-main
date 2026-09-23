from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/run-local-rag-mode-experiments.sh"


def test_rag_mode_experiment_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_rag_mode_experiment_script_runs_all_isolated_modes() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "for mode in disabled fused protected" in source
    assert 'RETRIEVAL_PLAN_SUMMARY_MODE="${mode}"' in source
    assert "RETRIEVAL_PLAN_SUMMARY_MODE=protected" in source
    assert "evaluation retrieval run" in source
    assert "evaluation retrieval compare" in source
