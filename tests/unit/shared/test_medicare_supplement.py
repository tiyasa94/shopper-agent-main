from shared.medicare_supplement import payment_text
from shared.rag import SearchPlanMetadata, SearchPlanResult


def _result(*, plan_name, text, document_type="mols-medicare-supplement-plans"):
    return SearchPlanResult(
        evidence_id="test",
        rank=1,
        score=1,
        plan_name=plan_name,
        text=text,
        metadata=SearchPlanMetadata(plan_name=plan_name, document_type=document_type),
    )


def test_payment_text_preserves_service_period_payers_and_prose_without_unheaded_rows():
    text = (
        "## Hospital services\n"
        "| Services | Medicare pays | Plan pays | You pay |\n| - | - | - | - |\n"
        "| Hospitalization | Hospitalization | Hospitalization | Hospitalization |\n"
        "| First period | All but $120 | $120 | $0** |\n"
        "\n**Additional eligibility condition.\n"
        "| Unheaded fragment | $999 | $20 | $0 |"
    )
    result = _result(plan_name="Selected", text=text)
    rendered = payment_text(result)
    assert "Service: Hospitalization. Applicable row or period: First period." in rendered
    assert (
        "The member pays: $0**. The supplement plan pays: $120. Medicare pays: All but $120."
        in rendered
    )
    assert "**Additional eligibility condition." in rendered
    assert "$999" not in rendered
    assert "without those headers are unavailable, not evidence of noncoverage" in rendered
    assert result.text == text


def test_payment_text_does_not_invent_absent_payers():
    result = _result(
        plan_name="Selected", text="| Service | You pay |\n| - | - |\n| Office visit | $0 |"
    )
    rendered = payment_text(result)
    assert "The member pays: $0." in rendered
    assert "The supplement plan pays:" not in rendered
    assert "Medicare pays:" not in rendered


def test_payment_text_leaves_other_document_families_byte_for_byte_unchanged():
    text = "| Service | You pay |\n| - | - |\n| Office visit | $0 |"
    assert (
        payment_text(_result(plan_name="Selected", text=text, document_type="iols-plans")) == text
    )
