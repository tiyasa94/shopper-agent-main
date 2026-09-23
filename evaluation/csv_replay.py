"""Run a question CSV through the local agent and checkpoint reviewable results."""

import argparse
import csv
import io
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from evaluation.clients.wxo import (
    GENERAL_SEARCH_TOOL,
    SEARCH_TOOL,
    AgentRunResult,
    WxoClient,
    _sanitized,
)
from evaluation.contexts import shopper_api_to_wxo

RESULT_COLUMNS = [
    "Evaluation ID",
    "Actual Response",
    "Duration Seconds",
    "Tool Trace",
    "Turn Result",
    "Search Query",
    "Search Plan IDs",
    "RAG Status",
    "RAG Coverage Complete",
    "RAG Missing Plans",
    "Retrieved RAG Context",
    "Guardrail/Audit Identification",
    "Guardrail/Audit Type",
    "Run ID",
    "Full Artifact Path",
    "Error",
    "Reviewer Rating",
    "Reviewer Notes",
]


def read_source_rows(path: Path) -> tuple[list[str], list[dict[str, str]], str]:
    """Read UTF-8 or the spreadsheet export's MacRoman encoding."""
    raw = path.read_bytes()
    encoding = "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = "mac_roman"
        text = raw.decode(encoding)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if (
        not reader.fieldnames
        or "Question" not in reader.fieldnames
        or "Context" not in reader.fieldnames
    ):
        raise ValueError("input CSV must contain Question and Context columns")
    return list(reader.fieldnames), list(reader), encoding


def source_context_to_wxo(raw_context: str) -> dict[str, Any]:
    """Translate the nested source spreadsheet context into WXO context variables."""
    return shopper_api_to_wxo(json.loads(raw_context))


def _first_response(result: AgentRunResult, tool_name: str) -> Any:
    responses = result.responses_for(tool_name)
    return responses[0].content if responses else None


def _audit_payload(result: AgentRunResult) -> dict[str, Any]:
    search = result.search_envelope() or {}
    if search.get("audit_completed") is True:
        return search.get("escalation", {})
    turn = result.turn_result()
    return turn.get("escalation", {}) if turn.get("audit_completed") is True else {}


def result_columns(result: AgentRunResult, artifact_path: Path) -> dict[str, str]:
    search_calls = result.calls_for(SEARCH_TOOL) or result.calls_for(GENERAL_SEARCH_TOOL)
    search_args = search_calls[0].args if search_calls else {}
    rag_tool = search_calls[0].name if search_calls else ""
    rag = _first_response(result, rag_tool)
    rag = rag if isinstance(rag, dict) else {}
    turn = result.turn_result()
    audit = _audit_payload(result)
    return {
        "Actual Response": result.response_text,
        "Duration Seconds": str(result.duration_seconds),
        "Tool Trace": result.trace_summary(),
        "Turn Result": json.dumps(turn, ensure_ascii=False),
        "Search Query": str(search_args.get("search_query", search_args.get("query", ""))),
        "Search Plan IDs": json.dumps(search_args.get("plan_ids", [])),
        "RAG Status": str(rag.get("outcome", rag.get("status", ""))),
        "RAG Coverage Complete": str(rag.get("coverage_complete", "")),
        "RAG Missing Plans": json.dumps(rag.get("missing_plans", [])),
        "Retrieved RAG Context": json.dumps(_sanitized(rag.get("results", [])), ensure_ascii=False),
        "Guardrail/Audit Identification": str(audit.get("identification", "")),
        "Guardrail/Audit Type": str(audit.get("type", "")),
        "Run ID": result.run_id,
        "Full Artifact Path": str(artifact_path),
        "Error": "",
    }


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _append_artifact(
    path: Path, evaluation_id: str, source_row: dict, result: AgentRunResult
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "evaluation_id": evaluation_id,
        "source": source_row,
        "result": asdict(result),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_sanitized(artifact), ensure_ascii=False, default=str) + "\n")


def _existing_results(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            row.get("Evaluation ID", ""): row
            for row in csv.DictReader(handle)
            if row.get("Evaluation ID")
        }


def run(args: argparse.Namespace) -> int:
    source_fields, source_rows, encoding = read_source_rows(args.input)
    output = args.output or Path("artifacts/evaluations") / f"{args.input.stem}-results.csv"
    artifact_path = output.with_suffix(".jsonl")
    prior = _existing_results(output) if args.resume else {}
    output_rows: list[dict[str, str]] = []
    fieldnames = [*source_fields, *[name for name in RESULT_COLUMNS if name not in source_fields]]

    candidates: list[tuple[int, dict[str, str]]] = []
    for row_number, source_row in enumerate(source_rows, start=1):
        if row_number < args.start:
            continue
        if args.nature and source_row.get("Nature") not in args.nature:
            continue
        candidates.append((row_number, source_row))
    if args.limit is not None:
        candidates = candidates[: args.limit]

    client = WxoClient(args.url, args.agent, timeout=args.timeout)
    print(
        f"Running {len(candidates)} local evaluations from {args.input} ({encoding}) -> {output}",
        flush=True,
    )

    for position, (row_number, source_row) in enumerate(candidates, start=1):
        evaluation_id = f"row_{row_number:03d}"
        completed = evaluation_id in prior and (
            prior[evaluation_id].get("Actual Response") or prior[evaluation_id].get("Error")
        )
        if completed:
            output_rows.append(prior[evaluation_id])
            print(f"[{position}/{len(candidates)}] {evaluation_id}: resumed", flush=True)
            continue

        output_row = {**source_row, "Evaluation ID": evaluation_id}
        try:
            context = source_context_to_wxo(source_row.get("Context", ""))
            result = client.run(evaluation_id, source_row.get("Question", ""), context)
            _append_artifact(artifact_path, evaluation_id, source_row, result)
            output_row.update(result_columns(result, artifact_path))
            print(
                f"[{position}/{len(candidates)}] {evaluation_id}: "
                f"{result.trace_summary()} ({result.duration_seconds}s)",
                flush=True,
            )
        except Exception as exc:  # checkpoint failures so a long run can continue
            output_row["Error"] = f"{type(exc).__name__}: {exc}"
            print(f"[{position}/{len(candidates)}] {evaluation_id}: ERROR {exc}", flush=True)
        output_rows.append(_sanitized(output_row))
        _write_csv(output, fieldnames, output_rows)

    _write_csv(output, fieldnames, output_rows)
    print(f"Results: {output}\nFull sanitized traces/RAG payloads: {artifact_path}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--nature", action="append", help="Only run matching Nature values")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--url", default=os.getenv("LOCAL_WXO_URL", "http://localhost:4321"))
    parser.add_argument(
        "--agent",
        default=os.getenv("LOCAL_WXO_AGENT_NAME", "Elevance_Health_Shopper_Portal"),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("LOCAL_WXO_E2E_TIMEOUT", "120")),
    )
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
