import pytest

from shared.plan_evidence import (
    DOCUMENT_EVIDENCE_PROCESSES,
    EvidenceProcess,
    blatant_premium_amount_requested,
    filter_attributable_plan_results,
    has_query_relevant_table_conflict,
    results_require_focused_question,
)
from shared.rag import PlanSummary, SearchPlanMetadata, SearchPlanResult


def _result(
    *,
    plan_name: str,
    text: str,
    rank: int = 1,
    document_type: str = "mols-medicare-supplement-plans",
) -> SearchPlanResult:
    return SearchPlanResult(
        evidence_id=f"evidence-{rank}",
        rank=rank,
        score=0.9,
        plan_name=plan_name,
        text=text,
        metadata=SearchPlanMetadata(
            plan_name=plan_name,
            document_id=f"doc-{plan_name}",
            document_type=document_type,
        ),
    )


@pytest.mark.parametrize(
    "query",
    [
        "What is the monthly premium for Plan G?",
        "How much is my premium?",
        "Compare the premiums for Plan G and Plan N.",
        "Which plan has the lower premium?",
        "Show me the premium amount.",
    ],
)
def test_blatant_premium_amount_queries_are_identified(query):
    assert blatant_premium_amount_requested([query])


@pytest.mark.parametrize(
    "query",
    [
        "When is written notice of a premium change sent?",
        "Can I pay my monthly premium online?",
        "Why did my premium increase?",
        "How much does Plan G cost?",
        "What will I pay each month for this plan?",
        "How much would I pay to enroll in Plan N?",
        "What is the Part A deductible for Plan G?",
        "How much does an urgent-care visit cost?",
    ],
)
def test_premium_policy_and_benefit_cost_queries_remain_document_eligible(query):
    assert not blatant_premium_amount_requested([query])


def test_document_processes_apply_only_to_configured_types():
    plan_g = PlanSummary(plan_id="G", plan_name="Plan G")
    plan_n = PlanSummary(plan_id="N", plan_name="Plan N")
    correct = _result(
        plan_name="Plan G",
        text="# Plan G (continued)\n\nPlan pays 100% of Part B excess charges.",
    )
    results = [
        correct,
        _result(
            plan_name="Plan G",
            text="# Plan N (continued)\n\nYou pay office visit copayments.",
            rank=2,
        ),
        _result(
            plan_name="Plan G",
            text="## Benefit Chart\n\nNote: A ' ' means 100% of the benefit is paid.",
            rank=3,
        ),
        _result(plan_name="Plan G", text=correct.text, rank=4),
    ]

    filtered = filter_attributable_plan_results(results, [plan_g, plan_n])

    assert DOCUMENT_EVIDENCE_PROCESSES["mols-medicare-supplement-plans"] == frozenset(
        {
            EvidenceProcess.VALIDATE_STRUCTURAL_PLAN_MARKERS,
            EvidenceProcess.REJECT_LOST_COVERAGE_GLYPH,
            EvidenceProcess.REQUIRE_FOCUSED_QUESTION,
            EvidenceProcess.REJECT_QUERY_RELEVANT_TABLE_CONFLICT,
        }
    )
    assert results_require_focused_question(results)
    assert filtered == [correct]

    unconfigured = _result(
        plan_name="Plan G",
        text="A passage without a leading plan heading.",
        document_type="mols-another-plan-document",
    )
    assert not results_require_focused_question([unconfigured])
    assert filter_attributable_plan_results([unconfigured], [plan_g, plan_n]) == [unconfigured]


def test_unrecognized_layout_is_not_rejected_for_lacking_plan_heading():
    plan_g = PlanSummary(plan_id="G", plan_name="Plan G")
    result = _result(
        plan_name="Plan G",
        text="Benefit summary in a non-Markdown source layout.",
    )

    assert filter_attributable_plan_results([result], [plan_g]) == [result]


def test_markerless_passage_is_rejected_when_same_plan_has_structural_identity():
    plan_g = PlanSummary(plan_id="G", plan_name="Plan G")
    identified = _result(
        plan_name="Plan G",
        text="# Plan G\n\nA structurally identified passage.",
    )
    markerless = _result(
        plan_name="Plan G",
        rank=2,
        text="A fragment without structural plan identity.",
    )

    assert filter_attributable_plan_results([identified, markerless], [plan_g]) == [identified]


def test_structural_activation_is_independent_for_each_attributed_plan():
    plan_g = PlanSummary(plan_id="G", plan_name="Plan G")
    plan_n = PlanSummary(plan_id="N", plan_name="Plan N")
    identified_g = _result(
        plan_name="Plan G",
        text="# Plan G\n\nA structurally identified passage.",
    )
    alternate_n = _result(
        plan_name="Plan N",
        rank=2,
        text="Plan N evidence from an alternate source layout without Markdown markers.",
    )

    assert filter_attributable_plan_results(
        [identified_g, alternate_n],
        [plan_g, plan_n],
    ) == [identified_g, alternate_n]


def test_structural_plan_marker_rejects_internally_mixed_passage():
    plan_g = PlanSummary(plan_id="G", plan_name="Plan G")
    plan_n = PlanSummary(plan_id="N", plan_name="Plan N")
    mixed = _result(
        plan_name="Plan G",
        text=(
            "# Plan G (continued)\n\n"
            "| (continued) Plan N | (continued) Plan N | (continued) Plan N |\n"
            "| Service | $0 | All costs |"
        ),
    )

    assert filter_attributable_plan_results([mixed], [plan_g, plan_n]) == []


def test_query_relevant_table_conflict_is_detected_without_benefit_specific_rules():
    plan_g = PlanSummary(plan_id="G", plan_name="Plan G")
    first = _result(
        plan_name="Plan G",
        text=(
            "# Plan G\n\n"
            "| Requested service | Requested service | Requested service | Requested service |\n"
            "| Allowed amount | $0 | 100% | $0 |"
        ),
    )
    second = _result(
        plan_name="Plan G",
        rank=2,
        text=(
            "# Plan G\n\n"
            "| Requested service | Requested service | Requested service | Requested service |\n"
            "| Allowed amount | $0 | $0 | All costs |"
        ),
    )
    filtered = filter_attributable_plan_results([first, second], [plan_g])

    assert has_query_relevant_table_conflict(filtered, "Who pays for the requested service?")
    assert not has_query_relevant_table_conflict(filtered, "What is the foreign travel benefit?")


def test_same_row_under_different_table_groups_is_not_a_conflict():
    result = _result(
        plan_name="Plan G",
        text=(
            "# Plan G\n\n"
            "| Inpatient service | Inpatient service | Inpatient service |\n"
            "| Allowed amount | $0 | 100% |\n"
            "| Outpatient service | Outpatient service | Outpatient service |\n"
            "| Allowed amount | $20 | 80% |"
        ),
    )

    assert not has_query_relevant_table_conflict([result], "What is the allowed amount?")


def test_table_conflict_ignores_empty_queries_and_rows_without_comparable_values():
    result = _result(
        plan_name="Plan G",
        text=(
            "Not a table row\n"
            "| | empty | row |\n"
            "| --- | --- | --- |\n"
            "| Ungrouped row | $10 | $20 |\n"
            "| Requested service | Requested service | Requested service |\n"
            "| Description | member | plan |"
        ),
    )

    assert not has_query_relevant_table_conflict([result], "")
    assert not has_query_relevant_table_conflict([result], "requested service")
