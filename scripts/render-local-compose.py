#!/usr/bin/env python3
"""Render ignored Compose environment overrides from local manifests and secrets."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import tomllib
from pathlib import Path


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _string(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _service_environment(manifest_path: Path, secrets_path: Path) -> dict[str, str]:
    manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    config_map = manifest.get("config_map")
    if not isinstance(config_map, dict):
        raise ValueError(f"manifest must contain [config_map]: {manifest_path}")
    environment = _env_file(secrets_path)
    environment.update({str(key): _string(value) for key, value in config_map.items()})
    return environment


def _add_rag_client_key(rag_environment: dict[str, str], agent_secrets_path: Path) -> None:
    api_key = _env_file(agent_secrets_path).get("RAG_API_KEY", "").strip()
    if not api_key:
        raise ValueError(f"RAG_API_KEY is required in {agent_secrets_path}")

    raw_client_keys = rag_environment.get("CLIENT_API_KEYS_JSON", "{}")
    try:
        client_keys = json.loads(raw_client_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("CLIENT_API_KEYS_JSON must be valid JSON") from exc
    if not isinstance(client_keys, dict) or not all(
        isinstance(key, str) and bool(key) and isinstance(value, str) and bool(value)
        for key, value in client_keys.items()
    ):
        raise ValueError("CLIENT_API_KEYS_JSON must be a JSON object of non-empty strings")
    if len(set(client_keys.values())) != len(client_keys):
        raise ValueError("CLIENT_API_KEYS_JSON values must be unique")

    matching_client = next(
        (client_id for client_id, value in client_keys.items() if value == api_key),
        None,
    )
    if matching_client is None:
        client_keys["shopper-agent"] = api_key
    elif matching_client != "shopper-agent":
        client_keys.pop("shopper-agent", None)
    rag_environment["CLIENT_API_KEYS_JSON"] = json.dumps(
        client_keys, separators=(",", ":"), sort_keys=True
    )


def _write_private_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-root", type=Path, required=True)
    parser.add_argument("--rag-secrets", type=Path, required=True)
    parser.add_argument("--shopper-secrets", type=Path, required=True)
    parser.add_argument("--agent-secrets", type=Path, required=True)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--rag-plan-summary-mode",
        choices=("disabled", "protected", "fused"),
        default=os.environ.get("RETRIEVAL_PLAN_SUMMARY_MODE"),
    )
    args = parser.parse_args()

    rag_root = args.platform_root / "apps" / "rag-api"
    shopper_root = args.platform_root / "apps" / "shopper-assistant-api"
    rag_environment = _service_environment(
        rag_root / "deployment" / "environments" / "local" / "manifest.toml",
        args.rag_secrets,
    )
    _add_rag_client_key(rag_environment, args.agent_secrets)
    if args.rag_plan_summary_mode:
        rag_environment["RETRIEVAL_PLAN_SUMMARY_MODE"] = args.rag_plan_summary_mode
    shopper_environment = _service_environment(
        shopper_root / "deployment" / "environments" / "local" / "manifest.toml",
        args.shopper_secrets,
    )
    shopper_environment.update(
        {
            "DATABASE_URL": (
                "postgresql://shopper_assistant:local-development-only"
                "@postgres:5432/shopper_assistant"
            ),
            "WXO_API_URL": "http://host.containers.internal:4321",
            "WXO_AUTH_TYPE": "local_pw",
            "WXO_AGENT_ID": args.agent_id,
            "WXO_USERNAME": "wxo.archer@ibm.com",
            "WXO_PASSWORD": "watsonx",
        }
    )
    shopper_environment.pop("WXO_API_KEY", None)

    _write_private_json(
        args.output,
        {
            "services": {
                "rag-api": {"environment": rag_environment},
                "shopper-api": {"environment": shopper_environment},
            }
        },
    )


if __name__ == "__main__":
    main()
