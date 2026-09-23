#!/usr/bin/env python3
"""Write a credential-free receipt for one completed WXO component import."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml

INCLUDED_ROOTS = ("src",)
INCLUDED_FILES = (
    "pyproject.toml",
    "uv.lock",
    "deployments/cloud.toml",
    "deployments/deploy-agent.sh",
    "deployments/draft/manifest.toml",
    "deployments/live/manifest.toml",
    "deployments/promote-to-live.sh",
    "deployments/preflight.py",
    "scripts/wxo_environment_contract.py",
    "scripts/write_import_receipt.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def source_hashes(root: Path) -> dict[str, str]:
    files = {root / name for name in INCLUDED_FILES}
    source_names = _git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        *INCLUDED_ROOTS,
    ).splitlines()
    files.update(root / name for name in source_names)
    return {
        path.relative_to(root).as_posix(): _sha256(path) for path in sorted(files) if path.is_file()
    }


def _load_snapshot(path: Path | None) -> dict | None:
    if path is None:
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Invalid WXO snapshot: {path}")
    return value


def write_receipt(
    *,
    root: Path,
    target_environment: str,
    output: Path,
    operation: str | None = None,
    before_snapshot: dict | None = None,
    after_snapshot: dict | None = None,
) -> dict:
    hashes = source_hashes(root)
    bundle = hashlib.sha256(
        "".join(f"{name}\0{digest}\n" for name, digest in hashes.items()).encode()
    ).hexdigest()
    agent = yaml.safe_load((root / "src/agent.yaml").read_text())
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    receipt = {
        "schema_version": 2,
        "imported_at": datetime.now(UTC).isoformat(),
        "target_environment": target_environment,
        "git_revision": _git(root, "rev-parse", "HEAD"),
        "git_dirty": bool(status),
        "agent_name": agent.get("name"),
        "source_bundle_sha256": bundle,
        "source_files": hashes,
    }
    if after_snapshot is not None:
        receipt.update(
            {
                "operation": operation,
                "agent_id": after_snapshot.get("agent_id"),
                "environments": after_snapshot.get("environments"),
                "live_version_before": (
                    before_snapshot.get("environments", {}).get("live", {}).get("current_version")
                    if before_snapshot is not None
                    else None
                ),
                "live_version_after": after_snapshot.get("environments", {})
                .get("live", {})
                .get("current_version"),
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--target-environment", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--operation", choices=("draft_import", "live_promotion"))
    parser.add_argument("--before-snapshot", type=Path)
    parser.add_argument("--after-snapshot", type=Path)
    args = parser.parse_args()
    if args.target_environment != "local" and not all(
        (args.operation, args.before_snapshot, args.after_snapshot)
    ):
        parser.error("cloud receipts require operation and before/after WXO snapshots")
    write_receipt(
        root=args.root.resolve(),
        target_environment=args.target_environment,
        output=args.output.resolve(),
        operation=args.operation,
        before_snapshot=_load_snapshot(args.before_snapshot),
        after_snapshot=_load_snapshot(args.after_snapshot),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
