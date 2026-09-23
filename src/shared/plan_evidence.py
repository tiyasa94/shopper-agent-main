"""Deterministic document-family evidence policies for plan retrieval.

Plan-search orchestration should not need to understand source-specific extraction defects. This
module dispatches on the RAG-owned document type, removes passages with recognized defects, and
applies set-level confidence gates without assuming every source follows one layout.
"""

import html
import re
from collections.abc import Sequence
from enum import StrEnum

from shared.context import plan_identity_key
from shared.medicare_supplement import DOCUMENT_TYPE as MEDICARE_SUPPLEMENT_DOCUMENT_TYPE
from shared.rag import PlanSummary, SearchPlanResult


class EvidenceProcess(StrEnum):
    """Supported source-evidence checks selected by document-family policy."""

    VALIDATE_STRUCTURAL_PLAN_MARKERS = "validate_structural_plan_markers"
    REJECT_LOST_COVERAGE_GLYPH = "reject_lost_coverage_glyph"
    REQUIRE_FOCUSED_QUESTION = "require_focused_question"
    REJECT_QUERY_RELEVANT_TABLE_CONFLICT = "reject_query_relevant_table_conflict"


DOCUMENT_EVIDENCE_PROCESSES: dict[str, frozenset[EvidenceProcess]] = {
    MEDICARE_SUPPLEMENT_DOCUMENT_TYPE: frozenset(
        {
            EvidenceProcess.VALIDATE_STRUCTURAL_PLAN_MARKERS,
            EvidenceProcess.REJECT_LOST_COVERAGE_GLYPH,
            EvidenceProcess.REQUIRE_FOCUSED_QUESTION,
            EvidenceProcess.REJECT_QUERY_RELEVANT_TABLE_CONFLICT,
        }
    ),
}
_NO_EVIDENCE_PROCESSES: frozenset[EvidenceProcess] = frozenset()


def _document_type(result: SearchPlanResult) -> str:
    return (result.metadata.document_type or "").strip().casefold()


def _evidence_processes(result: SearchPlanResult) -> frozenset[EvidenceProcess]:
    return DOCUMENT_EVIDENCE_PROCESSES.get(_document_type(result), _NO_EVIDENCE_PROCESSES)


def results_require_focused_question(results: Sequence[SearchPlanResult]) -> bool:
    """Return whether every result's document family disallows unbounded overviews."""

    return bool(results) and all(
        EvidenceProcess.REQUIRE_FOCUSED_QUESTION in _evidence_processes(result)
        for result in results
    )


_BLATANT_PREMIUM_AMOUNT_PATTERN = re.compile(
    r"\b(?:what(?:'s| is| are)|show(?: me)?|tell me)\s+"
    r"(?:(?:the|my|our|your|this|that)\s+)?"
    r"(?:(?:monthly|annual|yearly|base|total|subsidized|estimated)\s+)?"
    r"premiums?(?:\s+(?:for|on|under)\b.{0,60})?\s*[?!.]*$|"
    r"\bhow much\s+(?:is|are|would|will|could|should)\b.{0,40}"
    r"\bpremiums?\b|"
    r"\b(?:compare|higher|lower|highest|lowest|cheaper|cheapest|more expensive|"
    r"less expensive)\b.{0,60}\bpremiums?\b|"
    r"\bpremiums?\s+(?:amount|cost|price|quote)\b",
    flags=re.IGNORECASE,
)


def blatant_premium_amount_requested(queries: Sequence[str]) -> bool:
    """Identify only explicit premium-amount requests that documents cannot source."""

    return any(_BLATANT_PREMIUM_AMOUNT_PATTERN.search(query) for query in queries)


def _has_lost_coverage_glyph(text: str) -> bool:
    """Detect a chart whose meaningful coverage glyph was lost during document parsing."""

    return (
        re.search(
            r"note:\s+a\s+['\"]\s*['\"]\s+means\s+100%",
            text,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _structural_plan_markers(text: str, available: Sequence[PlanSummary]) -> set[str]:
    """Return catalog plans named in Markdown headings or plan-label table cells."""

    available_keys = {
        plan_identity_key(plan.plan_name) for plan in available if plan_identity_key(plan.plan_name)
    }
    candidates: list[str] = []
    for line in text.splitlines():
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if heading:
            candidates.append(heading.group(1))
        if line.strip().startswith("|") and line.strip().endswith("|"):
            candidates.extend(line.strip().strip("|").split("|"))

    markers: set[str] = set()
    for candidate in candidates:
        normalized = html.unescape(candidate).strip()
        normalized = re.sub(
            r"^(?:\(continued\)\s*)|(?:\s*\(continued\))$",
            "",
            normalized,
            flags=re.IGNORECASE,
        ).strip()
        key = plan_identity_key(normalized)
        if key in available_keys:
            markers.add(key)
    return markers


_TABLE_VALUE_PATTERN = re.compile(
    r"\$\s*\d[\d,.]*|\d+(?:\.\d+)?\s*%|\ball costs?\b|\bno cost\b|"
    r"\bnot covered\b|\bcovered in full\b|\bbalance\b",
    flags=re.IGNORECASE,
)
_QUERY_STOP_WORDS = {
    "and",
    "are",
    "booklet",
    "does",
    "for",
    "from",
    "how",
    "is",
    "paid",
    "plan",
    "say",
    "the",
    "this",
    "what",
    "who",
}


def _normalized_table_cell(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip().casefold()


def _query_terms(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", html.unescape(value).casefold())
        if len(token) >= 3 and token not in _QUERY_STOP_WORDS
    }


def _table_claims(text: str) -> list[tuple[str, str, tuple[tuple[str, ...], ...]]]:
    """Extract valued rows under repeated-cell table group labels.

    Many benefit tables use a row repeated across every column as a group label, followed by
    specific rows whose remaining cells contain payer values. Rows without that structure are
    ignored so unfamiliar document layouts remain eligible for normal best-effort handling.
    """

    claims: list[tuple[str, str, tuple[tuple[str, ...], ...]]] = []
    group = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [_normalized_table_cell(cell) for cell in stripped.strip("|").split("|")]
        if len(cells) < 3 or not cells[0] or all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        if len(set(cells)) == 1:
            group = cells[0]
            continue
        if not group:
            continue
        signature = tuple(
            tuple(match.group(0).replace(" ", "") for match in _TABLE_VALUE_PATTERN.finditer(cell))
            for cell in cells[1:]
        )
        if any(signature):
            claims.append((group, cells[0], signature))
    return claims


def has_query_relevant_table_conflict(results: Sequence[SearchPlanResult], query: str) -> bool:
    """Return whether structurally comparable rows materially disagree for the query.

    This is a set-level confidence gate, not a parser for any particular plan or benefit. It only
    activates for configured document families, recognized repeated-cell Markdown tables, and a
    row/group whose meaningful terms overlap the current question.
    """

    query_terms = _query_terms(query)
    if not query_terms:
        return False
    signatures: dict[tuple[str, str, str, str], set[tuple[tuple[str, ...], ...]]] = {}
    for result in results:
        if EvidenceProcess.REJECT_QUERY_RELEVANT_TABLE_CONFLICT not in _evidence_processes(result):
            continue
        document_type = _document_type(result)
        plan_key = plan_identity_key(result.plan_name)
        for group, row, signature in _table_claims(result.text):
            if not query_terms.intersection(_query_terms(f"{group} {row}")):
                continue
            key = (document_type, plan_key, group, row)
            values = signatures.setdefault(key, set())
            values.add(signature)
            if len(values) > 1:
                return True
    return False


def filter_attributable_plan_results(
    results: Sequence[SearchPlanResult], available: Sequence[PlanSummary]
) -> list[SearchPlanResult]:
    """Drop known corrupt, cross-plan, or duplicate passages using document-family policies.

    Unconfigured document families pass through this attribution stage unchanged except for exact
    per-plan duplicate removal. An unrecognized structure passes through when no structural plan
    marker exists for that document family and attributed plan in the result set. Filtering is
    best-effort and does not itself establish that a retained passage supports an answer.
    """

    markers_by_result = [_structural_plan_markers(result.text, available) for result in results]
    structurally_identified_groups = {
        (
            _document_type(result),
            plan_identity_key(result.plan_name),
        )
        for result, markers in zip(results, markers_by_result, strict=True)
        if EvidenceProcess.VALIDATE_STRUCTURAL_PLAN_MARKERS in _evidence_processes(result)
        and markers
    }

    usable: list[SearchPlanResult] = []
    seen_text: set[tuple[str, str]] = set()
    for result, markers in zip(results, markers_by_result, strict=True):
        text = result.text
        processes = _evidence_processes(result)
        if EvidenceProcess.REJECT_LOST_COVERAGE_GLYPH in processes and _has_lost_coverage_glyph(
            text
        ):
            continue
        expected_plan_key = plan_identity_key(result.plan_name)
        structural_group = (_document_type(result), expected_plan_key)
        if (
            EvidenceProcess.VALIDATE_STRUCTURAL_PLAN_MARKERS in processes
            and structural_group in structurally_identified_groups
            and markers != {expected_plan_key}
        ):
            continue
        text_key = re.sub(r"\s+", " ", text).strip().casefold()
        attributed_text_key = (plan_identity_key(result.plan_name), text_key)
        if attributed_text_key in seen_text:
            continue
        seen_text.add(attributed_text_key)
        usable.append(result)
    return usable
