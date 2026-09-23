#!/usr/bin/env python3
"""Interactive local Shopper Assistant API client for multi-turn debugging."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

from evaluation.clients.shopper_api import (
    PUBLIC_API_RESPONSE_KEY,
    PUBLIC_API_TRANSPORT_ATTEMPTS_KEY,
    ShopperApiClient,
)
from evaluation.clients.wxo import AgentRunResult

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTEXTS_FILE = REPO_ROOT / "tests/evaluation/behavioral_questions.yaml"
DEFAULT_CONTEXT = "medicare_member_current_plan"
DEFAULT_SHOPPER_ENV_FILE = (
    REPO_ROOT.parent / "shopper-platform/apps/shopper-assistant-api/.env.local"
)


def _dotenv_value(path: Path, name: str) -> str | None:
    """Read one dotenv value without asking a shell to reinterpret its JSON."""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} in {path} has invalid double-quote escaping") from exc
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        return value
    return None


def _api_key_from_json(raw_value: str, *, source: str) -> str:
    try:
        mapping = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"CLIENT_API_KEYS_JSON in {source} is not valid JSON") from exc
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError(f"CLIENT_API_KEYS_JSON in {source} must be a non-empty object")
    api_key = next(iter(mapping.values()))
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError(f"the first client API key in {source} is empty")
    return api_key.strip()


def _default_env_files() -> list[Path]:
    configured_root = os.environ.get("SHOPPER_PLATFORM_ROOT", "").strip()
    candidates = [REPO_ROOT / ".env.local"]
    if configured_root:
        candidates.append(
            Path(configured_root).expanduser() / "apps/shopper-assistant-api/.env.local"
        )
    candidates.append(DEFAULT_SHOPPER_ENV_FILE)
    return list(dict.fromkeys(path.resolve() for path in candidates))


def resolve_api_key(api_key_env: str, env_file: Path | None = None) -> tuple[str, str]:
    """Resolve a local API key, preferring an explicit environment variable."""
    explicit = os.environ.get(api_key_env, "").strip()
    if explicit:
        return explicit, f"${api_key_env}"

    inherited_json = os.environ.get("CLIENT_API_KEYS_JSON", "").strip()
    inherited_error: ValueError | None = None
    if inherited_json:
        try:
            return (
                _api_key_from_json(inherited_json, source="$CLIENT_API_KEYS_JSON"),
                "$CLIENT_API_KEYS_JSON",
            )
        except ValueError as exc:
            # A sourced dotenv file commonly loses the JSON quotes. Prefer the original file.
            inherited_error = exc

    candidates = [env_file.expanduser().resolve()] if env_file else _default_env_files()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        direct_key = _dotenv_value(candidate, api_key_env)
        if direct_key:
            return direct_key, str(candidate)
        client_keys = _dotenv_value(candidate, "CLIENT_API_KEYS_JSON")
        if client_keys:
            return _api_key_from_json(client_keys, source=str(candidate)), str(candidate)

    searched = ", ".join(str(path) for path in candidates)
    detail = f" ({inherited_error})" if inherited_error else ""
    raise ValueError(
        f"could not find {api_key_env} or CLIENT_API_KEYS_JSON{detail}; "
        f"searched: {searched}. Use --env-file for a different location."
    )


def load_contexts(path: Path) -> dict[str, dict[str, Any]]:
    """Load named request contexts from an evaluation corpus."""
    payload = yaml.safe_load(path.read_text())
    contexts = payload.get("contexts") if isinstance(payload, dict) else None
    if not isinstance(contexts, dict) or not contexts:
        raise ValueError(f"{path} does not contain a non-empty 'contexts' mapping")
    invalid = [name for name, value in contexts.items() if not isinstance(value, dict)]
    if invalid:
        raise ValueError(f"Invalid context definitions: {', '.join(sorted(invalid))}")
    return contexts


def new_session_id() -> str:
    return f"shopper-chat-{uuid.uuid4().hex[:12]}"


def _plan_label(value: Any) -> str:
    if isinstance(value, dict):
        name = value.get("plan_name") or value.get("name")
        plan_id = value.get("plan_id") or value.get("id")
        if name and plan_id:
            return f"{name} ({plan_id})"
        return str(name or plan_id or value)
    return str(value)


def _plan_list(value: Any) -> str:
    if not isinstance(value, list) or not value:
        return "none"
    return ", ".join(_plan_label(item) for item in value)


def describe_context(name: str, context: dict[str, Any]) -> str:
    current = context.get("user_current_plan")
    recommended = context.get("application_recommended_plans")
    return "\n".join(
        [
            f"context: {name}",
            (
                f"  audience={context.get('prospect_type', 'prospect')} "
                f"market={context.get('application_market_segment', 'unknown')}"
            ),
            f"  current={_plan_label(current) if current else 'none'}",
            f"  recommended={_plan_list(recommended)}",
        ]
    )


def _public_payload(result: AgentRunResult) -> dict[str, Any]:
    payload = result.context.get(PUBLIC_API_RESPONSE_KEY)
    return payload if isinstance(payload, dict) else {}


def _rag_lines(payload: dict[str, Any], mode: str) -> list[str]:
    rag = payload.get("rag_context")
    rag = rag if isinstance(rag, dict) else {}
    contexts = rag.get("contexts")
    contexts = contexts if isinstance(contexts, list) else []
    total = rag.get("total_retrieved", len(contexts))
    lines = [f"rag: {total} chunk(s)"]
    if mode == "off":
        return lines

    for index, item in enumerate(contexts, start=1):
        if not isinstance(item, dict):
            continue
        reference = item.get("reference")
        reference = reference if isinstance(reference, dict) else {}
        document = (
            reference.get("document_name")
            or reference.get("document_id")
            or reference.get("source")
            or "unknown source"
        )
        score = item.get("relevance_score")
        score_text = f" score={score}" if score is not None else ""
        lines.append(f"  [{index}] {document}{score_text}")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        normalized = " ".join(content.split())
        if mode == "summary" and len(normalized) > 500:
            normalized = f"{normalized[:497]}..."
        lines.append(f"      {normalized}")
    return lines


def format_result(result: AgentRunResult, *, rag_mode: str, raw: bool) -> str:
    """Render the public response and the diagnostics useful during agent iteration."""
    payload = _public_payload(result)
    query = payload.get("user_query")
    query = query if isinstance(query, dict) else {}
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    call_info = payload.get("call_info")
    call_info = call_info if isinstance(call_info, dict) else {}
    attempts = result.context.get(PUBLIC_API_TRANSPORT_ATTEMPTS_KEY, 1)

    intent = query.get("business_intent") or "not returned"
    elapsed = metadata.get("total_processing_time_ms")
    elapsed_text = f"{elapsed} ms" if elapsed is not None else f"{result.duration_seconds:.2f} s"
    trace_id = call_info.get("trace_id") or "not returned"
    run_id = call_info.get("run_id") or result.run_id or "not returned"

    lines = [
        "",
        f"assistant> {result.response_text.strip()}",
        "",
        f"intent: {intent}",
        f"plans searched: {_plan_list(metadata.get('plans_searched_for'))}",
        f"plans found: {_plan_list(metadata.get('plans_found'))}",
        f"escalation: {json.dumps(metadata.get('escalation'), ensure_ascii=False)}",
        f"timing: {elapsed_text}; HTTP attempts={attempts}",
        f"trace: run={run_id} trace={trace_id}",
        *_rag_lines(payload, rag_mode),
    ]
    if raw:
        lines.extend(["raw response:", json.dumps(payload, indent=2, ensure_ascii=False)])
    return "\n".join(lines)


def _print_help() -> None:
    print(
        """commands:
  /help                 show this help
  /contexts             list available request contexts
  /context NAME         switch context and start a fresh session
  /new                   start a fresh session with the current context
  /rag off|summary|full control retrieved-context output
  /raw on|off           toggle the complete public response payload
  /quit                  exit"""
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Chat with a local Shopper Assistant API and inspect its public diagnostics."
    )
    parser.add_argument("--url", default="http://127.0.0.1:8082")
    parser.add_argument("--context", default=DEFAULT_CONTEXT)
    parser.add_argument("--contexts-file", type=Path, default=DEFAULT_CONTEXTS_FILE)
    parser.add_argument("--api-key-env", default="SHOPPER_API_KEY")
    parser.add_argument(
        "--env-file",
        type=Path,
        help=(
            "Shopper Assistant API dotenv file (defaults to the sibling shopper-platform checkout)"
        ),
    )
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--rag", choices=("off", "summary", "full"), default="summary")
    parser.add_argument("--raw", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contexts = load_contexts(args.contexts_file)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: cannot load contexts: {exc}", file=sys.stderr)
        return 2
    if args.context not in contexts:
        choices = ", ".join(contexts)
        print(f"error: unknown context {args.context!r}; use one of {choices}", file=sys.stderr)
        return 2

    try:
        api_key, api_key_source = resolve_api_key(args.api_key_env, args.env_file)
    except (OSError, ValueError) as exc:
        print(f"error: cannot load local Shopper API key: {exc}", file=sys.stderr)
        return 2

    try:
        client = ShopperApiClient(
            args.url,
            api_key=api_key,
            timeout=args.timeout,
            allow_remote=False,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    context_name = args.context
    session_id = new_session_id()
    turn = 0
    rag_mode = args.rag
    raw = args.raw
    print("Local Shopper Assistant chat. Type /help for commands.")
    print(f"API key: loaded from {api_key_source}")
    print(describe_context(context_name, contexts[context_name]))
    print(f"session: {session_id}")

    try:
        while True:
            try:
                message = input("\nyou> ").strip()
            except EOFError:
                print()
                break
            if not message:
                continue
            if message.startswith("/"):
                try:
                    command = shlex.split(message)
                except ValueError as exc:
                    print(f"error: {exc}")
                    continue
                name = command[0].casefold()
                if name in {"/quit", "/exit"}:
                    break
                if name == "/help":
                    _print_help()
                elif name == "/contexts":
                    for available in contexts:
                        marker = "*" if available == context_name else " "
                        print(f"{marker} {available}")
                elif name == "/new" and len(command) == 1:
                    session_id = new_session_id()
                    turn = 0
                    print(f"new session: {session_id}")
                elif name == "/context" and len(command) == 2:
                    requested = command[1]
                    if requested not in contexts:
                        print(f"unknown context: {requested}")
                        continue
                    context_name = requested
                    session_id = new_session_id()
                    turn = 0
                    print(describe_context(context_name, contexts[context_name]))
                    print(f"new session: {session_id}")
                elif (
                    name == "/rag"
                    and len(command) == 2
                    and command[1]
                    in {
                        "off",
                        "summary",
                        "full",
                    }
                ):
                    rag_mode = command[1]
                    print(f"rag output: {rag_mode}")
                elif name == "/raw" and len(command) == 2 and command[1] in {"on", "off"}:
                    raw = command[1] == "on"
                    print(f"raw output: {'on' if raw else 'off'}")
                else:
                    print("unknown or malformed command; use /help")
                continue

            turn += 1
            try:
                result = client.run(
                    f"interactive-{session_id}-{turn:03d}",
                    message,
                    contexts[context_name],
                    session_id=session_id,
                )
            except Exception as exc:  # Keep the debugging session alive after request failures.
                print(f"request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            print(format_result(result, rag_mode=rag_mode, raw=raw))
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
