#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


def tool_registered_name(filepath: Path) -> str:
    """Return the name= value from the @tool() decorator, falling back to the filename stem."""
    try:
        tree = ast.parse(filepath.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        return str(kw.value.value)
    except Exception:
        pass
    return filepath.stem


def load_secrets(path: Path) -> dict[str, str]:
    """Read nonempty KEY=VALUE entries without evaluating shell expressions."""
    secrets: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        secrets[key.strip()] = val.strip()
    return secrets


def load_cloud_manifest(path: Path) -> tuple[dict, dict]:
    """Load non-secret tenant identity and config-map from the shared cloud manifest.

    Returns (wxo_tenant, cloud_cfg) — neither dict contains credentials or secrets.
    All values are public resource identifiers (names, UUIDs, URLs, region strings).
    """
    with open(path, "rb") as cloud_manifest_fh:
        cloud_manifest = tomllib.load(cloud_manifest_fh)
    return cloud_manifest.get("wxo_tenant", {}), cloud_manifest.get("config_map", {})


def validate_wxo_tenant(tenant: dict) -> None:
    """Validate tenant identity and require its exact HTTPS authority and path.

    Userinfo, explicit ports, query strings, and fragments are not accepted;
    authentication is supplied separately through IAM.
    """
    required = ("resource_name", "cli_alias", "instance_id", "api_url", "region", "auth_type")
    missing = [key for key in required if not str(tenant.get(key, "")).strip()]
    if missing:
        raise ValueError(f"Missing WXO tenant settings: {', '.join(missing)}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", str(tenant["cli_alias"])):
        raise ValueError("WXO tenant cli_alias contains unsupported characters")
    try:
        instance_id = str(UUID(str(tenant["instance_id"])))
    except ValueError as exc:
        raise ValueError("WXO tenant instance_id must be a UUID") from exc
    region = str(tenant["region"])
    parsed = urlsplit(str(tenant["api_url"]))
    expected_host = f"api.{region}.watson-orchestrate.cloud.ibm.com"
    expected_path = f"/instances/{instance_id}"
    if (
        parsed.scheme != "https"
        or parsed.netloc != expected_host
        or parsed.path.rstrip("/") != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("WXO tenant api_url must match its region and instance_id")
    if tenant["auth_type"] != "ibm_iam":
        raise ValueError("WXO tenant auth_type must be ibm_iam")


def validate_cloud_contract(target: str, cfg: dict, connections: dict, tenant: dict) -> None:
    """Validate cloud identity, RAG routing, and inference/RAG credential slots."""
    if target == "local":
        return
    if target not in {"draft", "live"}:
        raise ValueError(f"Unknown cloud deployment target: {target}")
    validate_wxo_tenant(tenant)
    required = (
        "WXO_AGENT_ID",
        "WXO_DRAFT_ENVIRONMENT_ID",
        "WXO_LIVE_ENVIRONMENT_ID",
        "RAG_API_ENVIRONMENT",
        "RAG_API_BASE_URL",
    )
    missing = [key for key in required if not str(cfg.get(key, "")).strip()]
    if missing:
        raise ValueError(f"Missing cloud deployment settings: {', '.join(missing)}")
    for key in ("WXO_AGENT_ID", "WXO_DRAFT_ENVIRONMENT_ID", "WXO_LIVE_ENVIRONMENT_ID"):
        try:
            UUID(str(cfg[key]))
        except ValueError as exc:
            raise ValueError(f"{key} must be a UUID") from exc
    if cfg["WXO_DRAFT_ENVIRONMENT_ID"] == cfg["WXO_LIVE_ENVIRONMENT_ID"]:
        raise ValueError("Draft and Live environment IDs must differ")

    expected_rag_environment = "dev" if target == "draft" else "stage"
    if cfg["RAG_API_ENVIRONMENT"] != expected_rag_environment:
        raise ValueError(f"{target} must use the {expected_rag_environment} RAG API environment")
    parsed = urlsplit(str(cfg["RAG_API_BASE_URL"]))
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("RAG_API_BASE_URL must be an HTTPS origin")
    credentials = connections.get("credentials", {})
    for app_id, label, secret in (
        ("elevance-rag-tool-anthem", "RAG", "RAG_API_KEY"),
        ("elevance-wxo-inference", "inference", "WXO_API_KEY"),
    ):
        connection = credentials.get(app_id, {})
        if connection.get("envs") != [target]:
            raise ValueError(
                f"{target} {label} credentials must target the {target} connection slot"
            )
        if connection.get("secret_vars") != [secret]:
            raise ValueError(f"{target} {label} credentials must require {secret}")


def main() -> int:
    """Validate deployment inputs and write the shell configuration for asset import.

    Cloud inference uses the tracked agent tenant. Tenant variables contain only
    public identifiers; runtime secrets are emitted separately as SET_CREDS entries.
    """
    manifest_path = Path(sys.argv[1])
    secrets_path = Path(sys.argv[2])
    output_path = Path(sys.argv[3])
    cloud_manifest_path = Path(sys.argv[4]) if len(sys.argv) > 4 else None
    root_dir = manifest_path.parent.parent.parent

    with open(manifest_path, "rb") as manifest_fh:
        m = tomllib.load(manifest_fh)

    secrets = load_secrets(secrets_path)
    target = manifest_path.parent.name
    cfg = dict(m["config_map"])
    if target != "local":
        if cloud_manifest_path is None:
            print("Error: cloud deployments require the shared cloud manifest.", file=sys.stderr)
            return 1
        wxo_tenant, cloud_cfg = load_cloud_manifest(cloud_manifest_path)
        overlap = sorted(set(cfg).intersection(cloud_cfg))
        if overlap:
            print(
                "Error: target manifest duplicates shared cloud settings: " + ", ".join(overlap),
                file=sys.stderr,
            )
            return 1
        cfg = {**cloud_cfg, **cfg}
        cfg["WXO_API_URL"] = wxo_tenant.get("api_url", "")
    try:
        validate_cloud_contract(
            target,
            cfg,
            m["connections"],
            wxo_tenant if target != "local" else {},
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    lines: list[str] = []

    if target == "local":
        lines.append(f'WO_ENVIRONMENT_NAME="{cfg["WO_ENVIRONMENT_NAME"]}"')
    else:
        tenant_vars = {
            "WXO_TENANT_RESOURCE_NAME": wxo_tenant["resource_name"],
            "WO_ENVIRONMENT_NAME": wxo_tenant["cli_alias"],
            "WXO_TENANT_INSTANCE_ID": wxo_tenant["instance_id"],
            "WXO_TENANT_API_URL": wxo_tenant["api_url"],
            "WXO_TENANT_REGION": wxo_tenant["region"],
            "WXO_TENANT_AUTH_TYPE": wxo_tenant["auth_type"],
        }
        lines.extend(f'{key}="{value}"' for key, value in tenant_vars.items())
    for key in (
        "WXO_AGENT_ID",
        "WXO_DRAFT_ENVIRONMENT_ID",
        "WXO_LIVE_ENVIRONMENT_ID",
        "RAG_API_ENVIRONMENT",
        "RAG_API_BASE_URL",
    ):
        if key in cfg:
            lines.append(f'{key}="{cfg[key]}"')

    abs_specs = " ".join(str(root_dir / s) for s in m["connections"]["specs"])
    lines.append(f'CONNECTION_SPECS="{abs_specs}"')

    # Each SET_CREDS_<N> encodes one set-credentials call as: "<app_id> <env_slot> KEY=VAL ..."
    credentials = m["connections"].get("credentials", {})
    cred_index = 0
    for app_id, cred in credentials.items():
        for env_slot in cred["envs"]:
            parts = [app_id, env_slot]
            for var in cred.get("secret_vars", []):
                if var not in secrets:
                    print(
                        f"Error: {var} is required in {secrets_path} but is not set.",
                        file=sys.stderr,
                    )
                    return 1
                parts.append(f"{var}={secrets[var]}")
            for var in cred.get("manifest_vars", []):
                if var not in cfg:
                    print(
                        f"Error: {var} is listed in manifest_vars but missing from [config_map].",
                        file=sys.stderr,
                    )
                    return 1
                parts.append(f"{var}={cfg[var]}")
            lines.append(f'SET_CREDS_{cred_index}="{" ".join(parts)}"')
            cred_index += 1
    lines.append(f'SET_CREDS_COUNT="{cred_index}"')

    tools = m["tools"]
    lines.append(f'TOOLS_DIR="{root_dir / tools["dir"]}"')
    lines.append(f'TOOLS_REQUIREMENTS="{root_dir / tools["requirements"]}"')
    lines.append(f'TOOLS_PACKAGE_DIR="{root_dir / tools["package_dir"]}"')

    # Each TOOL_<N> encodes one tools import call as: "<filepath> <app_id>"
    # TOOL_NAME_<N> is the registered tool name from the @tool(name=...) decorator.
    for i, (filename, app_id) in enumerate(tools["app_ids"].items()):
        filepath = root_dir / tools["dir"] / filename
        lines.append(f'TOOL_{i}="{filepath} {app_id}"')
        lines.append(f'TOOL_NAME_{i}="{tool_registered_name(filepath)}"')
    lines.append(f'TOOL_COUNT="{len(tools["app_ids"])}"')

    abs_agents = " ".join(str(root_dir / f) for f in m["agents"]["files"])
    lines.append(f'AGENT_FILES="{abs_agents}"')

    output_path.write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
