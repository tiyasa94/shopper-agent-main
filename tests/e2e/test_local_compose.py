"""Static contracts for the Podman Compose local-development stack."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _manifest(path: Path, *, value: str) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(f'[config_map]\nENVIRONMENT = "local"\nVALUE = "{value}"\n')


def test_compose_stack_keeps_wxo_external_and_persists_postgres() -> None:
    compose = (ROOT / "compose.local.yaml").read_text()

    assert "postgres:" in compose
    assert "rag-api:" in compose
    assert "shopper-api:" in compose
    assert "wxo:" not in compose
    assert "shopper-postgres-data:/var/lib/postgresql" in compose
    assert "POSTGRES_HOST_AUTH_METHOD" not in compose
    assert "${LOCAL_RAG_BIND_ADDRESS:-0.0.0.0}" in compose
    assert "${LOCAL_SHOPPER_API_PORT:-8082}" in compose
    assert "python -m app.jobs.schema_apply" in compose

    runner = (ROOT / "scripts" / "run-local-e2e.sh").read_text()
    assert "Local Shopper Assistant API is not ready" in runner
    assert "./scripts/local-platform.sh start" in runner


def test_local_e2e_runner_has_one_explicit_profile_matrix() -> None:
    runner = (ROOT / "scripts" / "run-local-e2e.sh").read_text()

    for profile in ("smoke", "full", "wxo"):
        assert f"  {profile})" in runner
    for legacy_profile in ("direct)", "public)", "fixture)", "local-rag)", "draft-rag)"):
        assert legacy_profile not in runner

    assert "run_workflows smoke" in runner
    assert "run_workflows full" in runner
    assert "test_local_shopper_api_workflows.py" in runner
    assert "test_wxo_diagnostic.py" in runner
    assert "rag_fixture" not in runner
    assert "run_rag_contract" not in runner
    assert "--live-e2e" in runner

    env_example = (ROOT / ".env.local.example").read_text()
    assert "RAG_API_KEY=" not in env_example


def test_runtime_override_merges_secrets_and_container_routes(tmp_path: Path) -> None:
    platform = tmp_path / "shopper-platform"
    rag = platform / "apps" / "rag-api"
    shopper = platform / "apps" / "shopper-assistant-api"
    _manifest(rag / "deployment/environments/local/manifest.toml", value="rag-manifest")
    _manifest(
        shopper / "deployment/environments/local/manifest.toml",
        value="shopper-manifest",
    )
    rag_secrets = tmp_path / "rag.env"
    rag_secrets.write_text(
        "VALUE=secret-must-lose\nMILVUS_PASSWORD=milvus-secret\n"
        'CLIENT_API_KEYS_JSON={"shopper-assistant-api":"shopper-key"}\n'
    )
    shopper_secrets = tmp_path / "shopper.env"
    shopper_secrets.write_text("WXO_API_KEY=unused-cloud-key\nWATSONX_AI_API_KEY=summary-secret\n")
    agent_secrets = tmp_path / "agent.env"
    agent_secrets.write_text("RAG_API_KEY=agent-rag-key\n")
    output = tmp_path / "runtime.json"

    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "render-local-compose.py"),
            "--platform-root",
            str(platform),
            "--rag-secrets",
            str(rag_secrets),
            "--shopper-secrets",
            str(shopper_secrets),
            "--agent-secrets",
            str(agent_secrets),
            "--agent-id",
            "local-agent-id",
            "--output",
            str(output),
        ],
        check=True,
        env={**os.environ, "RETRIEVAL_PLAN_SUMMARY_MODE": "fused"},
    )

    payload = json.loads(output.read_text())
    rag_environment = payload["services"]["rag-api"]["environment"]
    shopper_environment = payload["services"]["shopper-api"]["environment"]
    assert rag_environment["VALUE"] == "rag-manifest"
    assert rag_environment["MILVUS_PASSWORD"] == "milvus-secret"
    assert rag_environment["RETRIEVAL_PLAN_SUMMARY_MODE"] == "fused"
    assert json.loads(rag_environment["CLIENT_API_KEYS_JSON"]) == {
        "shopper-agent": "agent-rag-key",
        "shopper-assistant-api": "shopper-key",
    }
    assert "RAG_API_KEY" not in rag_environment
    assert shopper_environment["VALUE"] == "shopper-manifest"
    assert shopper_environment["WXO_AGENT_ID"] == "local-agent-id"
    assert shopper_environment["WXO_API_URL"] == "http://host.containers.internal:4321"
    assert shopper_environment["WXO_AUTH_TYPE"] == "local_pw"
    assert shopper_environment["DATABASE_URL"].endswith("@postgres:5432/shopper_assistant")
    assert "WXO_API_KEY" not in shopper_environment
    assert stat.S_IMODE(output.stat().st_mode) == 0o600

    agent_secrets.write_text("RAG_API_KEY=shopper-key\n")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "render-local-compose.py"),
            "--platform-root",
            str(platform),
            "--rag-secrets",
            str(rag_secrets),
            "--shopper-secrets",
            str(shopper_secrets),
            "--agent-secrets",
            str(agent_secrets),
            "--agent-id",
            "local-agent-id",
            "--output",
            str(output),
        ],
        check=True,
    )
    rag_environment = json.loads(output.read_text())["services"]["rag-api"]["environment"]
    assert json.loads(rag_environment["CLIENT_API_KEYS_JSON"]) == {
        "shopper-assistant-api": "shopper-key"
    }
