import pytest

from shared.effective_date import (
    UNESTABLISHED_EFFECTIVE_DATE_MESSAGE,
    is_personal_effective_date_query,
    requested_effective_date_message,
    validated_requested_effective_date,
)


@pytest.mark.parametrize(
    "query",
    [
        "If I enroll today, when would my Medicare coverage start?",
        "When would my coverage start if I enroll today?",
        "What is my coverage effective date if I enroll today?",
    ],
)
def test_personal_effective_date_query_accepts_either_clause_order(query):
    assert is_personal_effective_date_query(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "How is an enrollment effective date determined?",
        "What plans are available if I enroll today?",
        "When can I enroll?",
    ],
)
def test_personal_effective_date_query_rejects_general_enrollment_questions(query):
    assert is_personal_effective_date_query(query) is False


@pytest.mark.parametrize("value", ["", "2027-02-30", "02/01/2027", None])
def test_requested_effective_date_requires_a_valid_canonical_iso_date(value):
    assert validated_requested_effective_date(value) is None
    assert requested_effective_date_message(value) == UNESTABLISHED_EFFECTIVE_DATE_MESSAGE


def test_requested_effective_date_message_uses_the_validated_context_value():
    assert requested_effective_date_message("2027-02-01") == (
        "Based on your requested effective date, if you enroll today, your coverage is expected "
        "to begin on 2027-02-01, subject to eligibility verification, application review, and "
        "any required approvals."
    )
