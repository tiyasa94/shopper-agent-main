from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/run-local-experiment-matrix.sh"


def test_experiment_matrix_pins_the_rc4_and_research_snapshots() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "4b3291e70aaebe0b8f2a0f83a3dd0d107ca3d25e" in source
    assert "2e1d73aaa083a1e1e2534ea62ce3acbfc0a627d4" in source
    assert "e1002633928d95aeaf91326283b8e8bf3ee5033e" in source
    assert "1bcf3d36854ee820a6f7723c33b69e389f0b7ee8" in source


def test_experiment_matrix_supports_crossed_revisions_and_reproducible_outputs() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "--candidate label,agent_rev,platform_rev[,rag_mode]" in source
    assert "materialize_candidate" in source
    assert "source-fingerprint" in source
    assert "validate-cache" in source
    assert "evaluation assertions" in source
    assert "evaluation behavioral compare" in source
    assert "Restoring the current local shopper stack" in source
    assert 'DEPLOY_RUNTIME_SECRETS_FILE="${AGENT_RUNTIME_SECRETS}"' in source
    assert 'DEPLOY_RUNTIME_SECRETS_FILE="${ROOT_DIR}/.env.local"' not in source
