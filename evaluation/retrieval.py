"""Local-only retrieval benchmark with immutable inputs and resumable artifacts."""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import math
import os
import re
import statistics
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import yaml

from evaluation.io import sha256 as _sha256
from evaluation.io import utc_now as _utc_now

ROOT = Path(__file__).parents[1]
DEFAULT_CORPUS = ROOT / "tests/evaluation/retrieval_questions.yaml"
DEFAULT_RAG_ROOT = ROOT.parent / "shopper-platform" / "apps" / "rag-api"
DEFAULT_BASE_URL = "http://127.0.0.1:8081"
KS = (8, 12, 20)


def require_loopback_url(value: str) -> str:
    """Reject every non-loopback application target."""
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if parsed.scheme != "http" or not hostname or parsed.username or parsed.password:
        raise ValueError("RAG benchmark requires a loopback HTTP origin")
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.casefold() == "localhost"
    if not loopback or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("RAG benchmark refuses non-loopback or path-qualified URLs")
    return value.rstrip("/")


def _git_revision(path: Path) -> str:
    """Identify both the committed revision and scoped working-tree content."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        repository_root = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD", "--", "."],
            cwd=path,
            check=True,
            capture_output=True,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", "."],
            cwd=path,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    digest = hashlib.sha256(diff)
    for raw_path in sorted(item for item in untracked if item):
        digest.update(raw_path)
        try:
            digest.update((repository_root / os.fsdecode(raw_path)).read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    if not diff and not any(untracked):
        return head
    return f"{head}+dirty.{digest.hexdigest()[:12]}"


def load_corpus(path: Path) -> dict[str, Any]:
    """Load and expand the version-controlled retrieval corpus."""
    corpus = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(corpus, dict) or corpus.get("schema_version") != 1:
        raise ValueError(f"Unsupported retrieval corpus: {path}")
    contexts = corpus.get("contexts")
    groups = corpus.get("groups")
    if not isinstance(contexts, dict) or not isinstance(groups, list):
        raise ValueError("Retrieval corpus requires contexts and groups")
    cases: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Retrieval groups must be objects")
        group_contexts = group.get("contexts", [])
        items = group.get("items", [])
        if not isinstance(group_contexts, list) or not isinstance(items, list):
            raise ValueError("Retrieval group contexts and items must be lists")
        for context_name in group_contexts:
            context = contexts.get(context_name)
            if not isinstance(context, dict):
                raise ValueError(f"Unknown retrieval context: {context_name}")
            for item in items:
                case = {
                    "id": f"{group['id']}_{context_name}_{item['id']}",
                    "category": group["category"],
                    "tier": item.get("tier", group.get("tier", "diagnostic")),
                    "context": context_name,
                    "market": context["market"],
                    "query": item["query"],
                    "searches": item.get("searches")
                    or [
                        {
                            "semantic_query": item.get("semantic_query", item["query"]),
                            "requested_facts": item.get("requested_facts", []),
                            "conditions": item.get("conditions", []),
                            "lexical_terms": item.get("lexical_terms", []),
                        }
                    ],
                    "plan_ids": item.get("plan_ids")
                    or context[group.get("plan_selection", "default_plan_ids")],
                    "expected_concepts": item.get("expected_concepts", []),
                    "evidence_expectation": item.get("evidence_expectation", "answerable"),
                    "notes": item.get("notes", ""),
                }
                cases.append(case)
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Retrieval corpus contains duplicate expanded case IDs")
    expected = int(corpus.get("statistics", {}).get("expanded_cases", 0))
    if expected and len(cases) != expected:
        raise ValueError(f"Retrieval corpus expands to {len(cases)} cases, expected {expected}")
    return {**corpus, "expanded_cases": cases}


def _payload(case: dict[str, Any], context: dict[str, Any], arm: str) -> dict[str, Any]:
    available_plans = context["available_plans"]
    payload: dict[str, Any] = {
        "query": case["query"],
        "plan_ids": case["plan_ids"],
        "available_plans": available_plans,
        "baseline_plan_id": case["plan_ids"][0],
        "effective_year": context["effective_year"],
        "language": context.get("language", "en"),
        "state_code": context.get("state_code"),
        "user_type": context.get("user_type", "prospect"),
    }
    if context["market"] == "IOLS":
        payload["exchange_indicator"] = context["exchange_indicator"]
    if arm == "structured":
        payload["searches"] = case["searches"]
    else:
        payload["searches"] = [{"semantic_query": case["query"]}]
    return {key: value for key, value in payload.items() if value is not None}


def _request_case(
    session: requests.Session,
    base_url: str,
    case: dict[str, Any],
    context: dict[str, Any],
    arm: str,
    request_id: str,
) -> tuple[dict[str, Any], dict[str, Any], float]:
    payload = _payload(case, context, arm)
    path = "/retrieve/iols" if case["market"] == "IOLS" else "/retrieve/mols"
    started = time.perf_counter()
    response = session.post(
        f"{base_url}{path}",
        headers={"Content-Type": "application/json", "X-Request-ID": request_id},
        json=payload,
        timeout=60,
    )
    duration_ms = (time.perf_counter() - started) * 1000
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(f"{case['id']}: non-JSON HTTP {response.status_code}") from exc
    if not response.ok:
        raise RuntimeError(f"{case['id']}: HTTP {response.status_code}: {body}")
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise RuntimeError(f"{case['id']}: invalid retrieval response")
    return payload, body, duration_ms


def _normalized_text(value: object) -> str:
    return re.sub(r"[^a-z0-9$%]+", " ", str(value or "").casefold()).strip()


def _normalized_chunk_text(value: object) -> str:
    """Preserve non-English content when deciding whether passages are identical."""
    return re.sub(r"\W+", " ", str(value or "").casefold()).strip()


def _concept_matches(
    concepts: list[list[str]], results: list[dict[str, Any]], k: int
) -> list[bool]:
    text = _normalized_text(" ".join(str(result.get("text", "")) for result in results[:k]))
    return [any(_normalized_text(alias) in text for alias in aliases) for aliases in concepts]


def _first_relevant_rank(
    concepts: list[list[str]], results: list[dict[str, Any]], k: int
) -> int | None:
    for rank, result in enumerate(results[:k], 1):
        text = _normalized_text(result.get("text", ""))
        if any(_normalized_text(alias) in text for aliases in concepts for alias in aliases):
            return rank
    return None


def _score_case(case: dict[str, Any], body: dict[str, Any], duration_ms: float) -> dict[str, Any]:
    results = body["results"]
    concepts = case["expected_concepts"]
    metrics: dict[str, Any] = {}
    for k in KS:
        matches = _concept_matches(concepts, results, k)
        recall = sum(matches) / len(matches) if matches else None
        metrics[f"fact_recall_at_{k}"] = recall
        metrics[f"answerable_at_{k}"] = bool(matches) and all(matches)
        first_rank = _first_relevant_rank(concepts, results, k)
        metrics[f"mrr_at_{k}"] = 1 / first_rank if first_rank else 0.0
    result_plan_ids = [
        result.get("metadata", {}).get("prop_plan_id")
        for result in results
        if isinstance(result.get("metadata"), dict)
    ]
    expected_plans = set(case["plan_ids"])
    normalized_chunks = [
        (
            (
                result.get("metadata", {}).get("prop_plan_id")
                if isinstance(result.get("metadata"), dict)
                else None
            ),
            _normalized_chunk_text(result.get("text")),
        )
        for result in results
    ]
    metrics.update(
        plan_coverage=len(expected_plans & set(result_plan_ids)) / len(expected_plans),
        plan_contamination=any(plan_id not in expected_plans for plan_id in result_plan_ids),
        duplicate_chunks=len(normalized_chunks) - len(set(normalized_chunks)),
        result_count=len(results),
        duration_ms=duration_ms,
    )
    return metrics


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [record for record in records if record.get("status") == "success"]
    summary: dict[str, Any] = {
        "cases": len(records),
        "successful": len(successful),
        "errors": len(records) - len(successful),
        "calls": len(records),
    }
    for k in KS:
        recalls = [
            record["metrics"][f"fact_recall_at_{k}"]
            for record in successful
            if record["metrics"][f"fact_recall_at_{k}"] is not None
        ]
        summary[f"mean_fact_recall_at_{k}"] = statistics.fmean(recalls) if recalls else None
        summary[f"answerability_at_{k}"] = (
            statistics.fmean(
                float(record["metrics"][f"answerable_at_{k}"]) for record in successful
            )
            if successful
            else 0.0
        )
        summary[f"mrr_at_{k}"] = (
            statistics.fmean(record["metrics"][f"mrr_at_{k}"] for record in successful)
            if successful
            else 0.0
        )
    durations = [record["metrics"]["duration_ms"] for record in successful]
    summary.update(
        plan_contamination_rate=(
            statistics.fmean(
                float(record["metrics"]["plan_contamination"]) for record in successful
            )
            if successful
            else 0.0
        ),
        mean_plan_coverage=(
            statistics.fmean(record["metrics"]["plan_coverage"] for record in successful)
            if successful
            else 0.0
        ),
        duplicate_chunks=sum(record["metrics"]["duplicate_chunks"] for record in successful),
        latency_p50_ms=_percentile(durations, 0.50),
        latency_p95_ms=_percentile(durations, 0.95),
    )
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_category[record["category"]].append(record)
    summary["by_category"] = {
        category: _aggregate_category(items) for category, items in sorted(by_category.items())
    }
    return summary


def _aggregate_category(records: list[dict[str, Any]]) -> dict[str, Any]:
    successful = [record for record in records if record.get("status") == "success"]
    recall = [
        record["metrics"]["fact_recall_at_12"]
        for record in successful
        if record["metrics"]["fact_recall_at_12"] is not None
    ]
    return {
        "cases": len(records),
        "errors": len(records) - len(successful),
        "mean_fact_recall_at_12": statistics.fmean(recall) if recall else None,
        "answerability_at_12": (
            statistics.fmean(float(record["metrics"]["answerable_at_12"]) for record in successful)
            if successful
            else 0.0
        ),
    }


def _sentinel_fingerprint(
    session: requests.Session,
    base_url: str,
    corpus: dict[str, Any],
    arm: str,
) -> str:
    observations: list[dict[str, Any]] = []
    for index, case in enumerate(corpus["expanded_cases"][:10], 1):
        _, body, _ = _request_case(
            session,
            base_url,
            case,
            corpus["contexts"][case["context"]],
            arm,
            f"retrieval-sentinel-{index:02d}",
        )
        observations.append(
            {
                "id": case["id"],
                "results": [
                    {
                        "rank": result.get("rank"),
                        "document_id": result.get("metadata", {}).get("document_id"),
                        "plan_id": result.get("metadata", {}).get("prop_plan_id"),
                    }
                    for result in body["results"][:5]
                ],
            }
        )
    serialized = json.dumps(observations, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _write_records(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    columns = [
        "id",
        "tier",
        "category",
        "market",
        "query",
        "plan_ids",
        "status",
        "fact_recall_at_8",
        "fact_recall_at_12",
        "fact_recall_at_20",
        "answerable_at_12",
        "plan_coverage",
        "plan_contamination",
        "duplicate_chunks",
        "duration_ms",
        "top_context",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for record in records:
            metrics = record.get("metrics", {})
            results = record.get("response", {}).get("results", [])
            writer.writerow(
                {
                    **{column: record.get(column, "") for column in columns},
                    "plan_ids": json.dumps(record.get("plan_ids", [])),
                    **{key: metrics.get(key, "") for key in columns if key in metrics},
                    "top_context": "\n\n".join(
                        str(result.get("text", ""))[:1200] for result in results[:3]
                    ),
                }
            )


def run(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    base_url = require_loopback_url(args.base_url)
    cases = [
        case
        for case in corpus["expanded_cases"]
        if (not args.category or case["category"] in args.category)
        and (not args.tier or case["tier"] in args.tier)
    ]
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        raise ValueError("No retrieval cases selected")

    session = requests.Session()
    sentinel = _sentinel_fingerprint(session, base_url, corpus, args.arm)
    identity = {
        "schema_version": 1,
        "corpus_sha256": _sha256(args.corpus),
        "shopper_agent_revision": _git_revision(ROOT),
        "rag_api_revision": _git_revision(args.rag_root),
        "base_url": base_url,
        "arm": args.arm,
        "categories": sorted(args.category),
        "tiers": sorted(args.tier),
        "limit": args.limit,
        "milvus_sentinel_fingerprint": sentinel,
    }
    run_key = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    output = args.output or ROOT / "artifacts" / "retrieval-evaluations" / run_key
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and not args.force:
        prior = json.loads(manifest_path.read_text())
        if prior.get("identity") == identity and prior.get("complete") is True:
            print(f"Reusing complete retrieval run: {output}")
            return 0
        raise ValueError(f"Output exists with a different or incomplete identity: {output}")
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    started_at = _utc_now()
    for position, case in enumerate(cases, 1):
        record = {
            key: case[key]
            for key in (
                "id",
                "tier",
                "category",
                "market",
                "context",
                "query",
                "searches",
                "plan_ids",
                "expected_concepts",
                "evidence_expectation",
                "notes",
            )
        }
        try:
            request_id = f"retrieval-{run_key}-{position:03d}"
            payload, body, duration_ms = _request_case(
                session,
                base_url,
                case,
                corpus["contexts"][case["context"]],
                args.arm,
                request_id,
            )
            record.update(
                status="success",
                request=payload,
                response=body,
                metrics=_score_case(case, body, duration_ms),
                error="",
            )
            print(
                f"[{position:03d}/{len(cases):03d}] {case['id']}: "
                f"recall@12={record['metrics']['fact_recall_at_12']} "
                f"results={record['metrics']['result_count']}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - preserve checkpoint for every case
            record.update(status="error", error=str(exc))
            print(f"[{position:03d}/{len(cases):03d}] {case['id']}: ERROR {exc}", flush=True)
        records.append(record)
        _write_records(output / "results.jsonl", records)
    summary = _aggregate(records)
    manifest = {
        "identity": identity,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "complete": len(records) == len(cases) and not summary["errors"],
        "selected_case_ids": [case["id"] for case in cases],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    _write_csv(output / "results.csv", records)
    print(json.dumps(summary, indent=2))
    print(f"Artifacts: {output}")
    return 1 if summary["errors"] else 0


def _load_results(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for line in (path / "results.jsonl").read_text().splitlines():
        record = json.loads(line)
        records[record["id"]] = record
    return records


def compare(args: argparse.Namespace) -> int:
    baseline = _load_results(args.baseline)
    challenger = _load_results(args.challenger)
    common = sorted(baseline.keys() & challenger.keys())
    if not common:
        raise ValueError("Retrieval runs contain no common cases")
    wins = losses = ties = 0
    rows: list[dict[str, Any]] = []
    for case_id in common:
        before = baseline[case_id]
        after = challenger[case_id]
        before_recall = before.get("metrics", {}).get("fact_recall_at_12")
        after_recall = after.get("metrics", {}).get("fact_recall_at_12")
        if isinstance(before_recall, int | float) and isinstance(after_recall, int | float):
            if after_recall > before_recall:
                wins += 1
                verdict = "win"
            elif after_recall < before_recall:
                losses += 1
                verdict = "loss"
            else:
                ties += 1
                verdict = "tie"
        else:
            ties += 1
            verdict = "unscored"
        rows.append(
            {
                "id": case_id,
                "category": after.get("category"),
                "query": after.get("query"),
                "baseline_recall_at_12": before_recall,
                "challenger_recall_at_12": after_recall,
                "verdict": verdict,
            }
        )
    baseline_summary = _aggregate(list(baseline.values()))
    challenger_summary = _aggregate(list(challenger.values()))
    latency_ratio = challenger_summary["latency_p95_ms"] / max(
        baseline_summary["latency_p95_ms"], 0.001
    )
    comparison = {
        "cases": len(common),
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "paired_win_loss_ratio": wins / max(losses, 1),
        "baseline": baseline_summary,
        "challenger": challenger_summary,
        "p95_latency_ratio": latency_ratio,
        "promotion_checks": {
            "zero_plan_contamination": challenger_summary["plan_contamination_rate"] == 0,
            "recall_at_12_gain_at_least_0_05": (
                (challenger_summary["mean_fact_recall_at_12"] or 0)
                - (baseline_summary["mean_fact_recall_at_12"] or 0)
                >= 0.05
            ),
            "paired_wins_at_least_twice_losses": wins >= 2 * losses,
        },
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "summary.json").write_text(json.dumps(comparison, indent=2) + "\n")
    with (args.output / "paired.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(comparison, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="Run one local retrieval arm")
    run_parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    run_parser.add_argument(
        "--base-url", default=os.getenv("LOCAL_RAG_API_BASE_URL", DEFAULT_BASE_URL)
    )
    run_parser.add_argument("--rag-root", type=Path, default=DEFAULT_RAG_ROOT)
    run_parser.add_argument("--arm", choices=("control", "structured"), default="structured")
    run_parser.add_argument("--category", action="append", default=[])
    run_parser.add_argument("--tier", action="append", default=[])
    run_parser.add_argument("--limit", type=int)
    run_parser.add_argument("--output", type=Path)
    run_parser.add_argument("--force", action="store_true")
    run_parser.set_defaults(function=run)
    compare_parser = subparsers.add_parser("compare", help="Compare two completed runs")
    compare_parser.add_argument("--baseline", type=Path, required=True)
    compare_parser.add_argument("--challenger", type=Path, required=True)
    compare_parser.add_argument("--output", type=Path, required=True)
    compare_parser.set_defaults(function=compare)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.function(args)


if __name__ == "__main__":
    raise SystemExit(main())
