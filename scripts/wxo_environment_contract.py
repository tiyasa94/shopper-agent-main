#!/usr/bin/env python3
"""Inspect and verify the Draft/Live environment contract for one WXO agent."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ContractError(RuntimeError):
    """Report a selector or deployment transition that violates the release contract."""


def verify_cli_profile(
    *,
    profile_name: str,
    expected_url: str,
    config: Any | None = None,
) -> dict[str, str]:
    """Require a local CLI alias to resolve to the tracked WXO tenant URL."""
    if config is None:
        from ibm_watsonx_orchestrate.cli.config import (
            ENV_WXO_URL_OPT,
            ENVIRONMENTS_SECTION_HEADER,
            Config,
        )

        config = Config()
    else:
        ENVIRONMENTS_SECTION_HEADER = "environments"
        ENV_WXO_URL_OPT = "wxo_url"

    try:
        profile = config.get(ENVIRONMENTS_SECTION_HEADER, profile_name)
    except KeyError as exc:
        raise ContractError(
            f"WXO CLI profile {profile_name!r} is not configured; "
            "run the documented env add command"
        ) from exc
    if not isinstance(profile, dict):
        raise ContractError(f"WXO CLI profile {profile_name!r} is invalid")
    actual_url = str(profile.get(ENV_WXO_URL_OPT, "")).rstrip("/")
    if actual_url != expected_url.rstrip("/"):
        raise ContractError(
            f"WXO CLI profile {profile_name!r} URL does not match deployments/cloud.toml"
        )
    return {"profile_name": profile_name, "url": actual_url}


def _environment_items(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, list):
        items = response
    elif isinstance(response, dict):
        items = next(
            (
                response[key]
                for key in ("environments", "items", "results")
                if isinstance(response.get(key), list)
            ),
            None,
        )
        if items is None:
            items = [value for value in response.values() if isinstance(value, dict)]
    else:
        items = []
    return [item for item in items if isinstance(item, dict)]


def _environment_id(item: dict[str, Any]) -> str:
    for key in ("id", "environment_id", "agent_environment_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _environment_version(item: dict[str, Any]) -> int | str | None:
    for key in ("current_version", "version"):
        if key in item:
            value = item[key]
            if value is None or isinstance(value, (int, str)):
                return value
    return None


def inspect_contract(
    *,
    agent_name: str,
    expected_agent_id: str,
    expected_draft_environment_id: str,
    expected_live_environment_id: str,
    client: Any | None = None,
) -> dict[str, Any]:
    """Return a credential-free, validated snapshot from the active WXO tenant."""
    if client is None:
        from ibm_watsonx_orchestrate.client.agents.agent_client import AgentClient
        from ibm_watsonx_orchestrate.client.utils import instantiate_client

        client = instantiate_client(AgentClient)

    drafts = client.get_draft_by_name(agent_name)
    if not isinstance(drafts, list) or len(drafts) != 1:
        count = len(drafts) if isinstance(drafts, list) else 0
        raise ContractError(f"Expected exactly one agent named {agent_name!r}; found {count}")
    agent = drafts[0]
    agent_id = agent.get("id") if isinstance(agent, dict) else None
    if agent_id != expected_agent_id:
        raise ContractError(
            f"Agent ID mismatch for {agent_name!r}: expected {expected_agent_id}, got {agent_id}"
        )

    items = _environment_items(client.get_environments_for_agent(agent_id))
    by_id = {_environment_id(item): item for item in items if _environment_id(item)}
    missing = [
        environment_id
        for environment_id in (expected_draft_environment_id, expected_live_environment_id)
        if environment_id not in by_id
    ]
    if missing:
        raise ContractError(
            f"Configured WXO environment IDs were not returned: {', '.join(missing)}"
        )

    return {
        "schema_version": 1,
        "inspected_at": datetime.now(UTC).isoformat(),
        "agent_name": agent_name,
        "agent_id": agent_id,
        "environments": {
            "draft": {
                "id": expected_draft_environment_id,
                "current_version": _environment_version(by_id[expected_draft_environment_id]),
            },
            "live": {
                "id": expected_live_environment_id,
                "current_version": _environment_version(by_id[expected_live_environment_id]),
            },
        },
    }


def assert_transition(before: dict[str, Any], after: dict[str, Any], *, operation: str) -> None:
    """Require stable identities plus the expected Live-version transition."""
    for key in ("agent_name", "agent_id"):
        if before.get(key) != after.get(key):
            raise ContractError(f"WXO {key} changed during {operation}")
    if before.get("environments") is None or after.get("environments") is None:
        raise ContractError("WXO environment snapshot is incomplete")
    for environment in ("draft", "live"):
        if before["environments"].get(environment, {}).get("id") != after["environments"].get(
            environment, {}
        ).get("id"):
            raise ContractError(f"WXO {environment} environment ID changed during {operation}")

    live_before = before["environments"]["live"].get("current_version")
    live_after = after["environments"]["live"].get("current_version")
    if operation == "draft_import" and live_before != live_after:
        raise ContractError(
            f"Draft import changed Live version from {live_before!r} to {live_after!r}"
        )
    if operation == "live_promotion" and live_before == live_after:
        raise ContractError(f"Live promotion did not advance version {live_before!r}")


def _load_snapshot(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"Invalid WXO snapshot: {path}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"Invalid WXO snapshot: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--agent-name", required=True)
    inspect_parser.add_argument("--expected-agent-id", required=True)
    inspect_parser.add_argument("--expected-draft-environment-id", required=True)
    inspect_parser.add_argument("--expected-live-environment-id", required=True)
    inspect_parser.add_argument("--output", required=True, type=Path)

    profile_parser = subparsers.add_parser("verify-cli-profile")
    profile_parser.add_argument("--profile-name", required=True)
    profile_parser.add_argument("--expected-url", required=True)

    transition_parser = subparsers.add_parser("assert-transition")
    transition_parser.add_argument("--before", required=True, type=Path)
    transition_parser.add_argument("--after", required=True, type=Path)
    transition_parser.add_argument(
        "--operation", choices=("draft_import", "live_promotion"), required=True
    )
    args = parser.parse_args()

    try:
        if args.command == "inspect":
            snapshot = inspect_contract(
                agent_name=args.agent_name,
                expected_agent_id=args.expected_agent_id,
                expected_draft_environment_id=args.expected_draft_environment_id,
                expected_live_environment_id=args.expected_live_environment_id,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        elif args.command == "verify-cli-profile":
            verify_cli_profile(
                profile_name=args.profile_name,
                expected_url=args.expected_url,
            )
        else:
            assert_transition(
                _load_snapshot(args.before),
                _load_snapshot(args.after),
                operation=args.operation,
            )
    except ContractError as exc:
        parser.exit(1, f"Error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
