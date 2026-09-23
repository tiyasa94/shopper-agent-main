#!/usr/bin/env python3
"""Validate local WXO trace artifacts with shopper-assistant-api's real normalizer."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any


def _default_api_root() -> Path:
    configured = os.getenv("SHOPPER_ASSISTANT_API_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "shopper-platform/apps/shopper-assistant-api"


def _load_chat_accumulator(api_root: Path) -> type[Any]:
    """Load the current Shopper API response accumulator."""

    if not (api_root / "app/integrations/wxo/normalizer.py").is_file():
        raise FileNotFoundError(f"shopper-assistant-api normalizer not found under {api_root}")
    sys.path.insert(0, str(api_root))
    module = importlib.import_module("app.integrations.wxo.normalizer")
    chat_accumulator = getattr(module, "ChatAccumulator", None)
    if chat_accumulator is not None:
        return chat_accumulator
    raise AttributeError("shopper-assistant-api normalizer has no ChatAccumulator")


def _completed_payload(
    result: dict[str, Any],
    response_text: str,
    details: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the completed-chat shape expected by Shopper Assistant API."""

    message: dict[str, Any] = {
        "content": [{"response_type": "text", "text": response_text}],
        "context": result.get("context", {}),
    }
    return {
        "run_id": result.get("run_id", "artifact-run"),
        "status": "completed",
        "data": {"message": message},
        "step_history": [{"step_details": details}],
    }


def normalize_artifact(artifact: dict[str, Any], api_root: Path) -> dict[str, Any]:
    """Feed one shopper-agent artifact through the installed Shopper API normalizer."""
    result = artifact.get("result")
    if not isinstance(result, dict):
        raise ValueError("artifact has no result object")

    details: list[dict[str, Any]] = []
    for call in result.get("tool_calls", []):
        if isinstance(call, dict):
            details.append(
                {
                    "type": "tool_call",
                    "name": call.get("name"),
                    "tool_call_id": call.get("tool_call_id") or call.get("id"),
                    "args": call.get("args", {}),
                }
            )
    for response in result.get("tool_responses", []):
        if not isinstance(response, dict):
            continue
        content = response.get("content")
        details.append(
            {
                "type": "tool_response",
                "name": response.get("name"),
                "tool_call_id": response.get("tool_call_id") or response.get("id"),
                "content": content if isinstance(content, str) else json.dumps(content),
            }
        )

    Accumulator = _load_chat_accumulator(api_root)
    accumulator = Accumulator()
    response_text = str(result.get("response_text", "") or "")
    accumulator.merge_payload(_completed_payload(result, response_text, details))
    accumulator.append_text(response_text, complete=True)
    return {
        "response_text": accumulator.text,
        "business_intent": accumulator.business_intent,
        "plans_found": sorted(accumulator.plans_found),
        "plans_searched_for": sorted(accumulator.plans_searched_for),
        "rag_context_count": len(accumulator.rag_contexts),
        "rag_context": [item.model_dump(exclude_none=True) for item in accumulator.rag_contexts],
        "plan_details": accumulator.plan_details.model_dump(exclude_none=True),
        "escalation": accumulator.escalation.model_dump(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", type=Path)
    parser.add_argument("--shopper-api-root", type=Path, default=_default_api_root())
    args = parser.parse_args()

    summaries = []
    for path in args.artifacts:
        artifact = json.loads(path.read_text())
        normalized = normalize_artifact(artifact, args.shopper_api_root.resolve())
        summaries.append({"artifact": str(path), **normalized})
    print(json.dumps(summaries, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
