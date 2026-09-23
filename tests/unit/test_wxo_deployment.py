"""Verify the real deployment manifests provision the new inference connection."""

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("preflight", ROOT / "deployments/preflight.py")
assert SPEC and SPEC.loader
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


@pytest.mark.parametrize("target", ["local", "draft", "live"])
def test_manifest_provisions_wxo_inference_without_watsonx_project(target, tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.env"
    secrets.write_text("RAG_API_KEY=test-rag-key\nWXO_API_KEY=test-wxo-key\n")
    output = tmp_path / "preflight.env"
    manifest = ROOT / f"deployments/{target}/manifest.toml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "preflight",
            str(manifest),
            str(secrets),
            str(output),
            str(ROOT / "deployments/cloud.toml"),
        ],
    )
    assert preflight.main() == 0
    generated = output.read_text()
    slot = "draft" if target == "local" else target
    assert f"elevance-wxo-inference {slot} WXO_API_KEY=test-wxo-key" in generated
    assert "WXO_MODEL_ID=watsonx/openai/gpt-oss-120b" in generated
    tenant = tomllib.loads((ROOT / "deployments/cloud.toml").read_text())["wxo_tenant"]
    assert f"WXO_API_URL={tenant['api_url']}" in generated
    assert "WATSONX_AI" not in generated
    spec_path = ROOT / (
        "src/wxo_connection.local.yaml" if target == "local" else "src/wxo_connection.yaml"
    )
    connection = yaml.safe_load(spec_path.read_text())
    assert connection["app_id"] == "elevance-wxo-inference"
    assert connection["environments"][slot]["type"] == "team"


def test_old_watsonx_secret_cannot_satisfy_new_inference_connection(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "RAG_API_KEY=test\nWATSONX_AI_API_KEY=old-key\nWATSONX_AI_PROJECT_ID=old-project\n"
    )
    output = tmp_path / "preflight.env"
    monkeypatch.setattr(
        sys,
        "argv",
        ["preflight", str(ROOT / "deployments/local/manifest.toml"), str(secrets), str(output)],
    )
    assert preflight.main() == 1
    assert not output.exists()


@pytest.mark.parametrize("target", ["local", "draft", "live"])
def test_addendum_plugin_is_registered_without_a_connection(target):
    manifest = tomllib.loads((ROOT / f"deployments/{target}/manifest.toml").read_text())
    assert manifest["tools"]["app_ids"]["addendum_plugin.py"] == ""
    assert (
        preflight.tool_registered_name(ROOT / "src/tools/addendum_plugin.py") == "shopper_addenda"
    )
    agent = yaml.safe_load((ROOT / "src/agent.yaml").read_text())
    assert agent["plugins"]["agent_post_invoke"] == [{"plugin_name": "shopper_addenda"}]
    assert "_response_addenda" in agent["context_variables"]


@pytest.mark.parametrize("target", ["local", "draft"])
def test_tool_import_loop_supports_connectionless_tools_under_system_bash(target, tmp_path):
    """Execute the real import loop with nounset and capture CLI arguments without deploying."""
    script = (ROOT / "deployments/deploy-agent.sh").read_text()
    import_loop = script.split('echo "Importing tools into', 1)[1].split(
        'echo "Importing agent into', 1
    )[0]
    import_loop = import_loop.split("\n", 1)[1]
    manifest = tomllib.loads((ROOT / f"deployments/{target}/manifest.toml").read_text())
    tools = manifest["tools"]["app_ids"]
    log = tmp_path / "import arguments"
    environment = {
        "PATH": "/usr/bin:/bin",
        "ORCHESTRATE_BIN": "record_import",
        "TOOLS_PACKAGE_DIR": "package with spaces",
        "TOOLS_REQUIREMENTS": "requirements with spaces.txt",
        "TOOL_COUNT": str(len(tools)),
        "IMPORT_LOG": str(log),
    }
    expected = []
    for index, (filename, app_id) in enumerate(tools.items()):
        tool_file = f"src/tools/{filename}"
        environment[f"TOOL_{index}"] = f"{tool_file} {app_id}"
        expected.extend(
            [
                "tools",
                "import",
                "-k",
                "python",
                "-f",
                tool_file,
                "-p",
                environment["TOOLS_PACKAGE_DIR"],
                "-r",
                environment["TOOLS_REQUIREMENTS"],
            ]
        )
        if app_id:
            expected.extend(["-a", app_id])
    subprocess.run(
        [
            "/bin/bash",
            "-euc",
            'record_import() { printf "%s\\0" "$@" >> "$IMPORT_LOG"; }\n' + import_loop,
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert log.read_bytes().decode().split("\0")[:-1] == expected
