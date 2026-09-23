"""Single entry point for evaluation, replay, review, and diagnostic workflows."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from importlib import import_module

Command = tuple[str, str]

COMMANDS: dict[str, Command] = {
    "behavioral": ("run or compare behavioral evaluations", "evaluation.behavioral"),
    "retrieval": ("run or compare direct RAG retrieval evaluations", "evaluation.retrieval"),
    "assertions": ("check deterministic behavioral evaluation assertions", "evaluation.assertions"),
    "replay-csv": ("replay questions from a review CSV", "evaluation.csv_replay"),
    "review": ("prepare a reviewer-facing evaluation workbook", "evaluation.review"),
    "chat": ("chat with the local Shopper Assistant API", "evaluation.chat"),
    "inspect-artifact": (
        "normalize WXO artifacts through shopper-assistant-api",
        "evaluation.shopper_api_normalization",
    ),
    "curate-external": ("import the external review workbook", "evaluation.curation.external"),
    "curate-uat": ("build the reviewed UAT corpus", "evaluation.curation.uat"),
    "snapshot-paired": (
        "create or validate a paired release snapshot",
        "evaluation.snapshots.paired",
    ),
    "snapshot-standalone": (
        "create or validate a standalone release snapshot",
        "evaluation.snapshots.standalone",
    ),
}


def _usage() -> str:
    lines = ["usage: python -m evaluation <command> [arguments]", "", "commands:"]
    width = max(len(name) for name in COMMANDS)
    lines.extend(f"  {name:<{width}}  {description}" for name, (description, _) in COMMANDS.items())
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print(_usage())
        return 0

    command_name, *command_args = arguments
    command = COMMANDS.get(command_name)
    if command is None:
        print(f"error: unknown evaluation command {command_name!r}\n", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 2

    _, module_name = command
    command_main = import_module(module_name).main
    original_argv = sys.argv
    try:
        sys.argv = [f"evaluation {command_name}", *command_args]
        return command_main()
    finally:
        sys.argv = original_argv
