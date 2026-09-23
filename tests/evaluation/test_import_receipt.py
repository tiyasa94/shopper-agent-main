"""Tests for credential-free WXO import provenance receipts."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/write_import_receipt.py"
SPEC = importlib.util.spec_from_file_location("write_import_receipt", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_import_receipt_records_source_identity_without_environment_values(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"

    receipt = MODULE.write_receipt(
        root=ROOT,
        target_environment="local",
        output=output,
    )

    assert receipt["target_environment"] == "local"
    assert receipt["agent_name"] == "Elevance_Health_Shopper_Portal"
    assert receipt["source_bundle_sha256"]
    assert "src/agent.yaml" in receipt["source_files"]
    assert "src/tools/guardrail_plugin.py" in receipt["source_files"]
    assert "deployments/cloud.toml" in receipt["source_files"]
    assert "deployments/draft/manifest.toml" in receipt["source_files"]
    assert "deployments/live/manifest.toml" in receipt["source_files"]
    serialized = output.read_text()
    assert "SERVICE_PASSWORD" not in serialized
    assert ".env.local" not in serialized


def test_cloud_receipt_records_environment_contract_and_live_transition(tmp_path: Path) -> None:
    before = {
        "agent_id": "agent-id",
        "environments": {
            "draft": {"id": "draft-id", "current_version": None},
            "live": {"id": "live-id", "current_version": 5},
        },
    }
    after = {
        **before,
        "environments": {
            **before["environments"],
            "live": {"id": "live-id", "current_version": 6},
        },
    }

    receipt = MODULE.write_receipt(
        root=ROOT,
        target_environment="live",
        output=tmp_path / "receipt.json",
        operation="live_promotion",
        before_snapshot=before,
        after_snapshot=after,
    )

    assert receipt["schema_version"] == 2
    assert receipt["agent_id"] == "agent-id"
    assert receipt["environments"]["live"]["id"] == "live-id"
    assert receipt["live_version_before"] == 5
    assert receipt["live_version_after"] == 6


def test_source_hashes_exclude_gitignored_bytecode_and_keep_untracked_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    source = root / "src/tools/new_tool.py"
    bytecode = root / "src/tools/__pycache__/deleted_tool.cpython-313.pyc"
    source.parent.mkdir(parents=True)
    bytecode.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    bytecode.write_bytes(b"stale bytecode")
    (root / ".gitignore").write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
    MODULE._git(root, "init")

    hashes = MODULE.source_hashes(root)

    assert "src/tools/new_tool.py" in hashes
    assert "src/tools/__pycache__/deleted_tool.cpython-313.pyc" not in hashes
