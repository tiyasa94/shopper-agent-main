"""Check deterministic response and retrieval assertions for a behavioral run."""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SPEC = Path(__file__).parents[1] / "tests/evaluation/behavioral_assertions.yaml"
SUMMARY_DETAILS_PATTERN = re.compile(
    r"\ASummary answer:\n\S.*\n\nDetails:\n\S.*\Z",
    re.DOTALL,
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} is not an object")
        records.append(value)
    return records


def _load_spec(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError(f"Unsupported assertion spec: {path}")
    turns = value.get("turns")
    if not isinstance(turns, dict) or not turns:
        raise ValueError("Assertion spec requires a nonempty turns mapping")
    return value


def _response(record: dict[str, Any]) -> str:
    result = record.get("result")
    return str(result.get("response_text", "")) if isinstance(result, dict) else ""


def _available_plan_names(record: dict[str, Any]) -> list[str]:
    plans = record.get("available_plans")
    if not isinstance(plans, list):
        return []
    return [
        str(plan.get("plan_name", "")).strip()
        for plan in plans
        if isinstance(plan, dict) and str(plan.get("plan_name", "")).strip()
    ]


def _observed_plan_ids(record: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    """Return explicit plan IDs or infer them from public plan-name observability."""
    explicit: list[str] = []
    for field in ("plan_details_plan_ids", "search_plan_ids"):
        values = observed.get(field)
        if isinstance(values, list):
            explicit.extend(str(plan_id) for plan_id in values)
    if explicit:
        return list(dict.fromkeys(explicit))

    searched = observed.get("plans_searched_for")
    if not isinstance(searched, list):
        return []
    plan_ids_by_name = {
        str(plan.get("plan_name", "")).strip().casefold(): str(plan.get("plan_id", "")).strip()
        for plan in record.get("available_plans", [])
        if isinstance(plan, dict)
        and str(plan.get("plan_name", "")).strip()
        and str(plan.get("plan_id", "")).strip()
    }
    return [
        plan_ids_by_name[name]
        for value in searched
        if (name := str(value).strip().casefold()) in plan_ids_by_name
    ]


def _contains_plan_name(response: str, plan_name: str) -> bool:
    """Match a complete plan name without treating `Plan A` as `plan are`."""
    return (
        re.search(
            rf"(?<!\w){re.escape(plan_name)}(?!\w)",
            response,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _word_count(value: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", value, flags=re.UNICODE))


def _match_text(value: object) -> str:
    """Normalize harmless Unicode presentation differences for substring checks."""

    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"\s+([%])", r"\1", normalized)


def _check_record(record: dict[str, Any], assertion: dict[str, Any]) -> list[str]:
    response = _response(record)
    folded = _match_text(response)
    observations = record.get("observations")
    observed = observations if isinstance(observations, dict) else {}
    failures: list[str] = []

    prefix = assertion.get("required_response_prefix")
    if isinstance(prefix, str) and not response.startswith(prefix):
        failures.append("required response prefix is missing")

    structure = assertion.get("required_response_structure")
    if structure == "summary_details" and not SUMMARY_DETAILS_PATTERN.fullmatch(response):
        failures.append("response does not use the exact Summary answer/Details structure")
    if structure not in {None, "summary_details"}:
        raise ValueError(f"Unsupported required_response_structure: {structure}")

    for required in assertion.get("required_response_substrings", []):
        if _match_text(required) not in folded:
            failures.append(f"required response text is missing: {required}")
    for forbidden in assertion.get("forbidden_response_substrings", []):
        if _match_text(forbidden) in folded:
            failures.append(f"forbidden response text is present: {forbidden}")

    maximum = assertion.get("max_response_words")
    if isinstance(maximum, int) and _word_count(response) > maximum:
        failures.append(f"response exceeds {maximum} words")

    expected_rag = assertion.get("expected_rag_called")
    if isinstance(expected_rag, bool) and bool(observed.get("rag_called")) != expected_rag:
        failures.append(f"rag_called is not {expected_rag}")

    plan_details_observable = observed.get("plan_details_observable", True) is not False
    if plan_details_observable:
        expected_plan_details = assertion.get("expected_plan_details_called")
        if (
            isinstance(expected_plan_details, bool)
            and bool(observed.get("plan_details_called")) != expected_plan_details
        ):
            failures.append(f"plan_details_called is not {expected_plan_details}")

        expected_detail_types = assertion.get("expected_detail_types")
        if isinstance(expected_detail_types, list):
            actual_detail_types = observed.get("plan_detail_types", [])
            if not isinstance(actual_detail_types, list) or set(actual_detail_types) != set(
                expected_detail_types
            ):
                failures.append(f"plan_detail_types do not equal the set {expected_detail_types}")

        expected_outcome = assertion.get("expected_plan_details_outcome")
        if (
            isinstance(expected_outcome, str)
            and observed.get("plan_details_outcome") != expected_outcome
        ):
            failures.append(f"plan_details_outcome is not {expected_outcome}")

        expected_complete = assertion.get("expected_plan_details_coverage_complete")
        if (
            isinstance(expected_complete, bool)
            and observed.get("plan_details_coverage_complete") is not expected_complete
        ):
            failures.append(f"plan_details_coverage_complete is not {expected_complete}")

        expected_fallback = assertion.get("expected_plan_details_fallback_types")
        if isinstance(expected_fallback, list):
            actual_fallback = observed.get("plan_details_fallback_types", [])
            if not isinstance(actual_fallback, list) or set(actual_fallback) != set(
                expected_fallback
            ):
                failures.append(
                    f"plan_details_fallback_types do not equal the set {expected_fallback}"
                )

    expected_plan_ids = assertion.get("expected_plan_ids")
    if isinstance(expected_plan_ids, list):
        actual_plan_ids = _observed_plan_ids(record, observed)
        if len(actual_plan_ids) != len(expected_plan_ids) or set(actual_plan_ids) != set(
            expected_plan_ids
        ):
            failures.append(f"observed plan IDs do not equal the set {expected_plan_ids}")

    if assertion.get("forbid_available_plan_names") is True:
        exposed = [
            name for name in _available_plan_names(record) if _contains_plan_name(response, name)
        ]
        if exposed:
            failures.append(f"response enumerates available plan names: {', '.join(exposed)}")

    for field in (
        "sensitive_value_echoed",
        "serialized_envelope_exposed",
    ):
        if observed.get(field) is True:
            failures.append(f"{field} is true")
    return failures


def evaluate(run: Path, spec_path: Path) -> dict[str, Any]:
    spec = _load_spec(spec_path)
    records = _load_jsonl(run / "results.jsonl")
    assertions = spec["turns"]
    selected = [record for record in records if record.get("turn_id") in assertions]
    if not selected:
        raise ValueError("Run contains none of the asserted turn IDs")

    checks: list[dict[str, Any]] = []
    seen: Counter[str] = Counter()
    for record in selected:
        turn_id = str(record["turn_id"])
        assertion = assertions[turn_id]
        if not isinstance(assertion, dict):
            raise ValueError(f"Assertion for {turn_id} is not an object")
        failures = _check_record(record, assertion)
        seen[turn_id] += 1
        checks.append(
            {
                "trial": record.get("trial"),
                "conversation_id": record.get("conversation_id"),
                "turn_id": turn_id,
                "passed": not failures,
                "failures": failures,
            }
        )

    missing = sorted(set(assertions) - set(seen))
    failures = sum(not check["passed"] for check in checks)
    return {
        "schema_version": 1,
        "run": str(run.resolve()),
        "spec": str(spec_path.resolve()),
        "checks": len(checks),
        "passed": len(checks) - failures,
        "failed": failures,
        "missing_turns": missing,
        "results": checks,
    }


def _write_markdown(report: dict[str, Any], output: Path) -> None:
    lines = [
        "# Deterministic behavioral assertions",
        "",
        f"- Checks: {report['checks']}",
        f"- Passed: {report['passed']}",
        f"- Failed: {report['failed']}",
        f"- Missing turn definitions: {len(report['missing_turns'])}",
        "",
        "| Trial | Turn | Result | Failures |",
        "| ---: | --- | --- | --- |",
    ]
    for check in report["results"]:
        failures = "; ".join(check["failures"])
        lines.append(
            f"| {check['trial']} | {check['turn_id']} | "
            f"{'PASS' if check['passed'] else 'FAIL'} | {failures} |"
        )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    report = evaluate(args.run.resolve(), args.spec.resolve())
    output = args.output or args.run / "assertions"
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _write_markdown(report, output / "summary.md")
    print(json.dumps({key: report[key] for key in ("checks", "passed", "failed")}, indent=2))
    print(f"Artifacts: {output}")
    return 1 if args.strict and (report["failed"] or report["missing_turns"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
