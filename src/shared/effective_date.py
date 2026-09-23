"""Deterministic handling for context-backed coverage effective-date questions."""

import re
from datetime import date

REQUESTED_EFFECTIVE_DATE_MESSAGE_TEMPLATE = (
    "Based on your requested effective date, if you enroll today, your coverage is expected to "
    "begin on {requested_effective_date}, subject to eligibility verification, application "
    "review, and any required approvals."
)
UNESTABLISHED_EFFECTIVE_DATE_MESSAGE = (
    "The available information does not establish when your Medicare coverage would start. The "
    "date depends on the applicable enrollment period and your circumstances; contact Medicare or "
    "a licensed agent to confirm it."
)

_ENROLL_TODAY_PATTERN = re.compile(r"\b(?:if|when) i enroll today\b", flags=re.IGNORECASE)
_COVERAGE_PATTERN = re.compile(r"\b(?:coverage|benefits?)\b", flags=re.IGNORECASE)
_START_TIMING_PATTERN = re.compile(
    r"\b(?:start|begin|effective|effective date)\b",
    flags=re.IGNORECASE,
)


def is_personal_effective_date_query(query: str) -> bool:
    """Recognize a personal enroll-today coverage-start question in either clause order."""

    normalized = re.sub(r"\s+", " ", str(query or "")).strip()
    return all(
        pattern.search(normalized) is not None
        for pattern in (_ENROLL_TODAY_PATTERN, _COVERAGE_PATTERN, _START_TIMING_PATTERN)
    )


def validated_requested_effective_date(value: object) -> str | None:
    """Return one canonical, calendar-valid ISO date or ``None``."""

    candidate = str(value or "").strip()
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate if parsed.isoformat() == candidate else None


def requested_effective_date_message(value: object) -> str:
    """Render the approved caveated response, falling back safely without a valid date."""

    requested_effective_date = validated_requested_effective_date(value)
    if requested_effective_date is None:
        return UNESTABLISHED_EFFECTIVE_DATE_MESSAGE
    return REQUESTED_EFFECTIVE_DATE_MESSAGE_TEMPLATE.format(
        requested_effective_date=requested_effective_date
    )
